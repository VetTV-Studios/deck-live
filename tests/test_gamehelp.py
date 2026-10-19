"""Backend tests for gamehelp.py. Run: python3 -m unittest discover -s tests
Set GH_LIVE=1 to also hit YouTube through yt-dlp (needs network + yt-dlp on PATH or YTDLP)."""
import asyncio, hashlib, io, json, logging, os, shutil, sys, tempfile, time, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gamehelp as gh

log = logging.getLogger("t")
VID = lambda i: f"vid{i:08d}"  # 11 chars


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.g = gh.GameHelp(self.d, log)

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_(self, c):
        return asyncio.run(c)

    def test_normalize(self):
        self.assertEqual(gh.normalize_game("DOOM Eternal™"), "DOOM Eternal")
        self.assertEqual(gh.normalize_game("Control Ultimate Edition"), "Control game")
        self.assertEqual(gh.normalize_game("Death Stranding Director's Cut Edition"), "Death Stranding")
        self.assertEqual(gh.normalize_game("Celeste"), "Celeste game")
        self.assertEqual(gh.build_query("Hades II", "achievement", "Nightmare"), "Hades II Nightmare achievement guide")
        self.assertEqual(gh.build_query("Hades II", "settings"), "Hades II Legion Go best settings")

    def test_iso_duration(self):
        self.assertEqual(gh._iso_dur("PT1H2M3S"), 3723)
        self.assertEqual(gh._iso_dur("PT45S"), 45)
        self.assertEqual(gh._iso_dur("P1DT1S"), 86401)

    def fake_api(self):
        calls = []
        def http(url):
            calls.append(url)
            if "/search?" in url:
                return {"items": [{"id": {"videoId": VID(i)}, "snippet": {"title": f"Hades II guide {i}",
                         "channelTitle": "C", "channelId": "UCx"}} for i in range(15)]}
            return {"items": [{"id": VID(i), "contentDetails": {"duration": "PT10M"},
                               "statistics": {"viewCount": "1234"}} for i in range(15)]}
        return calls, http

    def test_api_then_cache_one_search_per_week(self):
        calls, http = self.fake_api()
        with mock.patch.object(self.g, "_http_json", side_effect=http):
            r1 = self.run_(self.g.search("Hades II", "walkthrough", api_key="K"))
            r2 = self.run_(self.g.search("Hades II", "walkthrough", api_key="K"))
        self.assertEqual(r1["provider"], "api"); self.assertFalse(r1["cached"])
        self.assertTrue(r2["cached"]); self.assertEqual(len(r2["items"]), gh.SHOW)
        self.assertEqual(sum("/search?" in c for c in calls), 1)
        self.assertEqual(sum("/videos?" in c for c in calls), 1)
        self.assertEqual(r1["items"][0]["duration"], 600); self.assertEqual(r1["items"][0]["views"], 1234)
        self.assertEqual(self.g.quota_used(), 1)
        self.assertIn("videoEmbeddable=true", calls[0]); self.assertIn("type=video", calls[0])

    def test_quota_guard_falls_back(self):
        with self.g._db() as db:
            db.execute("INSERT OR REPLACE INTO quota VALUES (?,?)", (gh.pt_day(), gh.QUOTA_GUARD))
        fake = [{"id": VID(1), "title": "t", "channel": "c", "channel_id": "UC", "duration": 5, "views": 1}]
        with mock.patch.object(self.g, "_http_json") as http, \
             mock.patch.object(self.g, "_ytdlp_search", return_value=fake) as yd:
            r = self.run_(self.g.search("Hades II", "boss", api_key="K"))
        http.assert_not_called(); yd.assert_called_once()
        self.assertEqual(r["provider"], "yt-dlp"); self.assertIn("quota guard", r["note"])

    def test_quota_exceeded_from_google_marks_day(self):
        import urllib.error, io
        err = urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b'{"reason":"quotaExceeded"}'))
        with mock.patch("urllib.request.urlopen", side_effect=err), \
             mock.patch.object(self.g, "_ytdlp_search", return_value=[]):
            r = self.run_(self.g.search("Hades II", "boss", api_key="K"))
        self.assertEqual(r["provider"], "yt-dlp"); self.assertEqual(self.g.quota_used(), gh.QUOTA_GUARD)

    def test_offline_serves_stale_cache(self):
        self.g._cache_put(gh.build_query("Hades II", "walkthrough").lower(), "api",
                          [{"id": VID(1), "title": "old", "channel": "c"}])
        with self.g._db() as db:
            db.execute("UPDATE cache SET ts=?", (time.time() - gh.TTL_S - 10,))
        with mock.patch.object(self.g, "_ytdlp_search", side_effect=gh.OfflineError("no network")):
            r = self.run_(self.g.search("Hades II"))
        self.assertTrue(r["offline"]); self.assertEqual(r["items"][0]["title"], "old")

    def test_no_provider_error(self):
        with mock.patch.object(self.g, "ytdlp_path", return_value=None):
            r = self.run_(self.g.search("Hades II"))
        self.assertEqual(r["items"], []); self.assertIn("yt-dlp not installed", r["error"])

    def test_parallel_searches_spend_one_search_quota(self):
        calls, http = self.fake_api()

        def slow(url):
            time.sleep(0.05)
            return http(url)

        async def both():
            return await asyncio.gather(
                self.g.search("Hades II", "walkthrough", api_key="K"),
                self.g.search("Hades II", "walkthrough", api_key="K"),
            )

        with mock.patch.object(self.g, "_http_json", side_effect=slow):
            r1, r2 = self.run_(both())
        self.assertEqual(sum("/search?" in c for c in calls), 1)
        self.assertTrue(r1["cached"] or r2["cached"])
        self.assertEqual(self.g.quota_used(), 1)

    def test_system_ytdlp_is_preferred(self):
        with open(self.g.ytdlp_local, "wb") as f:
            f.write(b"not-the-real-binary")
        with mock.patch("shutil.which", return_value="/usr/bin/yt-dlp"):
            self.assertEqual(self.g.ytdlp_path(), "/usr/bin/yt-dlp")

    def test_unverified_download_is_not_used_or_executed(self):
        with open(self.g.ytdlp_local, "wb") as f:
            f.write(b"#!/bin/sh\necho pwned\n")
        os.chmod(self.g.ytdlp_local, 0o755)
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("asyncio.create_subprocess_exec") as exe:
            self.assertIsNone(self.g.ytdlp_path())
            r = self.run_(self.g.search("Hades II"))
        exe.assert_not_called()
        self.assertIn("yt-dlp not installed", r["error"])
        self.assertTrue(os.path.exists(self.g.ytdlp_local))

    def test_verified_local_copy_is_used_when_nothing_is_on_path(self):
        blob = b"verified-zipapp"
        with mock.patch.object(gh, "YTDLP_SHA256", hashlib.sha256(blob).hexdigest()), \
                mock.patch("shutil.which", return_value=None):
            with open(self.g.ytdlp_local, "wb") as f:
                f.write(blob)
            self.assertEqual(self.g.ytdlp_path(), self.g.ytdlp_local)

    def test_install_deletes_a_bad_checksum_and_does_not_keep_it(self):
        class Body:
            def __init__(self, data):
                self._b = io.BytesIO(data)
            def read(self, n=-1):
                return self._b.read(n)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Body(b"malware")):
            with self.assertRaises(gh.ChecksumError):
                self.run_(self.g.install_ytdlp())
        self.assertFalse(os.path.exists(self.g.ytdlp_local))
        self.assertFalse(os.path.exists(self.g.ytdlp_local + ".part"))

    def test_install_keeps_a_matching_checksum(self):
        blob = b"#!/usr/bin/env python3\nprint('yt-dlp')\n"
        digest = hashlib.sha256(blob).hexdigest()

        class Body:
            def __init__(self, data):
                self._b = io.BytesIO(data)
            def read(self, n=-1):
                return self._b.read(n)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch.object(gh, "YTDLP_SHA256", digest), \
                mock.patch("urllib.request.urlopen", return_value=Body(blob)), \
                mock.patch("shutil.which", return_value=None):
            path = self.run_(self.g.install_ytdlp())
            self.assertEqual(path, self.g.ytdlp_local)
            self.assertEqual(self.g.ytdlp_path(), self.g.ytdlp_local)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)

    def test_pins_first_and_cached(self):
        main_items = [{"id": VID(i), "title": f"x{i}", "channel": "c"} for i in range(10)]
        pins = [{"id": VID(99), "title": "Hades II on Legion Go", "channel": "VetTV"},
                {"id": VID(98), "title": "Unrelated vlog", "channel": "VetTV"}]
        with mock.patch.object(self.g, "_ytdlp_search", side_effect=[main_items, pins]) as yd:
            r = self.run_(self.g.search("Hades II", pin_channel="UCvet"))
            r2 = self.run_(self.g.search("Hades II", pin_channel="UCvet"))
        self.assertEqual(yd.call_count, 2)  # second call fully cached
        self.assertEqual(r["items"][0]["id"], VID(99)); self.assertTrue(r["items"][0]["pinned"])
        self.assertNotIn(VID(98), [i["id"] for i in r["items"]])
        self.assertEqual(r2["items"][0]["id"], VID(99))

    @unittest.skipUnless(os.environ.get("GH_LIVE"), "live network test")
    def test_live_ytdlp(self):
        system = os.environ.get("YTDLP") or shutil.which("yt-dlp")
        self.assertTrue(system, "yt-dlp on PATH or YTDLP is required")
        t0 = time.time()
        with mock.patch("shutil.which", return_value=system):
            r = self.run_(self.g.search("Hollow Knight", "boss"))
        dt = time.time() - t0
        print(f"\nlive yt-dlp: {len(r['items'])} items in {dt:.1f}s; first: {r['items'][:1]}")
        self.assertEqual(r["provider"], "yt-dlp"); self.assertGreaterEqual(len(r["items"]), 5)
        t0 = time.time()
        with mock.patch("shutil.which", return_value=system):
            r2 = self.run_(self.g.search("Hollow Knight", "boss"))
        print(f"cached in {time.time()-t0:.3f}s")
        self.assertTrue(r2["cached"])
        with mock.patch("shutil.which", return_value=system):
            r3 = self.run_(self.g.search("Hollow Knight", "walkthrough", pin_channel="UCZU03wNQEGmDkGcNN-BT2Ng"))
        print("pinned:", [i["title"] for i in r3["items"] if i.get("pinned")])


if __name__ == "__main__":
    unittest.main()
