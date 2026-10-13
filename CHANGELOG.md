# Changelog

## 1.0.0 — 2026-10-12
- First release: YouTube (RTMPS) and Twitch streaming from Game Mode.
- Hardware H.264 (vah264enc → vaapih264enc → openh264enc → x264enc fallback), GPU scaling with aspect preservation, constant-fps output.
- Game audio from the default sink monitor, an optional noise-suppressed mic, and volume/gain sliders.
- A/V sync offset (default −160 ms), implemented by delaying the earlier stream.
- Auto-reconnect with backoff (2/5/10/20/30 s, up to 5 min); Stop cancels it.
- Live health stats; 20 s Diagnostics with stall, frame-rate and real-time checks.
- PyGObject worker runner (keeps the stream key out of `ps`, clean EOS), with gst-launch fallback.
- Defaults for new installs: 1080p60, mic off, A/V −160 ms.
- Verified on Legion Go / Bazzite.
- Twitch per-preset bitrate caps (max 6000 kbps) with a strict CBR / 1 s CPB buffer; effective bitrate shown in the panel.

## 0.x (development)
- Scaffold based on decky-plugin-template; platform trimming; encoder/capture/audio/sync fixes driven by device testing.
