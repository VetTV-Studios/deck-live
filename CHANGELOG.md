# Changelog

## Unreleased (fix/stream-key-leak)
- Security: Go Live no longer falls back to `gst-launch-1.0` when PyGObject is missing. That fallback put the RTMP URL with the stream key on the command line, readable by any local process through `/proc/<pid>/cmdline`. Streaming now refuses to start with a clear message (install `python3-gobject` or update Bazzite/SteamOS). Diagnostics, which only writes a local file, can still use gst-launch.
- A failed PyGObject check is no longer cached, so installing `python3-gobject` works after a reload instead of a Decky restart.
- Logs and error messages redact the stream key (raw and percent-encoded) and any `rtmp://` / `rtmps://` URL.
- `settings.json` (stream keys) is now created with mode 0600 from the start, fsync'd, and its folder is 0700. A file left 0644 by an older build is tightened on load.

## Unreleased (feature/encoder-fallback)
- Encoder fallback chain: VA-API, then NVENC (nvh264enc/nvautogpuh264enc, CUDA upload and scale), then x264, then openh264. Each encoder is checked with a test encode before use.
- Encoder setting (Auto/VA-API/NVENC/Software). Auto prefers NVENC when the display runs on an NVIDIA GPU.
- The encoder in use is shown in the Health line and in Diagnostics (with probe results and the display GPU).
- Low-VRAM fixes (RTX 3050 4GB on bazzite-deck-nvidia): the probe runs only at Go Live, only for the encoders the setting allows, stops at the first success, uses the real resolution/fps/properties, is cached per encoder+size+fps, times out, and runs in its own process so VRAM is freed. Element checks no longer load nvcodec/VA plugins. A GPU encoder that fails while live is skipped for the session and the stream moves to the next encoder at once. GPUs with 4 GB or less default to 720p30 at 4500 kbps (user can override), and NVENC is skipped when under 512 MiB is free. Legacy (pre-1.22) `nvh264enc` gets compatible property names. Go Live ignores a second tap while starting.

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
