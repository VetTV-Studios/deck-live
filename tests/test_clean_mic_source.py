"""Clean Mic source detection + mic picker tests (no GStreamer/PipeWire needed).
Run: python3 -m unittest discover -s tests -v"""
import asyncio, json, os, sys, tempfile, types, unittest
from unittest import mock

sys.modules.setdefault("decky", types.SimpleNamespace(
    logger=__import__("logging").getLogger("t"), DECKY_PLUGIN_SETTINGS_DIR=tempfile.mkdtemp(),
    DECKY_PLUGIN_RUNTIME_DIR=tempfile.mkdtemp(), DECKY_USER="box", DECKY_USER_HOME="/tmp",
    emit=None))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main as m  # noqa: E402


def node(nid, name, mclass="Audio/Source", desc=None):
    props = {"node.name": name, "media.class": mclass}
    if desc:
        props["node.description"] = desc
    return {"id": nid, "type": "PipeWire:Interface:Node", "info": {"props": props}}


DUMP = [
    node(40, "alsa_input.pci-0000_04_00.6.analog-stereo", desc="Internal Microphone"),
    node(41, "alsa_output.pci-0000_04_00.6.analog-stereo", "Audio/Sink", "Speakers"),
    node(52, "clean_mic", desc="Clean Mic (noise-cancelled)"),
    node(51, "clean_mic.capture", "Stream/Input/Audio"),
    node(60, "bluez_input.AA:BB:CC:DD:EE:FF.0", desc="Headset"),
    node(61, 'bad" name', desc="x"),
    {"id": 99, "type": "PipeWire:Interface:Metadata"},
]
HAVE = {"x264enc": True, "avenc_aac": True, "webrtcdsp": True, "audiocheblimit": True, "audiodynamic": True}


def run(coro):
    return asyncio.run(coro)


class Parse(unittest.TestCase):
    def test_lists_mics_clean_first(self):
        src = m._parse_mic_sources(DUMP)
        self.assertEqual([s["name"] for s in src],
                         ["clean_mic", "bluez_input.AA:BB:CC:DD:EE:FF.0", "alsa_input.pci-0000_04_00.6.analog-stereo"])
        self.assertTrue(src[0]["clean"])
        self.assertEqual(src[0]["desc"], m.CLEAN_MIC_LABEL)
        self.assertFalse(any(s["clean"] for s in src[1:]))

    def test_ignores_sinks_streams_and_unsafe_names(self):
        names = [s["name"] for s in m._parse_mic_sources(DUMP)]
        self.assertNotIn("clean_mic.capture", names)
        self.assertNotIn("alsa_output.pci-0000_04_00.6.analog-stereo", names)
        self.assertNotIn('bad" name', names)
        self.assertEqual(m._parse_mic_sources(None), [])


class Pick(unittest.TestCase):
    src = m._parse_mic_sources(DUMP)

    def test_clean_mic_selected_and_present(self):
        self.assertEqual(m._pick_mic("clean_mic", self.src, "alsa_input.x"), ("clean_mic", True, None))

    def test_clean_mic_selected_but_not_running_falls_back(self):
        others = [s for s in self.src if not s["clean"]]
        dev, clean, note = m._pick_mic("clean_mic", others, "alsa_input.x")
        self.assertEqual((dev, clean), ("alsa_input.x", False))
        self.assertIn("Clean Mic is not running", note)

    def test_system_default(self):
        self.assertEqual(m._pick_mic("", self.src, "alsa_input.x"), ("alsa_input.x", False, None))

    def test_system_default_is_clean_mic(self):
        self.assertEqual(m._pick_mic("", self.src, "clean_mic"), ("clean_mic", True, None))

    def test_specific_device(self):
        self.assertEqual(m._pick_mic("bluez_input.AA:BB:CC:DD:EE:FF.0", self.src, None)[:2],
                         ("bluez_input.AA:BB:CC:DD:EE:FF.0", False))

    def test_missing_device(self):
        dev, clean, note = m._pick_mic("usb_gone", self.src, None)
        self.assertEqual((dev, clean), (None, False))
        self.assertIn("not found", note)


class Pipeline(unittest.TestCase):
    def build(self, **kw):
        args = dict(have=HAVE, node=1, p=m.PRESETS["720p30"], mic=True, sink_el="filesink",
                    location="/tmp/x.flv", mic_dev="clean_mic")
        args.update(kw)
        pipe, _enc = m._build_pipeline(**args)
        return pipe

    def test_clean_mic_skips_webrtcdsp_and_gate(self):
        pipe = self.build(mic_clean=True)
        self.assertIn("device=clean_mic", pipe)
        self.assertNotIn("webrtcdsp", pipe)
        self.assertNotIn("audiodynamic", pipe)
        self.assertIn("audiocheblimit mode=high-pass", pipe)

    def test_raw_mic_keeps_webrtcdsp(self):
        pipe = self.build(mic_dev="alsa_input.x")
        self.assertIn("webrtcdsp", pipe)
        self.assertNotIn("audiocheblimit", pipe)

    def test_default_is_unchanged(self):
        # mic_clean defaults to False, so existing callers build exactly the old pipeline
        self.assertEqual(self.build(mic_dev="alsa_input.x"), self.build(mic_dev="alsa_input.x", mic_clean=False))


class Backend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = mock.patch.object(m, "SETTINGS_PATH", os.path.join(self.tmp, "settings.json"))
        self.p.start()
        self.addCleanup(self.p.stop)
        self.plugin = m.Plugin()

    def test_setting_persists_and_validates(self):
        self.assertEqual(run(self.plugin.set_mic_source("clean_mic")), {"ok": True, "mic_source": "clean_mic"})
        self.assertEqual(m._load()["mic_source"], "clean_mic")
        self.assertFalse(run(self.plugin.set_mic_source('x" ! filesrc location=/etc/shadow'))["ok"])
        self.assertEqual(m._load()["mic_source"], "clean_mic")
        self.assertTrue(run(self.plugin.set_mic_source(""))["ok"])
        self.assertEqual(m._load()["mic_source"], "")

    def test_old_settings_file_defaults_to_system_mic(self):
        with open(m.SETTINGS_PATH, "w") as f:
            json.dump({"platform": "youtube", "mic": True}, f)
        self.assertEqual(m._load()["mic_source"], "")

    def test_get_mic_sources(self):
        async def fake_run(cmd, env):
            if cmd == "pw-dump":
                return json.dumps(DUMP)
            if cmd.startswith("pactl get-default-sink"):
                return "alsa_output.pci-0000_04_00.6.analog-stereo"
            if cmd.startswith("pactl get-default-source"):
                return "alsa_input.pci-0000_04_00.6.analog-stereo"
            return ""
        with mock.patch.object(m, "_run", fake_run), mock.patch.object(m.shutil, "which", lambda x: "/usr/bin/" + x):
            r = run(self.plugin.get_mic_sources())
            self.assertTrue(r["clean_mic_present"])
            self.assertEqual(r["sources"][0]["name"], "clean_mic")
            self.assertEqual(r["default"], "alsa_input.pci-0000_04_00.6.analog-stereo")
            dev, clean, note = run(m._resolve_mic({}, {"mic_source": "clean_mic"}, r["default"]))
            self.assertEqual((dev, clean, note), ("clean_mic", True, None))
            # system default selected and it's a plain mic: no pw-dump needed, Deck Live filter stays on
            self.assertEqual(run(m._resolve_mic({}, {"mic_source": ""}, r["default"]))[1], False)


if __name__ == "__main__":
    unittest.main()
