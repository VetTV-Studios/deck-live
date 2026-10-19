"""Low-VRAM / lazy-probe tests for the RTX 3050 4GB case (no GPU needed).
Run: python3 -m unittest discover -s tests"""
import asyncio, os, sys, tempfile, time, types, unittest
from unittest import mock

sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=__import__("logging").getLogger("t"), DECKY_PLUGIN_SETTINGS_DIR=tempfile.mkdtemp(),
    DECKY_PLUGIN_RUNTIME_DIR=tempfile.mkdtemp(), DECKY_USER="box", DECKY_USER_HOME="/tmp"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main as m  # noqa: E402

ALL = {e: True for e in m.ELEMENTS}
P1080 = dict(m.PRESETS["1080p60"])
P720 = dict(m.PRESETS["720p30"])
NV_GPU = {"gpus": {"card0": "nvidia"}, "display_gpu": "nvidia", "nvidia": True, "source": "test"}


def run(c):
    return asyncio.run(c)


class Harness(unittest.TestCase):
    """Records every encoder probe; `works` decides each encoder's result."""
    works = {}
    free = 3000
    total = 4096

    def setUp(self):
        m._probe_cache.clear()
        m._vram_total = None
        self.probed = []

        async def fake_probe_worker(env, pipe):
            enc = next(e for e in m.VAAPI_ENCS + m.NVENC_ENCS + m.SW_ENCS if f"{e} name=venc" in pipe)
            self.probed.append((enc, pipe))
            ok = self.works.get(enc, False)
            return {"works": ok, "error": "" if ok else "venc: CUDA_ERROR_OUT_OF_MEMORY"}

        async def fake_vram(env=None):
            m._vram_total = self.total
            return self.free, self.total

        async def gi(env):
            return True
        self.patches = [mock.patch.object(m, "_probe_worker", fake_probe_worker),
                        mock.patch.object(m, "_vram_mib", fake_vram),
                        mock.patch.object(m, "_gi_available", gi),
                        mock.patch.object(m, "gpu_info", lambda *a: dict(NV_GPU))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def names(self):
        return [e for e, _ in self.probed]

    def choose(self, pref, p=P1080, have=ALL, **kw):
        return run(m.Plugin()._choose_encoder({}, have, pref, p, **kw))[0]


class LazyProbe(Harness):
    works = {"nvh264enc": True, "nvautogpuh264enc": True, "vah264enc": True, "x264enc": True}

    def test_software_never_opens_gpu(self):
        self.assertEqual(self.choose("software"), "x264enc")
        self.assertEqual(self.names(), ["x264enc"])

    def test_vaapi_probes_only_vaapi(self):
        self.assertEqual(self.choose("vaapi"), "vah264enc")
        self.assertEqual(self.names(), ["vah264enc"])

    def test_nvenc_probes_only_nvenc(self):
        self.assertEqual(self.choose("nvenc"), "nvh264enc")
        self.assertEqual(self.names(), ["nvh264enc"])

    def test_auto_stops_at_first_success(self):
        self.assertEqual(self.choose("auto"), "nvh264enc")
        self.assertEqual(self.names(), ["nvh264enc"])

    def test_cached_per_resolution_and_fps(self):
        self.choose("auto", P720)
        self.choose("auto", P720)
        self.assertEqual(len(self.probed), 1)          # same size/fps: cache hit, no new GPU session
        self.choose("auto", P1080)
        self.assertEqual(len(self.probed), 2)          # 720p30 pass is not trusted for 1080p60
        self.choose("auto", P1080, force_probe=True)   # Diagnostics re-probes only the needed one
        self.assertEqual(self.names(), ["nvh264enc"] * 3)

    def test_probe_runs_at_real_size_with_real_encoder_string(self):
        self.choose("nvenc", P1080)
        pipe = self.probed[0][1]
        self.assertIn("width=1920,height=1080,framerate=60/1", pipe)
        _, conv, venc, _ = m._video_chain(ALL, P1080, "nvh264enc", shell=False)
        real = m._build_pipeline(ALL, 42, P1080, False, "rtmp2sink", "rtmp://x/app/k", shell=False,
                                 encoder="nvh264enc")[0]
        self.assertIn(venc, pipe); self.assertIn(venc, real)
        self.assertIn(conv, pipe); self.assertIn(conv, real)
        self.assertIn("cudaupload ! cudaconvertscale", pipe)

    def test_get_encoders_and_state_never_probe(self):
        pl = m.Plugin()
        run(pl.get_encoders())
        run(pl.get_state())
        run(pl._main())
        self.assertEqual(self.probed, [])


class AutoChain(Harness):
    works = {"x264enc": True}

    def test_auto_walks_chain_in_order_until_success(self):
        have = dict(ALL, vah264enc=False, vaapih264enc=False)
        self.assertEqual(self.choose("auto", have=have), "x264enc")
        self.assertEqual(self.names(), ["nvh264enc", "nvautogpuh264enc", "x264enc"])

    def test_explicit_nvenc_failure_is_a_clear_error_not_silent_software(self):
        with self.assertRaises(m.NoEncoderError) as cm:
            self.choose("nvenc")
        self.assertIn("NVENC", str(cm.exception))
        self.assertNotIn("x264enc", self.names())

    def test_low_free_vram_skips_nvenc_without_opening_it(self):
        self.free = 300
        self.assertEqual(self.choose("auto", have=dict(ALL, vah264enc=False, vaapih264enc=False)), "x264enc")
        self.assertEqual(self.names(), ["x264enc"])
        self.free = 3000
        self.choose("auto", have=dict(ALL, vah264enc=False, vaapih264enc=False))
        self.assertIn("nvh264enc", self.names())        # transient skip was not cached


class NvencNicks(unittest.TestCase):
    def test_new_element_uses_p4_and_tune(self):
        v = m._video_chain(dict(ALL, nvenc_new=True), P1080, "nvh264enc", False)[2]
        self.assertIn("preset=p4 tune=low-latency", v)

    def test_legacy_element_uses_legacy_nicks_in_probe_and_stream(self):
        have = dict(ALL, nvenc_new=False)
        v = m._video_chain(have, P1080, "nvh264enc", False)[2]
        self.assertIn("preset=low-latency-hq", v)
        self.assertNotIn("p4", v); self.assertNotIn("tune=", v)
        self.assertIn(v, m._probe_pipeline(have, "nvh264enc", P1080))
        self.assertNotEqual(m._probe_key(have, "nvh264enc", P1080), m._probe_key(ALL, "nvh264enc", P1080))

    def test_available_uses_registry_lookup_not_plugin_load(self):
        calls = []

        class P:
            returncode = 0
            async def wait(self):
                return 0

        async def fake_exec(*argv, **kw):
            calls.append(argv)
            return P()
        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            have = run(m._available({}))
        self.assertTrue(all(a[:2] == ("gst-inspect-1.0", "--exists") for a in calls))
        self.assertIn(("gst-inspect-1.0", "--exists", "--atleast-version=1.22", "nvh264enc"), calls)
        self.assertTrue(have["nvenc_new"])


class RuntimeFallback(Harness):
    works = {"nvh264enc": True, "x264enc": True}

    def test_fault_classifier(self):
        f = m.is_encoder_fault
        self.assertTrue(f("nvh264enc", "venc: Failed to open encoder session", 300))
        self.assertTrue(f("nvh264enc", "cudaconvertscale0: CUDA_ERROR_OUT_OF_MEMORY", 300))
        self.assertTrue(f("vah264enc", "Pipeline exited (1)", 3, {"mode": "worker"}))  # died before a frame
        self.assertFalse(f("nvh264enc", "out: Could not connect to server", 3))       # network
        self.assertFalse(f("nvh264enc", "Pipeline exited (1)", 600, {"vframes": 9000}))
        self.assertFalse(f("x264enc", "venc: whatever", 3))                           # software: normal reconnect

    def test_fallback_order(self):
        self.assertEqual(m.fallback_order("auto", "nvidia", {"nvh264enc", "nvautogpuh264enc"})[:2],
                         ["vah264enc", "vaapih264enc"])
        self.assertEqual(m.fallback_order("nvenc", "nvidia", {"nvh264enc", "nvautogpuh264enc"})[-2:],
                         ["x264enc", "openh264enc"])
        self.assertEqual(m.fallback_order("software", "nvidia", {"x264enc"}), ["openh264enc"])

    def test_live_nvenc_failure_switches_encoder_immediately(self):
        pl = m.Plugin()
        pl.stop_ev = asyncio.Event()
        spawned = []

        async def fake_spawn(env, args):
            spawned.append(args["encoder"])
            return object(), "worker", args["encoder"]
        watch_results = [(1, "venc: CUDA_ERROR_OUT_OF_MEMORY")]

        async def fake_watch(proc, key, mode):
            if watch_results:
                return watch_results.pop(0)
            pl.stopping = True
            return 0, ""

        async def node(env):
            return 1

        async def audio(env):
            return "mon", None

        async def emit(*a, **k):
            pass
        s = dict(m.DEFAULTS, encoder="auto")
        ctx = {"env": {}, "have": dict(ALL, vah264enc=False, vaapih264enc=False), "s": s, "p": P720,
               "location": "rtmp://x/app/KEY", "base": "rtmp://x/app", "key": "KEY"}
        with mock.patch.object(m, "_spawn", fake_spawn), mock.patch.object(m, "_find_gamescope_node", node), \
                mock.patch.object(m, "_resolve_audio", audio), mock.patch.object(pl, "_watch", fake_watch), \
                mock.patch.object(m.decky, "emit", emit, create=True), \
                mock.patch.object(pl, "session_done", lambda: None):
            async def go():
                proc, mode = await pl._launch(ctx)
                t = time.time()
                await pl._session(proc, mode, ctx)
                return time.time() - t
            took = run(go())
        self.assertEqual(spawned, ["nvh264enc", "x264enc"])
        self.assertEqual(set(pl.bad_encoders), {"nvh264enc", "nvautogpuh264enc"})  # whole NVENC family
        self.assertLess(took, 1.5)  # no reconnect backoff onto the same failing encoder
        self.assertEqual(self.names(), ["nvh264enc", "x264enc"])


class LowVramDefaults(unittest.TestCase):
    def test_parse_nvidia_smi(self):
        async def fake_run(cmd, env, timeout=None):
            return "3712, 4096\n"
        with mock.patch.object(m, "_run", fake_run), mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
            self.assertEqual(run(m._vram_mib({})), (3712, 4096))
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(run(m._vram_mib({})), (None, None))

    def test_effective_preset(self):
        s = dict(m.DEFAULTS)
        self.assertEqual(m.effective_preset(s, 4096), "720p30")
        self.assertEqual(m.effective_preset(s, 3800), "720p30")
        self.assertEqual(m.effective_preset(s, 8192), "1080p60")
        self.assertEqual(m.effective_preset(s, None), "1080p60")       # no NVIDIA / no nvidia-smi
        self.assertEqual(m.effective_preset(dict(s, preset="1080p60", preset_set=True), 4096), "1080p60")
        p = m.PRESETS["720p30"]
        self.assertEqual((p["w"], p["h"], p["fps"], p["kbps"]), (1280, 720, 30, 4500))

    def test_user_override_and_state(self):
        d = tempfile.mkdtemp()
        async def vram(env=None):
            return 4096
        with mock.patch.object(m, "SETTINGS_PATH", os.path.join(d, "s.json")), \
                mock.patch.object(m, "_vram_total_mib", vram):
            pl = m.Plugin()
            st = run(pl.get_state())
            self.assertEqual(st["preset"], "720p30"); self.assertTrue(st["light_defaults"])
            # UI echoes the shown preset back on an unrelated change: still the light default
            run(pl.save_settings("youtube", "720p30", True))
            self.assertTrue(run(pl.get_state())["light_defaults"])
            # explicit pick wins
            run(pl.save_settings("youtube", "1080p60", True))
            st = run(pl.get_state())
            self.assertEqual(st["preset"], "1080p60"); self.assertFalse(st["light_defaults"])


class WorkerProbeProcess(unittest.TestCase):
    @unittest.skipUnless(os.path.exists("/usr/bin/python3") and __import__("shutil").which("gst-launch-1.0"),
                         "needs system GStreamer")
    def test_hung_probe_is_killed(self):
        with mock.patch.object(m, "PROBE_TIMEOUT_S", 1):
            t = time.time()
            r = run(m._probe_worker(m._user_env(), "videotestsrc is-live=true ! fakesink"))
        self.assertFalse(r["works"]); self.assertLess(time.time() - t, 8)


if __name__ == "__main__":
    unittest.main()
