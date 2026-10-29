"""Shared chat core for the YouTube hub, Crowd Choice, and Crowd Play.

Stdlib only. Twitch is read over anonymous IRC on a WebSocket (justinfan); a
user OAuth token is optional and is the only way to send. YouTube live chat is
polled with liveChatMessages, never faster than pollingIntervalMillis, and it
stops before the daily quota is exhausted. Tokens are never logged.

Nothing here imports decky, so unit tests can drive it with recorded fixtures.
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

TWITCH_HOST = "irc-ws.chat.twitch.tv"
TWITCH_PORT = 443
TWITCH_ANON_PASS = "SCHMOOPIIE"
YT_LIVECHAT_COST = 5
YT_VIDEO_COST = 1
YT_QUOTA_GUARD = 9000
MIN_POLL_MS = 1000
BUS_RATE = 20
BUS_WINDOW = 1.0
SEND_INTERVAL = 1.5
OVERLAY_PORT = 28765
OVERLAY_HOST = "127.0.0.1"
MAX_MESSAGES = 20
MAX_TEXT = 500
SEEN_LIMIT = 500
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RECONNECT_DELAYS = (2, 5, 10)

_BANG_RE = re.compile(r"(?:^|\s)(!\d+)\b")
_WORD_RE = re.compile(r"(?:^|\s)([A-Za-z0-9_]+)")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TWITCH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_OAUTH_RE = re.compile(r"oauth:[^\s'\"]+", re.I)
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.I)
_KEY_RE = re.compile(r"(?:api_key|key)=[^&\s'\"]+", re.I)
_AIZA_RE = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")

log = logging.getLogger("chatcore")


class ChatError(Exception):
    pass


class QuotaExceeded(ChatError):
    pass


class ConnectionClosed(ChatError):
    pass


def redact(text, secrets=()):
    """Strip credentials from text that might reach a log or the panel."""
    if not text:
        return "" if text is None else text
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "<redacted>")
    text = _OAUTH_RE.sub("oauth:<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _KEY_RE.sub("key=<redacted>", text)
    text = _AIZA_RE.sub("<redacted>", text)
    return text


def _unescape_tag(value):
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            n = value[i + 1]
            out.append({"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}.get(n, n))
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _parse_tags(blob):
    tags = {}
    for part in blob.split(";"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        tags[k] = _unescape_tag(v)
    return tags


class ChatMessage:
    """One chat line, whatever platform it came from."""

    __slots__ = ("id", "platform", "channel", "user_id", "user_name", "text", "ts", "badges")

    def __init__(self, id, platform, channel, user_id, user_name, text, ts, badges=()):
        self.id = id or _fallback_id(platform, user_id or user_name, ts, text)
        self.platform = platform
        self.channel = channel
        self.user_id = user_id or user_name or ""
        self.user_name = (user_name or user_id or "?")[:64]
        self.text = (text or "")[:MAX_TEXT]
        self.ts = float(ts)
        self.badges = tuple(badges)

    def as_dict(self):
        return {
            "id": self.id, "platform": self.platform, "channel": self.channel,
            "user_id": self.user_id, "user_name": self.user_name, "text": self.text,
            "ts": self.ts, "badges": list(self.badges),
        }


def _fallback_id(platform, user, ts, text):
    raw = f"{platform}|{user}|{ts}|{text}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def parse_irc_line(line):
    """Parse one IRC line. Returns ('ping', token), a ChatMessage, or None."""
    line = line.strip("\r\n")
    if not line:
        return None
    tags = {}
    if line.startswith("@"):
        try:
            blob, line = line[1:].split(" ", 1)
        except ValueError:
            return None
        tags = _parse_tags(blob)
    prefix = ""
    if line.startswith(":"):
        try:
            prefix, line = line[1:].split(" ", 1)
        except ValueError:
            return None
    if " :" in line:
        head, trailing = line.split(" :", 1)
        parts = head.split(" ")
        params = parts[1:]
        params.append(trailing)
    else:
        parts = line.split(" ")
        params = parts[1:]
        trailing = ""
    cmd = parts[0].upper() if parts else ""
    if cmd == "PING":
        return ("ping", params[0] if params else trailing)
    if cmd != "PRIVMSG" or len(params) < 2:
        return None
    channel = params[0].lstrip("#")
    text = params[-1]
    nick = prefix.split("!", 1)[0] if prefix else ""
    user_name = tags.get("display-name") or nick
    user_id = tags.get("user-id") or nick
    badges = tuple(b.split("/", 1)[0] for b in tags.get("badges", "").split(",") if b)
    ts = time.time()
    if tags.get("tmi-sent-ts"):
        try:
            ts = int(tags["tmi-sent-ts"]) / 1000.0
        except ValueError:
            pass
    return ChatMessage(tags.get("id", ""), "twitch", channel, user_id, user_name, text, ts, badges)


def twitch_login_lines(channel, oauth="", login="", nick_fn=None):
    """IRC commands sent once after the socket opens. oauth is not logged by callers."""
    channel = channel.lstrip("#").lower()
    lines = ["CAP REQ :twitch.tv/tags twitch.tv/commands"]
    token = (oauth or "").strip()
    account = (login or "").strip().lower()
    if token and account:
        if token.lower().startswith("oauth:"):
            secret = token
        else:
            secret = "oauth:" + token
        nick = account
        lines.append("PASS " + secret)
    else:
        nick = nick_fn() if nick_fn else "justinfan" + str(10000 + (os.getpid() % 80000))
        lines.append("PASS " + TWITCH_ANON_PASS)
    lines.append("NICK " + nick)
    lines.append("JOIN #" + channel)
    return lines


def parse_vote(text, options):
    """Return the index of the first matching option, or None.

    `!1` / `!2` match as their own tokens. Keywords match a whole word.
    """
    if not text:
        return None
    bangs = {m.group(1).lower() for m in _BANG_RE.finditer(" " + text.lower())}
    words = {m.group(1).lower() for m in _WORD_RE.finditer(" " + text.lower())}
    for i, opt in enumerate(options):
        for w in opt["words"]:
            token = w.lower()
            if token.startswith("!") and token in bangs:
                return i
            if not token.startswith("!") and token in words:
                return i
    return None


def poll_options(spec=""):
    """`''` is !1 and !2. `'yes,no'` is those keywords plus !1 and !2."""
    parts = [p.strip() for p in (spec or "").split(",") if p.strip()]
    if not parts:
        parts = ["!1", "!2"]
    opts = []
    for i, label in enumerate(parts[:6], 1):
        words = {f"!{i}", label.lower()}
        opts.append({"label": label[:40], "words": tuple(words)})
    return tuple(opts)


class Poll:
    """One vote per user. The first choice sticks until the poll is stopped."""

    def __init__(self, options):
        self.options = options
        self.votes = {}
        self.open = True

    def cast(self, msg):
        if not self.open or not msg.user_id:
            return False
        if msg.user_id in self.votes:
            return False
        choice = parse_vote(msg.text, self.options)
        if choice is None:
            return False
        self.votes[msg.user_id] = choice
        return True

    def stop(self):
        self.open = False

    def snapshot(self):
        counts = [0] * len(self.options)
        for idx in self.votes.values():
            counts[idx] += 1
        return {
            "open": self.open,
            "voters": len(self.votes),
            "options": [{"label": o["label"], "count": counts[i]} for i, o in enumerate(self.options)],
        }


class EventBus:
    """In-process pub/sub. Chat messages are capped; status and polls are not."""

    def __init__(self, rate=BUS_RATE, window=BUS_WINDOW, clock=None):
        self.rate = rate
        self.window = window
        self._clock = clock or time.monotonic
        self._start = self._clock()
        self._n = 0
        self.dropped = 0
        self._subs = []

    def subscribe(self, fn):
        self._subs.append(fn)
        return lambda: self._subs.remove(fn) if fn in self._subs else None

    def publish(self, event, reliable=False):
        if not reliable and event.get("type") == "message":
            now = self._clock()
            if now - self._start >= self.window:
                self._start = now
                self._n = 0
            if self._n >= self.rate:
                self.dropped += 1
                return False
            self._n += 1
        for fn in list(self._subs):
            try:
                fn(event)
            except Exception:
                log.exception("chat subscriber failed")
        return True


def _rfc3339(value):
    if not value:
        return time.time()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def parse_livechat(body, channel=""):
    """Turn one liveChatMessages response into (interval_ms, next_token, messages)."""
    if not isinstance(body, dict):
        raise ChatError("YouTube returned a non-JSON body")
    err = body.get("error")
    if err:
        reasons = [e.get("reason", "") for e in err.get("errors") or []]
        if "quotaExceeded" in reasons or err.get("code") == 403 and "quota" in str(err.get("message", "")).lower():
            raise QuotaExceeded("YouTube quota exceeded")
        raise ChatError(redact(str(err.get("message") or err)[:300]))
    interval = body.get("pollingIntervalMillis")
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = 5000
    if interval < MIN_POLL_MS:
        interval = MIN_POLL_MS
    messages = []
    for item in body.get("items") or []:
        snip = item.get("snippet") or {}
        kind = snip.get("type") or ""
        if kind not in ("textMessageEvent", "superChatEvent"):
            continue
        author = item.get("authorDetails") or {}
        text = snip.get("displayMessage") or (snip.get("textMessageDetails") or {}).get("messageText") or ""
        badges = []
        if author.get("isChatOwner"):
            badges.append("owner")
        if author.get("isChatModerator"):
            badges.append("moderator")
        if author.get("isChatSponsor"):
            badges.append("sponsor")
        messages.append(ChatMessage(
            item.get("id", ""), "youtube", channel or snip.get("liveChatId", ""),
            author.get("channelId", ""), author.get("displayName", ""),
            text, _rfc3339(snip.get("publishedAt")), badges))
    return interval, body.get("nextPageToken") or "", messages


def parse_video_chat_id(body):
    if not isinstance(body, dict):
        return ""
    if body.get("error"):
        err = body["error"]
        reasons = [e.get("reason", "") for e in err.get("errors") or []]
        if "quotaExceeded" in reasons:
            raise QuotaExceeded("YouTube quota exceeded")
        raise ChatError(redact(str(err.get("message") or err)[:300]))
    for item in body.get("items") or []:
        details = item.get("liveStreamingDetails") or {}
        chat_id = details.get("activeLiveChatId") or ""
        if chat_id:
            return chat_id
    return ""


def yt_list_url(live_chat_id, page_token=""):
    q = {"part": "snippet,authorDetails", "liveChatId": live_chat_id, "maxResults": "200"}
    if page_token:
        q["pageToken"] = page_token
    return "https://www.googleapis.com/youtube/v3/liveChat/messages?" + urllib.parse.urlencode(q)


def yt_video_url(video_id):
    q = {"part": "liveStreamingDetails", "id": video_id}
    return "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(q)


def yt_headers(api_key="", oauth=""):
    """Key and bearer go in headers, never the URL, so they stay out of logs."""
    headers = {"Accept": "application/json", "User-Agent": "DeckLive"}
    token = (oauth or "").strip()
    key = (api_key or "").strip()
    if token:
        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1].strip()
        headers["Authorization"] = "Bearer " + token
    elif key:
        headers["X-Goog-Api-Key"] = key
    else:
        raise ChatError("YouTube chat needs an API key or OAuth token")
    return headers


def http_json(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1 << 20)
    except urllib.error.HTTPError as e:
        raw = e.read(1 << 16)
        try:
            return json.loads(raw.decode())
        except Exception:
            raise ChatError(f"YouTube HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise ChatError("YouTube network error") from None
    return json.loads(raw.decode())


def seconds_until_quota_reset(now=None):
    """YouTube Data API quota resets at midnight Pacific."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        tz = timezone(timedelta(hours=-8))
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    nxt = (current + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(1.0, (nxt - current).total_seconds())


def quota_date_key(now=None):
    """Return the Pacific calendar date used for YouTube quota accounting."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        tz = timezone(timedelta(hours=-8))
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    return current.date().isoformat()


def accept_value(key):
    digest = hashlib.sha1((key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def encode_frame(opcode, payload, mask=None):
    """RFC6455 frame. mask=None sends an unmasked server frame; bytes mask a client frame."""
    if isinstance(payload, str):
        payload = payload.encode()
    head = bytes([0x80 | (opcode & 0x0F)])
    n = len(payload)
    bit = 0x80 if mask is not None else 0
    if n < 126:
        head += bytes([bit | n])
    elif n < 65536:
        head += bytes([bit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", n)
    if mask is None:
        return head + payload
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WsConn:
    """A very small WebSocket on top of an asyncio stream. No extra packages."""

    def __init__(self, reader, writer, mask_out=False, expect_mask=False):
        self.reader = reader
        self.writer = writer
        self.mask_out = mask_out
        self.expect_mask = expect_mask
        self._buf = bytearray()
        self._write_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, host, port, path, ssl_ctx=None):
        reader, writer = await asyncio.open_connection(
            host, port, ssl=ssl_ctx, server_hostname=host if ssl_ctx else None)
        key = base64.b64encode(os.urandom(16)).decode()
        host_header = host if port in (80, 443) else f"{host}:{port}"
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        status = head.decode("iso-8859-1", "replace").split("\r\n", 1)[0]
        if " 101 " not in status:
            writer.close()
            raise ConnectionClosed("websocket upgrade failed")
        if accept_value(key) not in head.decode("iso-8859-1", "replace"):
            writer.close()
            raise ConnectionClosed("websocket accept mismatch")
        return cls(reader, writer, mask_out=True, expect_mask=False)

    async def send_frame(self, opcode, payload):
        mask = os.urandom(4) if self.mask_out else None
        async with self._write_lock:
            self.writer.write(encode_frame(opcode, payload, mask))
            await self.writer.drain()

    async def send_text(self, text):
        await self.send_frame(1, text.encode())

    async def _exact(self, n):
        while len(self._buf) < n:
            chunk = await self.reader.read(max(1024, n - len(self._buf)))
            if not chunk:
                raise ConnectionClosed()
            self._buf.extend(chunk)
            if len(self._buf) > (1 << 20):
                raise ConnectionClosed("frame too large")
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def recv_frame(self):
        b0, b1 = await self._exact(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if self.expect_mask and not masked:
            raise ConnectionClosed("client frame was not masked")
        if not self.expect_mask and masked:
            raise ConnectionClosed("server frame was masked")
        if length == 126:
            length = struct.unpack("!H", await self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._exact(8))[0]
        if length > (1 << 16):
            raise ConnectionClosed("frame too large")
        mask = await self._exact(4) if masked else b""
        payload = await self._exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    async def recv_text(self):
        parts = []
        while True:
            fin, opcode, payload = await self.recv_frame()
            if opcode == 9:  # ping
                await self.send_frame(10, payload)
                continue
            if opcode == 8:
                raise ConnectionClosed()
            if opcode in (1, 0) or (opcode == 2 and not parts):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")
            elif fin and parts:
                return b"".join(parts).decode("utf-8", "replace")

    async def close(self):
        with contextlib.suppress(Exception):
            await self.send_frame(8, b"")
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()


async def connect_twitch():
    import ssl
    ctx = ssl.create_default_context()
    return await WsConn.connect(TWITCH_HOST, TWITCH_PORT, "/", ssl_ctx=ctx)


OVERLAY_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Deck Live chat</title>
<style>
  html,body{margin:0;background:transparent;color:#fff;font:16px/1.35 sans-serif}
  #poll{padding:8px 10px}
  .bar{display:flex;align-items:center;gap:8px;margin:2px 0}
  .track{flex:1;height:8px;background:rgba(255,255,255,.15);border-radius:4px;overflow:hidden}
  .fill{height:100%;background:#6cf}
  #log{padding:4px 10px 10px}
  .row{margin:2px 0;text-shadow:0 1px 2px #000}
  b{color:#9cf}
</style></head><body>
<div id="poll"></div><div id="log"></div>
<script>
const pollEl = document.getElementById("poll");
const logEl = document.getElementById("log");
const rows = [];
function renderPoll(p) {
  pollEl.textContent = "";
  if (!p || !p.options) return;
  const total = p.options.reduce((s, o) => s + o.count, 0) || 1;
  p.options.forEach(o => {
    const bar = document.createElement("div");
    bar.className = "bar";
    const name = document.createElement("span");
    name.textContent = o.label + " " + o.count;
    const track = document.createElement("div");
    track.className = "track";
    const fill = document.createElement("div");
    fill.className = "fill";
    fill.style.width = Math.round(100 * o.count / total) + "%";
    track.appendChild(fill);
    bar.appendChild(name);
    bar.appendChild(track);
    pollEl.appendChild(bar);
  });
}
function add(m) {
  const row = document.createElement("div");
  row.className = "row";
  const who = document.createElement("b");
  who.textContent = m.user_name || "";
  row.appendChild(who);
  row.appendChild(document.createTextNode(" " + (m.text || "")));
  logEl.appendChild(row);
  rows.push(row);
  while (rows.length > 20) logEl.removeChild(rows.shift());
}
const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
ws.onmessage = (ev) => {
  let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
  if (m.type === "hello") {
    logEl.textContent = ""; rows.length = 0;
    (m.messages || []).forEach(add);
    renderPoll(m.poll);
  } else if (m.type === "message" && m.message) add(m.message);
  else if (m.type === "poll") renderPoll(m.poll);
};
</script></body></html>
"""


def _http_response(code, reason, body=b"", content_type="text/plain", extra=""):
    if isinstance(body, str):
        body = body.encode()
    head = (
        f"HTTP/1.1 {code} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        f"{extra}\r\n"
    )
    return head.encode() + body


class Overlay:
    """Local page an OBS browser source can load. Bound to loopback only."""

    def __init__(self, core):
        self.core = core
        self.server = None
        self.url = ""
        self.clients = set()

    async def start(self, host=OVERLAY_HOST, port=OVERLAY_PORT):
        if self.server is not None:
            return self.url
        if host != OVERLAY_HOST:
            raise ChatError("overlay listens on 127.0.0.1 only")
        last = None
        ports = [0] if port == 0 else [port] + list(range(port + 1, port + 10))
        for p in ports:
            try:
                self.server = await asyncio.start_server(self._client, host, p)
                break
            except OSError as e:
                last = e
                self.server = None
        if self.server is None:
            raise ChatError(f"overlay bind failed: {last}")
        sock = self.server.sockets[0].getsockname()
        self.url = f"http://{sock[0]}:{sock[1]}/"
        return self.url

    async def stop(self):
        for q in list(self.clients):
            with contextlib.suppress(Exception):
                q.put_nowait(None)
        self.clients.clear()
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None

    def notify(self, event):
        for q in list(self.clients):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    async def _client(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        except Exception:
            writer.close()
            return
        text = head.decode("iso-8859-1", "replace")
        line = text.split("\r\n", 1)[0]
        parts = line.split(" ")
        if len(parts) < 2 or parts[0] != "GET":
            writer.write(_http_response(405, "Method Not Allowed", "GET only"))
            await writer.drain()
            writer.close()
            return
        path = parts[1].split("?", 1)[0]
        headers = {}
        for row in text.split("\r\n")[1:]:
            if ":" in row:
                k, v = row.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        peer = writer.get_extra_info("peername")
        if not peer or peer[0] not in ("127.0.0.1", "::1"):
            writer.write(_http_response(403, "Forbidden", "localhost only"))
            await writer.drain()
            writer.close()
            return
        if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            await self._ws(reader, writer, headers)
            return
        if path in ("/", "/index.html"):
            writer.write(_http_response(200, "OK", OVERLAY_HTML, "text/html; charset=utf-8"))
        else:
            writer.write(_http_response(404, "Not Found", "not found"))
        with contextlib.suppress(Exception):
            await writer.drain()
            writer.close()

    async def _ws(self, reader, writer, headers):
        if len(self.clients) >= 4:
            writer.write(_http_response(503, "Too Many", "too many overlay clients"))
            await writer.drain()
            writer.close()
            return
        key = headers.get("sec-websocket-key", "")
        if not key:
            writer.close()
            return
        # 101 must not say Connection: close, or OBS drops the socket after the handshake.
        writer.write((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_value(key)}\r\n"
            "\r\n"
        ).encode())
        await writer.drain()
        ws = WsConn(reader, writer, mask_out=False, expect_mask=True)
        q = asyncio.Queue(maxsize=32)
        self.clients.add(q)
        hello = {"type": "hello", "messages": [m.as_dict() for m in self.core.messages],
                 "poll": self.core.poll.snapshot() if self.core.poll else None}
        try:
            await ws.send_text(json.dumps(hello))

            async def pump():
                while True:
                    event = await q.get()
                    if event is None:
                        break
                    await ws.send_text(json.dumps(event))

            writer_task = asyncio.ensure_future(pump())
            try:
                while True:
                    _fin, opcode, payload = await ws.recv_frame()
                    if opcode == 8:
                        break
                    if opcode == 9:
                        await ws.send_frame(10, payload)
            finally:
                writer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await writer_task
        except Exception:
            pass
        finally:
            self.clients.discard(q)
            await ws.close()


class ChatCore:
    def __init__(self, logger=None, twitch_connect=None, http_json_fn=None, sleep_fn=None, clock=None,
                 quota_path=None):
        self.log = logger or log
        self.bus = EventBus(clock=clock)
        self.overlay = Overlay(self)
        self.messages = deque(maxlen=MAX_MESSAGES)
        self._seen = deque(maxlen=SEEN_LIMIT)
        self._seen_set = set()
        self.poll = None
        self.platform = ""
        self.channel = ""
        self.state = "offline"
        self.detail = ""
        self.quota_path = quota_path
        self.quota_date = quota_date_key()
        self.quota_used = 0
        self._load_quota_usage()
        self.yt_key = ""
        self.oauth = ""
        self.login = ""
        self._twitch_connect = twitch_connect or connect_twitch
        self._http = http_json_fn or http_json
        self._sleep_fn = sleep_fn
        self._stop = asyncio.Event()
        self._task = None
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._reconnect = True
        self.quota_limit = YT_QUOTA_GUARD

    def _load_quota_usage(self):
        if not self.quota_path:
            return
        try:
            fd = os.open(self.quota_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as f:
                saved = json.load(f)
            used = saved.get("used")
            if (saved.get("date") == self.quota_date and type(used) is int and used >= 0):
                self.quota_used = used
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save_quota_usage(self):
        if not self.quota_path:
            return
        directory = os.path.dirname(self.quota_path)
        tmp = self.quota_path + ".tmp"
        try:
            os.makedirs(directory, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump({"date": self.quota_date, "used": self.quota_used}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.quota_path)
        except OSError:
            self.log.warning("Could not save YouTube quota usage")

    def _refresh_quota_date(self):
        date = quota_date_key()
        if date != self.quota_date:
            self.quota_date = date
            self.quota_used = 0
            self._save_quota_usage()

    def _charge_quota(self, units):
        self._refresh_quota_date()
        self.quota_used += units
        self._save_quota_usage()

    def _exhaust_quota(self):
        self._refresh_quota_date()
        self.quota_used = self.quota_limit
        self._save_quota_usage()

    def _secrets(self):
        vals = []
        for secret in (self.oauth, self.yt_key):
            if not secret:
                continue
            vals.append(secret)
            low = secret.lower()
            if low.startswith("oauth:"):
                vals.append(secret.split(":", 1)[1])
            elif low.startswith("bearer "):
                vals.append(secret.split(" ", 1)[1])
        return tuple(vals)

    def _emit(self, event, reliable=False):
        self.bus.publish(event, reliable=reliable)
        self.overlay.notify(event)

    def _set_status(self, state, detail=""):
        detail = redact(detail, self._secrets())[:300]
        self.state = state
        self.detail = detail
        self._emit({
            "type": "status", "state": state, "platform": self.platform,
            "channel": self.channel, "detail": detail,
        }, reliable=True)
        self.log.info("Chat %s %s %s %s", state, self.platform or "-", self.channel or "-", detail)

    def snapshot(self):
        self._refresh_quota_date()
        return {
            "state": self.state, "platform": self.platform, "channel": self.channel,
            "detail": self.detail, "messages": [m.as_dict() for m in self.messages],
            "poll": self.poll.snapshot() if self.poll else None,
            "overlay_url": self.overlay.url,
            "has_yt_key": bool(self.yt_key), "has_oauth": bool(self.oauth),
            "has_login": bool(self.login), "quota_used": self.quota_used,
            "dropped": self.bus.dropped,
        }

    async def ensure_overlay(self):
        if self.overlay.url:
            return self.overlay.url
        try:
            return await self.overlay.start()
        except Exception as e:
            self.log.warning("Chat overlay unavailable: %s", redact(str(e), self._secrets()))
            return ""

    def _remember(self, msg):
        if msg.id in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            old = self._seen.popleft()
            self._seen_set.discard(old)
        self._seen.append(msg.id)
        self._seen_set.add(msg.id)
        self.messages.append(msg)
        return True

    def _on_message(self, msg):
        if not self._remember(msg):
            return
        if self.poll and self.poll.cast(msg):
            self._emit({"type": "poll", "poll": self.poll.snapshot()}, reliable=True)
        self._emit({"type": "message", "message": msg.as_dict()})

    def start_poll(self, spec=""):
        self.poll = Poll(poll_options(spec))
        self._emit({"type": "poll", "poll": self.poll.snapshot()}, reliable=True)
        return self.poll.snapshot()

    def stop_poll(self):
        if self.poll:
            self.poll.stop()
        snap = self.poll.snapshot() if self.poll else None
        self._emit({"type": "poll", "poll": snap}, reliable=True)
        return snap

    async def connect(self, platform, channel, yt_key="", oauth="", login="", reconnect=True):
        await self.disconnect()
        self._refresh_quota_date()
        self.platform = (platform or "").strip().lower()
        self.channel = (channel or "").strip().lstrip("#")
        self.yt_key = (yt_key or "").strip()
        self.oauth = (oauth or "").strip()
        self.login = (login or "").strip()
        self._reconnect = reconnect
        self._stop = asyncio.Event()
        err = self._validate()
        if err:
            self._set_status("error", err)
            return {"ok": False, "error": err}
        self._task = asyncio.get_running_loop().create_task(self._runner())
        return {"ok": True}

    def _validate(self):
        if self.platform == "twitch":
            if not _TWITCH_LOGIN_RE.match(self.channel):
                return "Enter a Twitch channel name"
            return ""
        if self.platform == "youtube":
            if not self.channel:
                return "Enter a YouTube video id or live chat id"
            if not self.yt_key and not self.oauth:
                return "YouTube chat needs an API key or OAuth token"
            return ""
        return "Choose Twitch or YouTube"

    async def disconnect(self):
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        ws = self._ws
        self._ws = None
        if ws is not None:
            await ws.close()
        if self.state != "offline":
            self._set_status("offline", "")

    async def close(self):
        await self.disconnect()
        await self.overlay.stop()

    async def send(self, text):
        text = (text or "").replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return {"ok": False, "error": "Nothing to send"}
        if self.platform != "twitch":
            return {"ok": False, "error": "Sending is implemented for Twitch chat"}
        if not self.oauth or not self.login:
            return {"ok": False, "error": "Twitch OAuth and login are required to send"}
        if self._ws is None or self.state != "live":
            return {"ok": False, "error": "Chat is not connected"}
        async with self._send_lock:
            wait = SEND_INTERVAL - (time.monotonic() - self._last_send)
            if wait > 0:
                return {"ok": False, "error": "Slow down"}
            line = f"PRIVMSG #{self.channel} :{text[:450]}"
            try:
                await self._ws.send_text(line)
            except Exception as e:
                return {"ok": False, "error": redact(str(e), self._secrets())[:200]}
            self._last_send = time.monotonic()
        return {"ok": True}

    async def _sleep(self, seconds):
        if self._sleep_fn is not None:
            result = self._sleep_fn(seconds)
            if asyncio.iscoroutine(result):
                await result
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), seconds)

    async def _runner(self):
        try:
            if self.platform == "twitch":
                await self._run_twitch()
            else:
                await self._run_youtube()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._set_status("error", redact(str(e), self._secrets()))
        finally:
            if self.state not in ("error", "quota"):
                self.state = "offline"
                self.detail = ""

    async def _run_twitch(self):
        attempt = 0
        while not self._stop.is_set():
            ws = None
            failed = False
            opened = time.monotonic()
            try:
                self._set_status("connecting", "")
                ws = await self._twitch_connect()
                self._ws = ws
                for line in twitch_login_lines(self.channel, self.oauth, self.login):
                    await ws.send_text(line)
                self._set_status("live", "")
                buf = ""
                while not self._stop.is_set():
                    chunk = await ws.recv_text()
                    buf += chunk.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        await self._on_irc(line)
                    if buf:
                        # A frame may be one complete IRC line with no newline.
                        parsed = parse_irc_line(buf)
                        if parsed is not None:
                            await self._on_irc(buf)
                            buf = ""
            except asyncio.CancelledError:
                raise
            except ConnectionClosed:
                pass
            except Exception as e:
                failed = True
                self._set_status("error", redact(str(e), self._secrets()))
            finally:
                self._ws = None
                if ws is not None:
                    await ws.close()
            if self._stop.is_set() or not self._reconnect:
                break
            # A session that stayed up gets a fresh budget. A socket that dies
            # immediately does not, or a rejected login would reconnect forever.
            if not failed and time.monotonic() - opened >= 30:
                attempt = 0
            attempt += 1
            if attempt > len(RECONNECT_DELAYS):
                break
            if not failed and self.state == "live":
                self._set_status("connecting", "reconnecting")
            await self._sleep(RECONNECT_DELAYS[min(attempt - 1, len(RECONNECT_DELAYS) - 1)])
        if self.state != "error":
            self._set_status("offline", "")

    async def _on_irc(self, line):
        parsed = parse_irc_line(line)
        if parsed is None:
            return
        if isinstance(parsed, tuple) and parsed[0] == "ping":
            if self._ws is not None:
                await self._ws.send_text("PONG :" + parsed[1])
            return
        if isinstance(parsed, ChatMessage):
            self._on_message(parsed)

    async def _http_call(self, url, headers):
        fn = self._http
        if asyncio.iscoroutinefunction(fn):
            return await fn(url, headers)
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(url, headers))

    async def _run_youtube(self):
        self._set_status("connecting", "")
        chat_id = self.channel
        if _VIDEO_ID_RE.match(chat_id):
            self._refresh_quota_date()
            if self.quota_used + YT_VIDEO_COST > self.quota_limit:
                self._set_status("quota", f"paused at {self.quota_used} units")
                return
            body = await self._http_call(yt_video_url(chat_id), yt_headers(self.yt_key, self.oauth))
            self._charge_quota(YT_VIDEO_COST)
            chat_id = parse_video_chat_id(body)
            if not chat_id:
                self._set_status("error", "That video has no active live chat")
                return
        page = ""
        while not self._stop.is_set():
            self._refresh_quota_date()
            if self.quota_used + YT_LIVECHAT_COST > self.quota_limit:
                self._set_status("quota", f"paused at {self.quota_used} units")
                await self._sleep(seconds_until_quota_reset())
                if self._stop.is_set():
                    break
                continue
            try:
                body = await self._http_call(
                    yt_list_url(chat_id, page), yt_headers(self.yt_key, self.oauth))
            except QuotaExceeded:
                self._exhaust_quota()
                continue
            except Exception as e:
                self._set_status("error", redact(str(e), self._secrets()))
                return
            self._charge_quota(YT_LIVECHAT_COST)
            try:
                interval, page, messages = parse_livechat(body, chat_id)
            except QuotaExceeded:
                self._exhaust_quota()
                continue
            except ChatError as e:
                self._set_status("error", str(e))
                return
            if self.state != "live":
                self._set_status("live", "")
            for msg in messages:
                self._on_message(msg)
            await self._sleep(interval / 1000.0)
        if self.state != "error":
            self._set_status("offline", "")
