"""Session, Go Live, and diagnostics file tests. No GPU. Run: python3 -m unittest discover -s tests"""
import asyncio, json, logging, os, stat, sys, tempfile, types, unittest
from unittest import mock

sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=__import__("logging").getLogger("t"), DECKY_PLUGIN_SETTINGS_DIR=tempfile.mkdtemp(),
    DECKY_PLUGIN_RUNTIME_DIR=tempfile.mkdtemp(), DECKY_USER="box", DECKY_USER_HOME="/tmp",
    emit=mock.AsyncMock()))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main as m  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = -sig

    def kill(self):
        self.signals.append(9)
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class WatchProc(FakeProc):
    """stdout.readline raises; stderr.read would hang if the watcher did not cancel it."""

    def __init__(self):
        super().__init__()
        self.stdout = self
        self.stderr = self
        self.read_cancelled = False

    async def readline(self):
        await asyncio.sleep(0)  # let the stderr reader park before the watcher crashes
        raise asyncio.LimitOverrunError("line too long", 100)

    async def read(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.read_cancelled = True
            raise


class Session(unittest.TestCase):
    def test_watcher_exception_stops_the_encoder(self):
        plugin = m.Plugin()
        plugin.stop_ev = asyncio.Event()
        plugin.stop_ev.set()
        proc = WatchProc()

        async def go():
            with mock.patch.object(plugin, "session_done"), \
                    mock.patch.object(plugin, "_emit_status", new=mock.AsyncMock()):
                await plugin._session(proc, "worker", {"key": "secret-key"})

        run(go())
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(proc.signals)
        self.assertTrue(proc.read_cancelled)
        self.assertIsNone(plugin.proc)

    def test_second_go_live_while_starting_is_rejected(self):
        m._save({**m.DEFAULTS, "keys": {"youtube": "secret-key"}})
        plugin = m.Plugin()
        started = asyncio.Event()
        release = asyncio.Event()

        async def avail(env):
            started.set()
            await release.wait()
            return {"rtmp2sink": True, "x264enc": True, "avenc_aac": True}

        async def node(env):
            return 1

        async def launch(ctx):
            return FakeProc(), "worker"

        async def idle(*a, **k):
            await asyncio.Event().wait()

        async def go():
            with mock.patch.object(m, "_available", avail), \
                    mock.patch.object(m, "_find_gamescope_node", node), \
                    mock.patch.object(m.shutil, "which", return_value="/usr/bin/gst-launch-1.0"), \
                    mock.patch.object(plugin, "_launch", launch), \
                    mock.patch.object(plugin, "_session", idle), \
                    mock.patch.object(plugin, "_emit_status", new=mock.AsyncMock()):
                first = asyncio.create_task(plugin.start_stream())
                await started.wait()
                second = await plugin.start_stream()
                release.set()
                result = await first
            if plugin.session is not None:
                plugin.session.cancel()
                try:
                    await plugin.session
                except asyncio.CancelledError:
                    pass
            return result, second

        result, second = run(go())
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(second.get("error"), "Already live")
        self.assertFalse(plugin.starting)

    def test_chat_state_does_not_include_tokens(self):
        plugin = m.Plugin()
        logs = []

        class H(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        handler = H()
        m.decky.logger.addHandler(handler)
        token = "oauth:super-secret-token"
        key = "AIzaSySUPERSECRETKEY12"
        try:
            self.assertTrue(run(plugin.chat_set_oauth(token, "vet")))
            self.assertTrue(run(plugin.chat_set_yt_key(key)))
            st = run(plugin.chat_get_state())
        finally:
            m.decky.logger.removeHandler(handler)
            if plugin._chat is not None:
                run(plugin._chat.close())
        blob = json.dumps(st) + "\n" + "\n".join(logs)
        self.assertNotIn("super-secret-token", blob)
        self.assertNotIn(key, blob)
        self.assertTrue(st["has_oauth"])
        self.assertTrue(st["has_yt_key"])
        self.assertTrue(st["has_login"])
        self.assertTrue(st["overlay_url"].startswith("http://127.0.0.1:"))
        with open(m.SETTINGS_PATH) as f:
            saved = f.read()
        self.assertIn(token, saved)
        self.assertEqual(stat.S_IMODE(os.stat(m.SETTINGS_PATH).st_mode), 0o600)


class DiagFile(unittest.TestCase):
    def test_symlink_is_replaced_and_the_target_is_untouched(self):
        d = tempfile.mkdtemp()
        target = os.path.join(d, "secret")
        link = os.path.join(d, "decky-live-test.flv")
        with open(target, "w") as f:
            f.write("keep-me")
        os.symlink(target, link)
        m._prepare_diag_file(link)
        self.assertFalse(os.path.islink(link))
        self.assertTrue(os.path.isfile(link))
        with open(target) as f:
            self.assertEqual(f.read(), "keep-me")
        self.assertEqual(stat.S_IMODE(os.stat(link).st_mode), 0o600)

    def test_dangling_symlink_is_not_followed(self):
        d = tempfile.mkdtemp()
        missing = os.path.join(d, "does-not-exist")
        link = os.path.join(d, "decky-live-test.flv")
        os.symlink(missing, link)
        m._prepare_diag_file(link)
        self.assertFalse(os.path.islink(link))
        self.assertFalse(os.path.exists(missing))
        self.assertTrue(os.path.isfile(link))


if __name__ == "__main__":
    unittest.main()
