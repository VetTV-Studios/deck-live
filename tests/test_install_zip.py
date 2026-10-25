"""install.sh must reject a bad zip before it extracts anything."""
import os, subprocess, tempfile, unittest, zipfile

INSTALL = os.path.join(os.path.dirname(__file__), "..", "install.sh")
ROOT = "Deck Live"
NEEDED = {
    f"{ROOT}/plugin.json": "{}",
    f"{ROOT}/main.py": "# plugin",
    f"{ROOT}/dist/index.js": "export {}",
}


def zip_with(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


def check(path):
    return subprocess.run(["sh", INSTALL, "--check-zip", path], capture_output=True, text=True)


class ZipLayout(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.z = os.path.join(self.d, "plugin.zip")

    def test_good_layout_is_accepted_and_not_extracted(self):
        zip_with(self.z, NEEDED)
        r = check(self.z)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.d, ROOT)))

    def test_dotdot_member_is_rejected(self):
        members = dict(NEEDED)
        members["../evil.txt"] = "pwned"
        zip_with(self.z, members)
        r = check(self.z)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("escapes", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.d, "evil.txt")))

    def test_absolute_member_is_rejected(self):
        members = dict(NEEDED)
        members["/tmp/deck-live-escaped"] = "pwned"
        zip_with(self.z, members)
        r = check(self.z)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("escapes", r.stderr)

    def test_missing_frontend_bundle_is_rejected(self):
        members = dict(NEEDED)
        del members[f"{ROOT}/dist/index.js"]
        zip_with(self.z, members)
        r = check(self.z)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unexpected zip layout", r.stderr)
        self.assertIn("dist/index.js", r.stderr)


if __name__ == "__main__":
    unittest.main()
