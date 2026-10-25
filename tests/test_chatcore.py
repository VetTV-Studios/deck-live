"""Chat core tests. Recorded fixtures only: no Twitch or YouTube network."""
import asyncio, json, logging, os, unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import chatcore as cc

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def run(coro):
    return asyncio.run(coro)


def run_session(core, **kwargs):
    """Connect and run the reader on one loop. asyncio.run cancels tasks it did not await."""
    async def go():
        result = await core.connect(**kwargs)
        if core._task is not None:
            await core._task
        return result
    return run(go())


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


class Irc(unittest.TestCase):
    def test_fixture_messages_ping_and_ignores(self):
        messages = []
        pings = []
        for line in fixture("twitch_irc.txt").splitlines():
            parsed = cc.parse_irc_line(line)
            if isinstance(parsed, cc.ChatMessage):
                messages.append(parsed)
            elif isinstance(parsed, tuple) and parsed[0] == "ping":
                pings.append(parsed)
        self.assertEqual(len(messages), 4)
        self.assertEqual(pings, [("ping", "tmi.twitch.tv")])
        vet = messages[0]
        self.assertEqual(vet.user_name, "Vet TV")  # \s escape
        self.assertEqual(vet.user_id, "111")
        self.assertEqual(vet.text, "hello !1")
        self.assertEqual(vet.id, "msg-1")
        self.assertIn("moderator", vet.badges)
        self.assertEqual(vet.ts, 1710000000)
        self.assertEqual(messages[2].user_name, "bob")
        self.assertEqual(messages[2].user_id, "bob")
        self.assertEqual(messages[2].text, "plain hi")

    def test_anonymous_login_does_not_send_a_token(self):
        lines = cc.twitch_login_lines("VetTV", oauth="oauth:super-secret-token", login="")
        blob = "\n".join(lines)
        self.assertNotIn("super-secret-token", blob)
        self.assertIn("PASS " + cc.TWITCH_ANON_PASS, lines)
        self.assertTrue(any(line.startswith("NICK justinfan") for line in lines))
        self.assertIn("JOIN #vettv", lines)

    def test_oauth_login_is_on_the_wire_only_with_a_nick(self):
        lines = cc.twitch_login_lines("Chan", oauth="raw-token", login="Vet")
        self.assertIn("PASS oauth:raw-token", lines)
        self.assertIn("NICK vet", lines)
        self.assertIn("JOIN #chan", lines)


class Votes(unittest.TestCase):
    def test_bang_keywords_and_one_vote_per_user(self):
        poll = cc.Poll(cc.poll_options("yes,no"))
        vet = cc.ChatMessage("1", "twitch", "c", "111", "Vet", "!1", 1)
        self.assertTrue(poll.cast(vet))
        self.assertFalse(poll.cast(cc.ChatMessage("2", "twitch", "c", "111", "Vet", "!2", 2)))
        self.assertFalse(poll.cast(cc.ChatMessage("3", "twitch", "c", "111", "Vet", "no", 3)))
        self.assertTrue(poll.cast(cc.ChatMessage("4", "twitch", "c", "222", "Ace", "NO thanks", 4)))
        self.assertFalse(poll.cast(cc.ChatMessage("5", "twitch", "c", "333", "Bea", "hello", 5)))
        self.assertEqual(poll.snapshot()["options"][0]["count"], 1)
        self.assertEqual(poll.snapshot()["options"][1]["count"], 1)
        self.assertEqual(poll.snapshot()["voters"], 2)
        poll.stop()
        self.assertFalse(poll.cast(cc.ChatMessage("6", "twitch", "c", "444", "Cy", "!1", 6)))
        again = cc.Poll(cc.poll_options())
        self.assertTrue(again.cast(cc.ChatMessage("7", "twitch", "c", "111", "Vet", "I pick !2", 7)))
        self.assertEqual(again.votes["111"], 1)
        self.assertIsNone(cc.parse_vote("see !12", cc.poll_options()))
        self.assertEqual(cc.parse_vote("!1", cc.poll_options()), 0)


class Bus(unittest.TestCase):
    def test_messages_are_capped_and_status_is_not(self):
        t = {"n": 0.0}
        bus = cc.EventBus(rate=2, window=1, clock=lambda: t["n"])
        got = []
        bus.subscribe(got.append)
        self.assertTrue(bus.publish({"type": "message", "n": 1}))
        self.assertTrue(bus.publish({"type": "message", "n": 2}))
        self.assertFalse(bus.publish({"type": "message", "n": 3}))
        self.assertEqual(bus.dropped, 1)
        self.assertTrue(bus.publish({"type": "poll", "poll": {}}, reliable=True))
        self.assertEqual(got[-1]["type"], "poll")
        t["n"] = 1.0
        self.assertTrue(bus.publish({"type": "message", "n": 4}))
        self.assertEqual([e.get("n") for e in got if e["type"] == "message"], [1, 2, 4])

    def test_overlay_only_receives_published_messages(self):
        core = cc.ChatCore(clock=lambda: 0)
        core.bus.rate = 1
        received = []
        core.overlay.notify = received.append
        self.assertTrue(core._emit({"type": "message", "n": 1}))
        self.assertFalse(core._emit({"type": "message", "n": 2}))
        self.assertEqual([event["n"] for event in received], [1])


class YouTube(unittest.TestCase):
    def test_quota_reset_uses_elapsed_time_across_dst(self):
        fall_back = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        spring_forward = datetime(2026, 3, 8, 0, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        self.assertEqual(cc.seconds_until_quota_reset(fall_back), 24.5 * 60 * 60 + 5)
        self.assertEqual(cc.seconds_until_quota_reset(spring_forward), 22.5 * 60 * 60 + 5)

    def test_quota_usage_survives_connections_until_pacific_date_changes(self):
        updates = []
        core = cc.ChatCore(quota_used=123, quota_date="2026-10-25",
                           quota_callback=lambda date, used: updates.append((date, used)))
        same_day = datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        next_day = datetime(2026, 10, 26, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
        core._refresh_quota_date(same_day)
        self.assertEqual(core.quota_used, 123)
        core._refresh_quota_date(next_day)
        self.assertEqual(core.quota_used, 0)
        self.assertEqual(updates, [("2026-10-26", 0)])

    def test_fixture_parse_skips_non_text_and_honors_interval_floor(self):
        body = json.loads(fixture("youtube_livechat.json"))
        interval, page, messages = cc.parse_livechat(body, "CHATID")
        self.assertEqual(interval, 7500)
        self.assertEqual(page, "page-2")
        self.assertEqual([m.id for m in messages], ["LCC.msg1", "LCC.msg2"])
        self.assertEqual(messages[0].user_name, "Vet")
        self.assertIn("moderator", messages[0].badges)
        self.assertIn("sponsor", messages[1].badges)
        fast = dict(body, pollingIntervalMillis=100)
        self.assertEqual(cc.parse_livechat(fast)[0], cc.MIN_POLL_MS)
        with self.assertRaises(cc.QuotaExceeded):
            cc.parse_livechat(json.loads(fixture("youtube_quota.json")))

    def test_key_and_oauth_stay_out_of_the_url(self):
        url = cc.yt_list_url("CHATID", "page-2")
        self.assertNotIn("AIza", url)
        self.assertNotIn("oauth", url)
        self.assertIn("liveChatId=CHATID", url)
        self.assertIn("pageToken=page-2", url)
        key_headers = cc.yt_headers(api_key="AIzaSySUPERSECRETKEY12")
        self.assertEqual(key_headers["X-Goog-Api-Key"], "AIzaSySUPERSECRETKEY12")
        self.assertNotIn("Authorization", key_headers)
        oauth_headers = cc.yt_headers(api_key="AIzaSySUPERSECRETKEY12", oauth="ya29.secret")
        self.assertEqual(oauth_headers["Authorization"], "Bearer ya29.secret")
        self.assertNotIn("X-Goog-Api-Key", oauth_headers)

    def test_youtube_uses_its_oauth_not_the_twitch_oauth(self):
        headers = []

        async def http(_url, request_headers):
            headers.append(dict(request_headers))
            return {"items": [], "pollingIntervalMillis": 1000}

        async def sleep(_seconds):
            core._stop.set()

        core = cc.ChatCore(http_json_fn=http, sleep_fn=sleep)
        run_session(core, platform="youtube", channel="CHATID", oauth="twitch-token",
                    yt_oauth="google-token")
        self.assertTrue(headers[0]["Authorization"].endswith("google-token"))
        self.assertNotIn("twitch-token", headers[0]["Authorization"])

    def test_poll_waits_for_the_polling_interval(self):
        first = json.loads(fixture("youtube_livechat.json"))
        calls = []

        async def http(url, headers):
            calls.append((url, dict(headers)))
            if len(calls) == 1:
                return first
            repeat = json.loads(fixture("youtube_livechat.json"))
            repeat["items"] = [first["items"][0], {
                "id": "LCC.msg3",
                "snippet": {"type": "textMessageEvent", "publishedAt": "2026-10-18T20:00:02.000Z",
                            "displayMessage": "third"},
                "authorDetails": {"channelId": "UCcy", "displayName": "Cy"},
            }]
            repeat["nextPageToken"] = "page-3"
            return repeat

        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                core._stop.set()

        core = cc.ChatCore(http_json_fn=http, sleep_fn=sleep)
        result = run_session(core, platform="youtube", channel="CHATID",
                             yt_key="AIzaSySUPERSECRETKEY12", reconnect=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(sleeps[0], 7.5)
        self.assertIn("pageToken=page-2", calls[1][0])
        self.assertNotIn("AIza", calls[0][0])
        self.assertEqual(calls[0][1]["X-Goog-Api-Key"], "AIzaSySUPERSECRETKEY12")
        self.assertEqual([m.id for m in core.messages], ["LCC.msg1", "LCC.msg2", "LCC.msg3"])
        self.assertEqual(core.quota_used, cc.YT_LIVECHAT_COST * 2)

    def test_quota_guard_does_not_poll_again(self):
        calls = []

        async def http(url, headers):
            calls.append((url, dict(headers)))
            return json.loads(fixture("youtube_livechat.json"))

        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                core._stop.set()

        core = cc.ChatCore(http_json_fn=http, sleep_fn=sleep)
        core.quota_limit = cc.YT_LIVECHAT_COST
        run_session(core, platform="youtube", channel="CHATID", oauth="ya29.secret")
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(len(sleeps), 2)
        self.assertEqual(core.state, "offline")
        self.assertNotIn("ya29.secret", calls[0][0])
        self.assertEqual(calls[0][1]["Authorization"], "Bearer ya29.secret")

    def test_video_id_resolves_once(self):
        urls = []

        async def http(url, headers):
            urls.append(url)
            if "/videos?" in url:
                return {"items": [{"liveStreamingDetails": {"activeLiveChatId": "CHATID"}}]}
            return {"items": [], "pollingIntervalMillis": 5000, "nextPageToken": "n"}

        async def sleep(_seconds):
            core._stop.set()

        core = cc.ChatCore(http_json_fn=http, sleep_fn=sleep)
        run_session(core, platform="youtube", channel="dQw4w9WgXcQ", yt_key="AIzaSySUPERSECRETKEY12")
        self.assertIn("/videos?", urls[0])
        self.assertIn("liveChatId=CHATID", urls[1])
        self.assertNotIn("AIza", urls[0] + urls[1])
        self.assertEqual(core.quota_used, cc.YT_VIDEO_COST + cc.YT_LIVECHAT_COST)

    def test_video_quota_exhaustion_waits_and_retries_resolution(self):
        urls = []

        async def http(url, _headers):
            urls.append(url)
            if "/videos?" in url and urls.count(url) == 1:
                raise cc.QuotaExceeded()
            if "/videos?" in url:
                return {"items": [{"liveStreamingDetails": {"activeLiveChatId": "CHATID"}}]}
            return {"items": [], "pollingIntervalMillis": 1000}

        sleeps = []

        async def sleep(_seconds):
            sleeps.append(_seconds)
            if len(sleeps) == 1:
                core._set_quota_used(0)
            else:
                core._stop.set()

        core = cc.ChatCore(http_json_fn=http, sleep_fn=sleep)
        core.quota_limit = 10
        run_session(core, platform="youtube", channel="dQw4w9WgXcQ", yt_key="AIzaSySUPERSECRETKEY12")
        self.assertEqual(sum("/videos?" in url for url in urls), 2)
        self.assertTrue(any("liveChatId=CHATID" in url for url in urls))
        self.assertEqual(len(sleeps), 2)

    def test_errors_are_redacted(self):
        token = "oauth:super-secret-token"
        key = "AIzaSySUPERSECRETKEY12"
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("chat-redact-test")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.addHandler(H())
        logger.propagate = False

        async def http(url, headers):
            raise RuntimeError(f"boom {token} {key} Bearer ya29.secret key=abc")

        async def sleep(_s):
            core._stop.set()

        core = cc.ChatCore(logger=logger, http_json_fn=http, sleep_fn=sleep)
        run_session(core, platform="youtube", channel="CHATID", yt_key=key, oauth=token)
        blob = core.detail + "\n" + json.dumps(core.snapshot()) + "\n" + "\n".join(records)
        self.assertNotIn("super-secret-token", blob)
        self.assertNotIn(key, blob)
        self.assertNotIn("ya29.secret", blob)
        self.assertIn("<redacted>", blob)


class TwitchSession(unittest.TestCase):
    def test_fixture_is_read_and_ponged_without_logging_the_token(self):
        blob = fixture("twitch_irc.txt")
        sent = []

        class FakeWs:
            def __init__(self):
                self._done = False

            async def send_text(self, text):
                sent.append(text)

            async def recv_text(self):
                if self._done:
                    raise cc.ConnectionClosed()
                self._done = True
                return blob

            async def close(self):
                pass

        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("chat-twitch-test")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.addHandler(H())
        logger.propagate = False
        token = "oauth:super-secret-token"

        async def connect():
            return FakeWs()

        core = cc.ChatCore(logger=logger, twitch_connect=connect)
        core.start_poll("yes,no")
        run_session(core, platform="twitch", channel="VetTV", oauth=token, login="vet", reconnect=False)
        self.assertIn("PONG :tmi.twitch.tv", sent)
        self.assertIn("PASS oauth:super-secret-token", sent)  # on the wire, not in the log
        self.assertEqual(list(core.messages), [])
        snap = core.poll.snapshot()
        self.assertEqual(snap["options"][0]["count"], 0)
        self.assertEqual(snap["options"][1]["count"], 0)
        self.assertEqual(snap["voters"], 0)
        logs = "\n".join(records) + json.dumps(core.snapshot())
        self.assertNotIn("super-secret-token", logs)
        self.assertEqual(core.state, "error")
        self.assertIn("Login authentication failed", core.detail)

    def test_session_is_not_live_until_own_join_acknowledgement(self):
        statuses = []

        class FakeWs:
            def __init__(self):
                self.chunks = [":vet!vet@vet.tmi.twitch.tv JOIN #vettv\r\n",
                               ":vet!vet@vet.tmi.twitch.tv PRIVMSG #vettv :hello\r\n"]

            async def send_text(self, _text):
                pass

            async def recv_text(self):
                if self.chunks:
                    return self.chunks.pop(0)
                raise cc.ConnectionClosed()

            async def close(self):
                pass

        core = cc.ChatCore(twitch_connect=lambda: asyncio.sleep(0, result=FakeWs()))
        core.bus.subscribe(lambda event: statuses.append(event["state"]) if event["type"] == "status" else None)
        run_session(core, platform="twitch", channel="vettv", oauth="token", login="vet", reconnect=False)
        self.assertLess(statuses.index("connecting"), statuses.index("live"))
        self.assertEqual([msg.text for msg in core.messages], ["hello"])
        self.assertEqual(core.state, "offline")

    def test_a_flapping_socket_stops(self):
        n = {"i": 0}

        class FakeWs:
            async def send_text(self, _text):
                return None

            async def recv_text(self):
                raise cc.ConnectionClosed()

            async def close(self):
                return None

        async def connect():
            n["i"] += 1
            return FakeWs()

        async def sleep(_seconds):
            return None

        core = cc.ChatCore(twitch_connect=connect, sleep_fn=sleep)
        run_session(core, platform="twitch", channel="vettv", reconnect=True)
        self.assertEqual(n["i"], 1 + len(cc.RECONNECT_DELAYS))
        self.assertEqual(core.state, "offline")

    def test_send_requires_oauth_and_is_rate_limited(self):
        core = cc.ChatCore()

        class FakeWs:
            def __init__(self):
                self.sent = []

            async def send_text(self, text):
                self.sent.append(text)

            async def close(self):
                pass

        ws = FakeWs()
        core.platform = "twitch"
        core.channel = "vettv"
        core.state = "live"
        core._ws = ws
        denied = run(core.send("hi"))
        self.assertFalse(denied["ok"])
        self.assertEqual(ws.sent, [])
        core.oauth = "oauth:super-secret-token"
        core.login = "vet"
        ok = run(core.send("hello crowd"))
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ws.sent, ["PRIVMSG #vettv :hello crowd"])
        slow = run(core.send("again"))
        self.assertEqual(slow.get("error"), "Slow down")
        self.assertNotIn("super-secret-token", json.dumps(ok) + json.dumps(slow) + json.dumps(core.snapshot()))


class Frames(unittest.TestCase):
    def test_masked_roundtrip(self):
        mask = b"\x11\x22\x33\x44"
        frame = cc.encode_frame(1, b"hello", mask)
        self.assertEqual(frame[1] & 0x80, 0x80)

        class Reader:
            def __init__(self, data):
                self.data = data

            async def read(self, n):
                out = self.data[:n]
                self.data = self.data[n:]
                return out

        async def go():
            ws = cc.WsConn(Reader(frame), None, expect_mask=True)
            return await ws.recv_frame()

        fin, opcode, payload = run(go())
        self.assertTrue(fin)
        self.assertEqual(opcode, 1)
        self.assertEqual(payload, b"hello")

    def test_accept_value_is_stable(self):
        # RFC 6455 sample: key dGhlIHNhbXBsZSBub25jZQ== -> s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
        self.assertEqual(cc.accept_value("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class Overlay(unittest.TestCase):
    def test_page_and_socket_stay_on_loopback(self):
        core = cc.ChatCore()
        core.messages.append(cc.ChatMessage("1", "twitch", "vettv", "u", "Vet", "hello", 1))

        async def go():
            with self.assertRaises(cc.ChatError):
                await core.overlay.start(host="0.0.0.0", port=0)
            url = await core.overlay.start(port=0)
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)
            port = int(url.rsplit(":", 1)[1].rstrip("/"))

            def fetch():
                import urllib.request
                with urllib.request.urlopen(url, timeout=2) as resp:
                    return resp.status, resp.read().decode()

            status, body = await asyncio.get_running_loop().run_in_executor(None, fetch)
            self.assertEqual(status, 200)
            self.assertIn("Deck Live chat", body)
            self.assertNotIn("oauth", body.lower())
            ws = await cc.WsConn.connect("127.0.0.1", port, "/ws")
            hello = json.loads(await asyncio.wait_for(ws.recv_text(), 2))
            self.assertEqual(hello["messages"][0]["text"], "hello")
            core._emit({"type": "message", "message": {"user_name": "Ace", "text": "!2"}})
            nxt = json.loads(await asyncio.wait_for(ws.recv_text(), 2))
            self.assertEqual(nxt["message"]["text"], "!2")
            q = next(iter(core.overlay.clients))
            for _ in range(q.maxsize):
                q.put_nowait({"type": "message", "message": {}})
            await core.overlay.stop()
            await ws.close()
            self.assertFalse(core.overlay.clients)
            self.assertFalse(core.overlay.connections)
            return True

        self.assertTrue(run(go()))

    def test_websocket_rejects_foreign_origins_and_host_authorities(self):
        core = cc.ChatCore()

        async def request(headers):
            url = await core.overlay.start(port=0)
            port = int(url.rsplit(":", 1)[1].rstrip("/"))
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            request_headers = {
                "Host": f"127.0.0.1:{port}",
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                "Sec-WebSocket-Version": "13",
                **headers,
            }
            writer.write(("GET /ws HTTP/1.1\r\n" + "".join(
                f"{name}: {value}\r\n" for name, value in request_headers.items()) + "\r\n").encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n"), 2)
            writer.close()
            await writer.wait_closed()
            return response

        async def go():
            foreign = await request({"Origin": "http://attacker.invalid"})
            wrong_host = await request({"Host": "127.0.0.1:1"})
            await core.overlay.stop()
            self.assertIn(b"403", foreign)
            self.assertIn(b"403", wrong_host)

        run(go())


class Redact(unittest.TestCase):
    def test_patterns(self):
        text = "oauth:abc123 Bearer ya29.zzz key=secret AIzaSySUPERSECRETKEY12"
        out = cc.redact(text)
        self.assertNotIn("abc123", out)
        self.assertNotIn("ya29.zzz", out)
        self.assertNotIn("AIzaSySUPERSECRETKEY12", out)
        self.assertNotIn("key=secret", out)


if __name__ == "__main__":
    unittest.main()
