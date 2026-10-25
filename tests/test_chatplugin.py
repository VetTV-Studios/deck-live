import asyncio
import importlib.util
import logging
import os
import sys
import types
import unittest
from unittest.mock import patch


def load_main():
    decky = types.ModuleType("decky")
    decky.DECKY_PLUGIN_SETTINGS_DIR = "/tmp/deck-live-test-settings"
    decky.DECKY_PLUGIN_RUNTIME_DIR = "/tmp/deck-live-test-runtime"
    decky.logger = logging.getLogger("chat-plugin-test")
    spec = importlib.util.spec_from_file_location(
        "deck_live_main_test", os.path.join(os.path.dirname(__file__), "..", "main.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"decky": decky}):
        spec.loader.exec_module(module)
    return module


class ChatPlugin(unittest.TestCase):
    def test_connect_uses_only_the_selected_provider_oauth(self):
        module = load_main()

        class Core:
            def __init__(self):
                self.calls = []

            async def ensure_overlay(self):
                return ""

            async def connect(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {"ok": True}

        async def go():
            core = Core()
            plugin = module.Plugin()
            plugin._chat = core
            settings = {"chat_platform": "youtube", "chat_channel": "CHATID",
                        "chat_yt_key": "google-key", "chat_oauth": "twitch-token",
                        "chat_login": "twitch-user", "chat_yt_oauth": "google-token"}
            with patch.object(module, "_load", return_value=settings), patch.object(module, "_save"):
                await plugin.chat_connect("youtube", "CHATID")
                await plugin.chat_connect("twitch", "vettv")
            yt_args, yt_kwargs = core.calls[0]
            twitch_args, twitch_kwargs = core.calls[1]
            self.assertEqual(yt_args[3:5], ("", ""))
            self.assertEqual(yt_kwargs["yt_oauth"], "google-token")
            self.assertEqual(twitch_args[3:5], ("twitch-token", "twitch-user"))
            self.assertEqual(twitch_kwargs["yt_oauth"], "")

        asyncio.run(go())

    def test_saving_twitch_credentials_reconnects_an_active_session(self):
        module = load_main()

        class Core:
            platform = "twitch"
            channel = "vettv"
            state = "live"
            _reconnect = True

            def __init__(self):
                self._task = asyncio.get_running_loop().create_future()
                self.calls = []

            async def connect(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {"ok": True}

        async def go():
            core = Core()
            plugin = module.Plugin()
            plugin._chat = core
            settings = {"chat_oauth": "old-token", "chat_login": "old-user",
                        "chat_yt_key": "", "chat_yt_oauth": ""}
            with patch.object(module, "_load", return_value=settings), patch.object(module, "_save"):
                await plugin.chat_set_oauth("new-token", "new-user")
            args, kwargs = core.calls[0]
            self.assertEqual(args[:5], ("twitch", "vettv", "", "new-token", "new-user"))
            self.assertTrue(kwargs["reconnect"])

        asyncio.run(go())

    def test_quota_usage_is_loaded_and_persisted_by_pacific_date(self):
        module = load_main()
        plugin = module.Plugin()
        settings = {"chat_yt_quota_date": "2000-01-01", "chat_yt_quota_used": 123}
        saved = []
        with patch.object(module, "_load", return_value=settings), patch.object(
                module, "_save", side_effect=lambda value: saved.append(value.copy())):
            core = plugin._chatcore()
        self.assertEqual(core.quota_used, 0)
        self.assertNotEqual(core.quota_date, "2000-01-01")
        self.assertEqual(saved[-1]["chat_yt_quota_date"], core.quota_date)
        self.assertEqual(saved[-1]["chat_yt_quota_used"], 0)


if __name__ == "__main__":
    unittest.main()
