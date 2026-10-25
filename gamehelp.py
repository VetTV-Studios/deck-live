"""Game Help module for Deck Live: YouTube help videos for the running game.

Providers:
  1. YouTube Data API v3 (user's own key): search.list (own bucket, default 100 calls/day per
     project, 1 call each) + videos.list (1 unit from the shared 10,000/day bucket) for duration/views.
  2. yt-dlp fallback ("ytsearchN:<q>" --flat-playlist -J): no key, no quota.
Everything is cached (SQLite, 7 days) per normalized query; quota guard stops API use at
QUOTA_GUARD search calls per Pacific-time day and falls back automatically.
Network/process work runs off the event loop (threads / niced subprocess) so streaming isn't affected.
"""
import asyncio
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3"
TTL_S = 7 * 24 * 3600
QUOTA_GUARD = 90          # hard stop below Google's default 100 search.list calls/day
MAX_RESULTS = 15
SHOW = 8
# Pinned zipapp. SHA256 is the "yt-dlp" line of the release's published SHA2-256SUMS
# (https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/SHA2-256SUMS).
# A download that does not match is deleted and never executed. A yt-dlp already on PATH is preferred.
YTDLP_VERSION = "2026.08.19"
YTDLP_SHA256 = "1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6"
YTDLP_URL = f"https://github.com/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/yt-dlp"

TOPICS = {
    "walkthrough": "{game} walkthrough",
    "boss": "{game} boss guide",
    "achievements": "{game} achievement guide",
    # Handheld-first: covers Steam Deck, Legion Go, ROG Ally, etc.
    "settings": "{game} handheld best settings",
    "next": "{game} what to do next",
}
_EDITION = re.compile(
    r"\b(definitive|game of the year|goty|complete|deluxe|ultimate|enhanced|remastered|"
    r"anniversary|director'?s cut|gold|premium|special|collector'?s)\s+edition\b", re.I)


def normalize_game(name):
    n = re.sub(r"[\u2122\u00ae\u00a9]", "", name or "")
    n = _EDITION.sub("", n)
    n = re.sub(r"\s*[-:–]\s*$", "", n)
    n = re.sub(r"\s{2,}", " ", n).strip()
    # Short/generic titles ("Control", "Inside") match unrelated videos; disambiguate.
    if n and len(n.split()) == 1 and "game" not in n.lower():
        n += " game"
    return n


def build_query(game, topic, extra=""):
    g = normalize_game(game)
    if topic == "achievement" and extra:
        return f"{g} {extra} achievement guide"
    if topic == "custom" and extra:
        return f"{g} {extra}"
    return TOPICS.get(topic, TOPICS["walkthrough"]).format(game=g)


def pt_day(now=None):
    """Quota day key (YouTube quotas reset at midnight Pacific Time)."""
    now = now or time.time()
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:  # no tzdata in bundled Python: approximate with PST (UTC-8)
        return _dt.datetime.utcfromtimestamp(now - 8 * 3600).strftime("%Y-%m-%d")


def _iso_dur(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _ssl_ctx():
    for ca in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
               "/etc/ssl/cert.pem"):
        if os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


class QuotaError(Exception):
    pass


class OfflineError(Exception):
    pass


class ChecksumError(Exception):
    pass


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class GameHelp:
    def __init__(self, data_dir, logger, env_fn=lambda: dict(os.environ)):
        os.makedirs(data_dir, exist_ok=True)
        self.dir = data_dir
        self.log = logger
        self.env_fn = env_fn
        self.db_path = os.path.join(data_dir, "gamehelp.sqlite")
        self.ytdlp_local = os.path.join(data_dir, "yt-dlp")
        self._lock = asyncio.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, ts REAL, provider TEXT, data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS quota (day TEXT PRIMARY KEY, calls INTEGER)")

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            with db:  # commit/rollback
                yield db
        finally:
            db.close()

    # ---------- cache / quota ----------
    def _cache_get(self, k):
        with self._db() as db:
            row = db.execute("SELECT ts, provider, data FROM cache WHERE k=?", (k,)).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    def _cache_put(self, k, provider, data):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (k, time.time(), provider, json.dumps(data)))

    def quota_used(self):
        with self._db() as db:
            row = db.execute("SELECT calls FROM quota WHERE day=?", (pt_day(),)).fetchone()
        return row[0] if row else 0

    def _quota_take(self):
        day = pt_day()
        with self._db() as db:
            row = db.execute("SELECT calls FROM quota WHERE day=?", (day,)).fetchone()
            used = row[0] if row else 0
            if used >= QUOTA_GUARD:
                raise QuotaError(f"daily search quota guard reached ({used}/{QUOTA_GUARD})")
            db.execute("INSERT OR REPLACE INTO quota VALUES (?,?)", (day, used + 1))

    def _quota_exhaust(self):  # Google said quotaExceeded: stop for the rest of the PT day
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO quota VALUES (?,?)", (pt_day(), QUOTA_GUARD))

    # ---------- providers ----------
    def _http_json(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "DeckLive-GameHelp/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=8, context=_ssl_ctx()) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 403 and ("quotaExceeded" in body or "dailyLimitExceeded" in body):
                self._quota_exhaust()
                raise QuotaError("YouTube API quota exceeded")
            if e.code in (400, 403):
                raise QuotaError(f"YouTube API rejected the request ({e.code}); check the API key")
            raise OfflineError(f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OfflineError(str(e))

    def _api_search(self, key, q, channel_id=None, n=MAX_RESULTS):
        self._quota_take()
        params = {"part": "snippet", "q": q, "type": "video", "maxResults": n,
                  "videoEmbeddable": "true", "relevanceLanguage": "en", "safeSearch": "moderate", "key": key}
        if channel_id:
            params["channelId"] = channel_id
        res = self._http_json(f"{API}/search?{urllib.parse.urlencode(params)}")
        items = [{"id": it["id"]["videoId"], "title": it["snippet"]["title"],
                  "channel": it["snippet"]["channelTitle"], "channel_id": it["snippet"]["channelId"]}
                 for it in res.get("items", []) if it.get("id", {}).get("videoId")]
        if items:  # videos.list: 1 unit (shared 10k bucket), up to 50 ids
            ids = ",".join(i["id"] for i in items)
            det = self._http_json(f"{API}/videos?" + urllib.parse.urlencode(
                {"part": "contentDetails,statistics", "id": ids, "key": key}))
            info = {v["id"]: v for v in det.get("items", [])}
            for i in items:
                v = info.get(i["id"], {})
                i["duration"] = _iso_dur(v.get("contentDetails", {}).get("duration"))
                vc = v.get("statistics", {}).get("viewCount")
                i["views"] = int(vc) if vc else None
        return items

    def _verified_local(self):
        """The plugin's own copy, or None unless its bytes match the pinned checksum."""
        path = self.ytdlp_local
        if not os.path.isfile(path):
            return None
        try:
            ok = _file_sha256(path) == YTDLP_SHA256
        except OSError:
            return None
        return path if ok else None

    def ytdlp_path(self):
        """Distro/user binary on PATH, else a checksum-verified download. Never an unverified file."""
        system = shutil.which("yt-dlp")
        local = self._verified_local()
        if system:
            try:
                if os.path.isfile(self.ytdlp_local) and os.path.samefile(system, self.ytdlp_local):
                    system = None  # our download is on PATH; it still has to match the pin
            except OSError:
                pass
        if system:
            return system
        return local

    async def _ytdlp_search(self, q, channel_id=None, n=MAX_RESULTS):
        exe = self.ytdlp_path()
        if not exe:
            raise OfflineError("yt-dlp not installed (Settings: Download yt-dlp, or add a YouTube API key)")
        if os.path.isfile(self.ytdlp_local):
            try:
                same = os.path.samefile(exe, self.ytdlp_local)
            except OSError:
                same = False
            if same and self._verified_local() is None:
                raise OfflineError("yt-dlp download failed checksum verification and was not run")
        target = (f"https://www.youtube.com/channel/{channel_id}/search?query={urllib.parse.quote(q)}"
                  if channel_id else f"ytsearch{n}:{q}")
        py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
        cmd = ([py, exe] if exe == self.ytdlp_local else [exe])
        cmd = ["nice", "-n", "19"] + cmd + [target, "--flat-playlist", "-J", "--no-warnings",
                                            "--playlist-end", str(n), "--socket-timeout", "8"]
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, env=self.env_fn())
        try:
            out, err = await asyncio.wait_for(p.communicate(), 25)
        except asyncio.TimeoutError:
            p.kill()
            raise OfflineError("yt-dlp timed out")
        if p.returncode != 0:
            raise OfflineError("yt-dlp failed: " + err.decode(errors="replace").strip().splitlines()[-1:][0][:200]
                               if err.strip() else f"yt-dlp exited {p.returncode}")
        items = []
        for e in json.loads(out or b"{}").get("entries") or []:
            if e.get("id") and (e.get("_type") in (None, "url")) and len(e["id"]) == 11:
                items.append({"id": e["id"], "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
                              "channel_id": e.get("channel_id"), "duration": int(e["duration"]) if e.get("duration") else None,
                              "views": e.get("view_count")})
        return items

    async def install_ytdlp(self):
        """Download the pinned official zipapp. It is kept only when the SHA256 matches."""
        def dl():
            tmp = self.ytdlp_local + ".part"
            req = urllib.request.Request(YTDLP_URL, headers={"User-Agent": "DeckLive"})
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                h = hashlib.sha256()
                with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as r, os.fdopen(fd, "wb") as f:
                    fd = None
                    while True:
                        b = r.read(1 << 16)
                        if not b:
                            break
                        h.update(b)
                        f.write(b)
                digest = h.hexdigest()
                if digest != YTDLP_SHA256:
                    raise ChecksumError(
                        f"yt-dlp checksum mismatch (got {digest[:12]}, expected {YTDLP_SHA256[:12]})")
                os.chmod(tmp, 0o700)
                os.replace(tmp, self.ytdlp_local)
            finally:
                if fd is not None:
                    os.close(fd)
                if os.path.exists(tmp):
                    os.unlink(tmp)
        await asyncio.get_event_loop().run_in_executor(None, dl)
        if self._verified_local() is None:
            raise ChecksumError("downloaded yt-dlp failed verification and was not kept")
        return self.ytdlp_local

    # ---------- public ----------
    async def search(self, game, topic="walkthrough", extra="", api_key="", pin_channel="", refresh=False):
        q = build_query(game, topic, extra)
        k = q.lower()
        loop = asyncio.get_event_loop()
        offline = False
        # The cache check is inside the lock. Prefetch and the panel both search the same
        # query; without this, both miss and both spend a search.list unit.
        async with self._lock:
            cached = self._cache_get(k)
            fresh = bool(cached) and not refresh and time.time() - cached[0] < TTL_S
            provider, items, note, ts, from_cache = None, None, "", None, False
            if fresh:
                provider, items, ts, from_cache = cached[1], cached[2], cached[0], True
            else:
                if api_key:
                    try:
                        items = await loop.run_in_executor(None, self._api_search, api_key, q)
                        provider = "api"
                    except (QuotaError, OfflineError) as e:
                        note = str(e)
                        self.log.warning(f"GameHelp API unavailable: {e}; falling back to yt-dlp")
                if items is None:
                    try:
                        items = await self._ytdlp_search(q)
                        provider = "yt-dlp"
                    except OfflineError as e:
                        if cached:  # offline: serve stale cache
                            provider, items, ts, from_cache = cached[1], cached[2], cached[0], True
                            offline = True
                            note = str(e)
                        else:
                            return self._result(q, None, [], error=(note + "; " if note else "") + str(e))
                if items is not None and not from_cache:
                    self._cache_put(k, provider, items)
            res = await self._with_pins(items, game, q, api_key, pin_channel)
        return self._result(q, provider, res, cache=from_cache, ts=ts, offline=offline, note=note)

    async def _with_pins(self, items, game, q, api_key, pin_channel):
        """Pinned channel's matching videos first (own query, cached 7 days)."""
        if not pin_channel:
            return items[:SHOW]
        pk = f"pin:{pin_channel}:{q.lower()}"
        cached = self._cache_get(pk)
        pins = None
        if cached and time.time() - cached[0] < TTL_S:
            pins = cached[2]
        else:
            try:
                if api_key:
                    pins = await asyncio.get_event_loop().run_in_executor(
                        None, self._api_search, api_key, normalize_game(game), pin_channel, 5)
                else:
                    pins = (await self._ytdlp_search(normalize_game(game), pin_channel, 5))
                self._cache_put(pk, "pin", pins)
            except (QuotaError, OfflineError) as e:
                self.log.info(f"GameHelp pin lookup skipped: {e}")
                pins = cached[2] if cached else []
        g = normalize_game(game).replace(" game", "").lower()
        pins = [dict(p, pinned=True) for p in pins if g and g in p["title"].lower()][:3]
        seen = {p["id"] for p in pins}
        return (pins + [i for i in items if i["id"] not in seen])[:SHOW]

    def _result(self, q, provider, items, cache=False, ts=None, offline=False, note="", error=""):
        return {"query": q, "provider": provider, "items": items, "cached": cache,
                "cached_at": ts, "offline": offline, "note": note, "error": error,
                "quota_used": self.quota_used(), "quota_limit": QUOTA_GUARD,
                "ytdlp": bool(self.ytdlp_path())}
