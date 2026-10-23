# Changelog

## 1.0.3 — 2026-10-23
- SteamOS compatibility: Go Live can now use systems that have GStreamer but not PyGObject, using a protected gst-launch runner that receives the pipeline over stdin and disables ptrace access before exec to keep the stream key out of readable process arguments.
- Streaming still fails closed if the operating system cannot protect the process arguments. Health statistics remain available with PyGObject; the gst-launch runner reports live/offline status without detailed health.

## 1.0.2 — 2026-10-18
- Security: Go Live no longer falls back to `gst-launch-1.0` when PyGObject is missing. That fallback put the RTMP URL with the stream key on the command line, readable by any local process through `/proc/<pid>/cmdline`. Streaming now refuses to start with a clear message (install `python3-gobject` or update Bazzite/SteamOS). Diagnostics, which only writes a local file, can still use gst-launch.
- A failed PyGObject check is no longer cached, so installing `python3-gobject` works after a reload instead of a Decky restart.
- Logs and error messages redact the stream key (raw and percent-encoded) and any `rtmp://` / `rtmps://` URL.
- `settings.json` (stream keys) is now created with mode 0600 from the start, fsync'd, and its folder is 0700. A file left 0644 by an older build is tightened on load.

## 1.0.1 — 2026-10-13
- New `install.sh`: one-line installer and updater (`curl -L …/install.sh | sh`) that needs no Decky developer mode. It downloads the latest release, swaps the plugin folder atomically, and restarts plugin_loader. It works with stock tools (curl, unzip or a python3 fallback, systemctl).
- New `uninstall.sh` (`PURGE=1` also removes settings and stream keys).
- Releases now also carry an unversioned `deck-live.zip` asset for a stable `latest/download` URL.
- No plugin code changes.

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
