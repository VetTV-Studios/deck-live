"""Encoder selection / pipeline tests (no GPU needed). Run: python3 -m unittest discover -s tests"""
import json, os, re, subprocess, sys, tempfile, types, unittest

sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=__import__("logging").getLogger("t"), DECKY_PLUGIN_SETTINGS_DIR=tempfile.mkdtemp(),
    DECKY_PLUGIN_RUNTIME_DIR=tempfile.mkdtemp(), DECKY_USER="box", DECKY_USER_HOME="/tmp"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main as m  # noqa: E402

ALL = {e: True for e in m.ELEMENTS}
P = m.PRESETS["1080p60"]
WORKER = os.path.join(os.path.dirname(__file__), "..", "gst_worker.py")


def build(encoder, have=ALL, **kw):
    return m._build_pipeline(have, 42, P, False, "filesink", "/tmp/x.flv", "mon", None, shell=False,
                             encoder=encoder, **kw)[0]


class Order(unittest.TestCase):
    def test_orders(self):
        self.assertEqual(m.encoder_order("auto", "intel")[:2], ["vah264enc", "vaapih264enc"])
        self.assertEqual(m.encoder_order("auto", None)[0], "vah264enc")
        self.assertEqual(m.encoder_order("auto", "nvidia")[:2], ["nvh264enc", "nvautogpuh264enc"])
        self.assertEqual(m.encoder_order("nvenc", "intel"), ["nvh264enc", "nvautogpuh264enc"])
        self.assertEqual(m.encoder_order("vaapi", "nvidia"), ["vah264enc", "vaapih264enc"])
        self.assertEqual(m.encoder_order("software", "nvidia"), ["x264enc", "openh264enc"])

    def test_pick_fallbacks(self):
        w = {"vah264enc": False, "nvh264enc": True, "x264enc": True}
        self.assertEqual(m.pick_encoder("auto", "intel", w), "nvh264enc")      # VA broken -> NVENC
        with self.assertRaises(m.NoEncoderError):                              # forced VA: only VA is tried
            m.pick_encoder("vaapi", "intel", w)
        self.assertEqual(m.pick_encoder("software", "nvidia", w), "x264enc")
        self.assertEqual(m.pick_encoder("auto", "nvidia", {"nvautogpuh264enc": True, "vah264enc": True}),
                         "nvautogpuh264enc")
        self.assertEqual(m.pick_encoder("auto", None, {"openh264enc": True}), "openh264enc")
        with self.assertRaises(RuntimeError):
            m.pick_encoder("software", None, {"vah264enc": True})
        with self.assertRaises(RuntimeError):
            m.pick_encoder("auto", None, {})


class Gpu(unittest.TestCase):
    @staticmethod
    def w(path, data, mode="w"):
        with open(path, mode) as f:
            f.write(data)

    def fake(self, cards, connected, cmd=None):
        d, p = tempfile.mkdtemp(), tempfile.mkdtemp()
        for c, vendor in cards.items():
            os.makedirs(f"{d}/{c}/device"); self.w(f"{d}/{c}/device/vendor", vendor + "\n")
        for conn, st in connected.items():
            os.makedirs(f"{d}/{conn}"); self.w(f"{d}/{conn}/status", st + "\n")
        if cmd:
            os.makedirs(f"{p}/1234"); self.w(f"{p}/1234/cmdline", "\0".join(cmd).encode(), "wb")
        return m.gpu_info(d, p)

    def test_hybrid_laptop_internal_on_intel(self):
        g = self.fake({"card0": "0x8086", "card1": "0x10de"},
                      {"card0-eDP-1": "connected", "card1-HDMI-A-1": "disconnected"})
        self.assertEqual(g["display_gpu"], "intel"); self.assertEqual(g["gpus"]["card1"], "nvidia")

    def test_external_on_nvidia(self):
        g = self.fake({"card0": "0x8086", "card1": "0x10de"},
                      {"card0-eDP-1": "disconnected", "card1-HDMI-A-1": "connected"})
        self.assertEqual(g["display_gpu"], "nvidia")

    def test_gamescope_prefer_vk_device_wins(self):
        g = self.fake({"card0": "0x8086", "card1": "0x10de"}, {"card0-eDP-1": "connected"},
                      ["/usr/bin/gamescope", "-e", "--prefer-vk-device", "10de:25a2", "--", "steam"])
        self.assertEqual(g["display_gpu"], "nvidia"); self.assertIn("prefer-vk-device", g["source"])

    @unittest.skipUnless(os.environ.get("DECK_LIVE_DEVICE_TESTS"), "opens real encoders; set DECK_LIVE_DEVICE_TESTS=1")
    def test_probe_and_choose_on_box(self):
        import asyncio
        env = m._user_env()
        have = asyncio.run(m._available(env))
        enc, g = asyncio.run(m.Plugin()._choose_encoder(env, have, "auto", m.PRESETS["720p30"]))
        print("\nbox probe:", m.probe_snapshot(), "->", enc, g["display_gpu"])
        self.assertTrue(m.probe_snapshot(m.PRESETS["720p30"])[f"{enc} 1280x720@30"]["works"])

    def test_real_box_does_not_crash(self):
        self.assertIn("display_gpu", m.gpu_info())


class Pipelines(unittest.TestCase):
    def test_nvenc_cuda_path(self):
        s = build("nvh264enc")
        for frag in ["cudaupload ! cudaconvertscale add-borders=true",
                     "video/x-raw(memory:CUDAMemory),width=1920,height=1080,format=NV12",
                     "nvh264enc name=venc rc-mode=cbr bitrate=9000 vbv-buffer-size=9000 gop-size=120 bframes=0",
                     "preset=p4 tune=low-latency zerolatency=true repeat-sequence-header=true",
                     "video/x-h264,profile=high"]:
            self.assertIn(frag, s)

    def test_nvenc_system_memory_path(self):
        have = dict(ALL, cudaupload=False)
        s = build("nvautogpuh264enc", have)
        self.assertNotIn("cuda", s.split("nvautogpuh264enc")[0])
        self.assertIn("videoscale add-borders=true ! video/x-raw,width=1920,height=1080,format=NV12", s)

    def test_vaapi_unchanged(self):
        s = build("vah264enc")
        self.assertIn("vah264enc name=venc target-usage=7 rate-control=cbr bitrate=9000 cpb-size=9000 key-int-max=120 b-frames=0", s)
        self.assertIn("vapostproc add-borders=true", s)

    def test_legacy_default_when_no_encoder_given(self):
        s = m._build_pipeline({"x264enc": True, "avenc_aac": True}, 1, P, False, "filesink", "/tmp/x", shell=False)[0]
        self.assertIn("x264enc name=venc", s)

    def _run_worker(self, pipe):
        pipe = pipe.replace("pipewiresrc name=vsrc path=42 do-timestamp=true keepalive-time=100 always-copy=true",
                            "videotestsrc name=vsrc is-live=true ! video/x-raw,width=2560,height=1600,framerate=30/1")
        pipe = re.sub(r'pulsesrc do-timestamp=true device="mon" ', "audiotestsrc is-live=true ", pipe)
        r = subprocess.run(["/usr/bin/python3", WORKER], input=json.dumps({"pipeline": pipe, "duration": 3}),
                           capture_output=True, text=True, timeout=40)
        lines = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        return r.returncode, lines

    @unittest.skipUnless(os.path.exists("/usr/bin/python3"), "needs system python")
    def test_software_end_to_end(self):
        rc, lines = self._run_worker(build("x264enc"))
        stats = [l["stats"] for l in lines if "stats" in l]
        self.assertEqual(rc, 0, lines); self.assertTrue(any("eos" in l for l in lines))
        self.assertGreater(stats[-1]["vframes"], 100)

    @unittest.skipUnless(os.path.exists("/usr/bin/python3"), "needs system python")
    def test_nvenc_system_path_graph_runs_with_stand_in_encoder(self):
        """Everything around nvh264enc (CPU convert/scale/videorate/caps/mux) runs; encoder swapped for x264."""
        s = build("nvh264enc", dict(ALL, cudaupload=False))
        s = re.sub(r"nvh264enc name=venc [^!]*", "x264enc name=venc tune=zerolatency bitrate=9000 key-int-max=120 bframes=0 ", s)
        s = s.replace("video/x-h264,profile=high", "video/x-h264")
        rc, lines = self._run_worker(s)
        self.assertEqual(rc, 0, lines)

    @unittest.skipUnless(os.path.exists("/usr/bin/python3"), "needs system python + gi")
    def test_worker_probe(self):
        def probe(pipe):
            out = subprocess.run(["/usr/bin/python3", WORKER, "--probe"], input=json.dumps({"pipeline": pipe, "timeout": 8}),
                                 capture_output=True, text=True, timeout=60).stdout.strip().splitlines()[-1]
            return json.loads(out)
        p = m.PRESETS["1080p60"]
        r = probe(m._probe_pipeline({"x264enc": True}, "x264enc", p))
        self.assertTrue(r["works"], r)
        self.assertFalse(probe("videotestsrc num-buffers=5 ! bogusenc ! fakesink")["works"])
        self.assertFalse(probe(m._probe_pipeline(ALL, "nvh264enc", p))["works"])  # no NVIDIA here

if __name__ == "__main__":
    unittest.main()
