"""Stream-key safety tests (no GPU or GStreamer needed). Run: python3 -m unittest discover -s tests"""
import asyncio, io, json, os, stat, sys, tempfile, types, unittest
from unittest import mock

sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=__import__("logging").getLogger("t"), DECKY_PLUGIN_SETTINGS_DIR=tempfile.mkdtemp(),
    DECKY_PLUGIN_RUNTIME_DIR=tempfile.mkdtemp(), DECKY_USER="box", DECKY_USER_HOME="/tmp",
    emit=None))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main as m  # noqa: E402
import gst_launch_worker as glw  # noqa: E402

KEY = "abcd-efgh/ijkl+mnop"
URL = "rtmps://a.rtmps.youtube.com:443/live2/" + KEY
HAVE = {"x264enc": True, "avenc_aac": True, "rtmp2sink": True}
ARGS = dict(have=HAVE, node=1, p=m.PRESETS["720p30"], mic=False, sink_el="rtmp2sink", location=URL)


def run(coro):
    return asyncio.run(coro)


class FailClosed(unittest.TestCase):
    def setUp(self):
        m._gi_ok = False
        m._secure_gst_ok = False

    def test_no_pygobject_uses_private_gst_launch_runner(self):
        async def no_gi(env):
            return False
        async def secure_runner(env):
            return True
        class P:
            class stdin:
                buf = b""
                @classmethod
                def write(cls, b): cls.buf += b
                @staticmethod
                async def drain(): pass
                @staticmethod
                def close(): pass
        async def fake_exec(*argv, **kw):
            fake_exec.argv = argv
            return P
        with mock.patch.object(m, "_gi_available", no_gi), \
                mock.patch.object(m, "_secure_gst_available", secure_runner), \
                mock.patch("asyncio.create_subprocess_exec", fake_exec), \
                mock.patch("asyncio.create_subprocess_shell") as sh:
            proc, mode, enc = run(m._spawn({}, dict(ARGS)))
            sh.assert_not_called()
        self.assertIs(proc, P)
        self.assertEqual(mode, "gst-launch")
        self.assertFalse(any(KEY in str(a) for a in fake_exec.argv))
        self.assertIn(KEY, json.loads(P.stdin.buf)["pipeline"])

    def test_network_output_refuses_if_process_privacy_is_unavailable(self):
        async def no_gi(env):
            return False
        async def no_secure_runner(env):
            return False
        args = dict(ARGS, location="rtmp://example.invalid/app")
        with mock.patch.object(m, "_gi_available", no_gi), \
                mock.patch.object(m, "_secure_gst_available", no_secure_runner), \
                mock.patch("asyncio.create_subprocess_shell") as sh, \
                mock.patch("asyncio.create_subprocess_exec") as ex:
            with self.assertRaises(RuntimeError):
                run(m._spawn({}, args))
            sh.assert_not_called()
            ex.assert_not_called()

    def test_diagnostics_filesink_may_still_use_gst_launch(self):
        async def no_gi(env):
            return False
        async def fake_shell(cmd, **kw):
            fake_shell.cmd = cmd
            return object()
        args = dict(ARGS, sink_el="filesink", location="/tmp/x.flv")
        with mock.patch.object(m, "_gi_available", no_gi), mock.patch("asyncio.create_subprocess_shell", fake_shell):
            proc, mode, enc = run(m._spawn({}, args))
        self.assertEqual(mode, "gst-launch")
        self.assertNotIn("rtmp", fake_shell.cmd)

    def test_worker_path_keeps_key_out_of_argv(self):
        class P:
            class stdin:
                buf = b""
                @classmethod
                def write(cls, b): cls.buf += b
                @staticmethod
                async def drain(): pass
                @staticmethod
                def close(): pass
        async def gi(env):
            return True
        async def fake_exec(*argv, **kw):
            fake_exec.argv = argv
            return P
        with mock.patch.object(m, "_gi_available", gi), mock.patch("asyncio.create_subprocess_exec", fake_exec):
            proc, mode, enc = run(m._spawn({}, dict(ARGS)))
        self.assertEqual(mode, "worker")
        self.assertFalse(any(KEY in str(a) for a in fake_exec.argv))
        self.assertIn(KEY, json.loads(P.stdin.buf)["pipeline"])  # delivered over stdin instead

    def test_gst_launch_runner_locks_process_access_before_exec(self):
        events = []
        cfg = {"pipeline": 'rtmp2sink location="' + URL + '"'}
        with mock.patch.object(glw, "_disable_proc_access", lambda: events.append("locked")), \
                mock.patch.object(glw, "sys") as worker_sys, \
                mock.patch.object(glw.shutil, "which", return_value="/usr/bin/gst-launch-1.0"), \
                mock.patch.object(glw.os, "execvpe", side_effect=lambda *args: events.append(("exec", args))):
            worker_sys.argv = ["gst_launch_worker.py"]
            worker_sys.stdin = io.StringIO(json.dumps(cfg))
            glw.main()
        self.assertEqual(events[0], "locked")
        self.assertEqual(events[1][0], "exec")
        self.assertIn(KEY, events[1][1][1][-1])

    def test_gst_launch_runner_refuses_exec_if_lock_fails(self):
        cfg = {"pipeline": 'rtmp2sink location="' + URL + '"'}
        with mock.patch.object(glw, "_disable_proc_access", side_effect=OSError("prctl failed")), \
                mock.patch.object(glw, "sys") as worker_sys, \
                mock.patch.object(glw.shutil, "which", return_value="/usr/bin/gst-launch-1.0"), \
                mock.patch.object(glw.os, "execvpe") as execvpe:
            worker_sys.argv = ["gst_launch_worker.py"]
            worker_sys.stdin = io.StringIO(json.dumps(cfg))
            with self.assertRaises(OSError):
                glw.main()
            execvpe.assert_not_called()

    def test_failed_gi_check_is_not_cached(self):
        calls = []
        async def fake_run(cmd, env):
            calls.append(cmd)
            return "" if len(calls) == 1 else "ok"
        with mock.patch.object(m, "_run", fake_run), mock.patch("os.path.exists", return_value=True):
            self.assertFalse(run(m._gi_available({})))
            self.assertTrue(run(m._gi_available({})))   # python3-gobject installed later
            self.assertTrue(run(m._gi_available({})))
        self.assertEqual(len(calls), 2)  # success is cached

    def test_start_stream_returns_user_facing_error(self):
        async def no_gi(env):
            return False
        async def have(env):
            return dict(HAVE)
        async def no_secure_runner(env):
            return False
        async def node(env):
            return 7
        async def audio(env):
            return "mon", None
        settings = {**m.DEFAULTS, "keys": {"youtube": KEY}, "preset": "720p30"}
        with mock.patch.object(m, "_gi_available", no_gi), \
                mock.patch.object(m, "_secure_gst_available", no_secure_runner), \
                mock.patch.object(m, "_available", have), \
                mock.patch.object(m, "_load", return_value=settings), \
                mock.patch.object(m, "_find_gamescope_node", node), mock.patch.object(m, "_resolve_audio", audio), \
                mock.patch("shutil.which", return_value="/usr/bin/gst-launch-1.0"), \
                mock.patch("asyncio.create_subprocess_shell") as sh, \
                mock.patch("asyncio.create_subprocess_exec") as ex, \
                self.assertLogs("t", "INFO") as logs:
            r = run(m.Plugin().start_stream())
            sh.assert_not_called()
            ex.assert_not_called()
        self.assertFalse(r["ok"])
        self.assertIn("Secure GStreamer runner", r["error"])
        self.assertFalse(any(KEY in line for line in logs.output))


class Redact(unittest.TestCase):
    def test_raw_and_percent_encoded_key_and_urls(self):
        text = (f"rtmp2sink: could not connect to {URL}\n"
                f"location={URL.replace(KEY, 'abcd-efgh%2Fijkl%2Bmnop')}\n"
                f"bare key {KEY} and quoted abcd-efgh/ijkl%2Bmnop and rtmp://live.twitch.tv/app/live_123")
        out = m._redact(text, KEY)
        for bad in (KEY, "abcd-efgh%2Fijkl%2Bmnop", "abcd-efgh/ijkl%2Bmnop", "live_123", "rtmp://", "rtmps://"):
            self.assertNotIn(bad, out)
        self.assertIn("<redacted", out)

    def test_no_key_still_strips_urls(self):
        self.assertEqual(m._redact("x rtmps://h/app/secret y"), "x <redacted-url> y")
        self.assertEqual(m._redact(""), "")
        self.assertIsNone(m._redact(None))

    def test_fail_redacts(self):
        with self.assertLogs("t", "ERROR") as logs:
            r = m.Plugin()._fail("boom rtmps://h/app/secret")
        self.assertNotIn("secret", r["error"]); self.assertNotIn("secret", "".join(logs.output))


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
                     "settings permission checks require POSIX")
class SettingsFile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "plug", "settings.json")
        self.p = mock.patch.object(m, "SETTINGS_PATH", self.path)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_created_0600_even_with_permissive_umask(self):
        old = os.umask(0)
        try:
            m._save({"keys": {"youtube": KEY}})
        finally:
            os.umask(old)
        self.assertEqual(self.mode(self.path), 0o600)
        self.assertEqual(self.mode(os.path.dirname(self.path)), 0o700)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertEqual(m._load()["keys"]["youtube"], KEY)

    def test_tmp_opened_0600_before_any_data_is_written(self):
        modes = []
        real_open = os.open
        def spy(path, flags, mode=0o777, **kw):
            fd = real_open(path, flags, mode, **kw)
            if path.endswith(".tmp"):
                modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return fd
        old = os.umask(0)
        try:
            with mock.patch("os.open", spy):
                m._save({"keys": {}})
        finally:
            os.umask(old)
        self.assertEqual(modes, [0o600])

    def test_fsync_called(self):
        with mock.patch("os.fsync", wraps=os.fsync) as fs:
            m._save({"keys": {}})
        self.assertGreaterEqual(fs.call_count, 1)

    def test_stale_world_readable_tmp_is_not_reused(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path + ".tmp", "w") as f:
            f.write("old")
        os.chmod(self.path + ".tmp", 0o644)
        m._save({"keys": {"youtube": KEY}})
        self.assertEqual(self.mode(self.path), 0o600)

    def test_load_tightens_old_0644_file(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"keys": {"youtube": KEY}}, f)
        os.chmod(self.path, 0o644)
        m._load()
        self.assertEqual(self.mode(self.path), 0o600)


if __name__ == "__main__":
    unittest.main()
