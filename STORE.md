# Decky Plugin Store submission

Deck Live is ready to submit to the [Decky Plugin Database](https://github.com/SteamDeckHomebrew/decky-plugin-database). Follow the [official submitting guide](https://wiki.deckbrew.xyz/en/plugin-dev/submitting-plugins).

## Checklist (self-review)

- [x] Public repository
- [x] `LICENSE` (BSD-3-Clause + template copyright)
- [x] `plugin.json` with `api_version: 1` and `publish` (`tags`, `description`, **PNG** `image`)
- [x] `package.json` version bumped (`2.0.0`); `pnpm-lock.yaml` lockfileVersion `9.0`
- [x] No unused `backend/`, `defaults/`, or empty assets; `assets/icon.png` is the store image
- [x] No root flag; Python Decky backend + React frontend only
- [x] No LLM features
- [x] No black-box binaries shipped in the zip. Optional Game Help fallback downloads the **official** yt-dlp zipapp and keeps it only after SHA256 verification; system `yt-dlp` on `PATH` is preferred
- [x] CI: frontend build, Python unit tests, shellcheck (`install.sh`, `uninstall.sh`, `package.sh`)
- [ ] Hardware testing on Steam Deck SteamOS (Stable or Beta) — required for store approval (category B: Python backend)

## Submit / update

1. Tag and publish a GitHub release (`v2.0.0`) with `deck-live.zip` from `./package.sh`.
2. Fork `SteamDeckHomebrew/decky-plugin-database`.
3. Add submodule: `git submodule add https://github.com/VetTV-Studios/deck-live.git "plugins/Deck Live"` (or update the existing submodule to the release commit).
4. Open a PR against the plugin database. Use the PR template; answer the custom-backend checklist (**No** custom backend / **No** statically linked binary).
5. Arrange Steam Deck SteamOS testing for category B plugins.

## Build the store zip

```sh
pnpm install --frozen-lockfile
pnpm build
./package.sh   # writes ../deck-live.zip
```

Zip layout: `Deck Live/{dist/index.js, main.py, gst_worker.py, gst_launch_worker.py, gamehelp.py, plugin.json, package.json, README.md, LICENSE, assets/icon.png}`.

## Reviewer notes

- Stream keys and optional YouTube API keys stay in `settings.json` mode `0600`; never logged; never on argv for live RTMP (stdin to worker / protected gst-launch runner).
- Game Help calls `youtube.googleapis.com` and may download yt-dlp from GitHub releases when the user opts in. Network work is off the streaming event loop.
- Chat / Crowd features are **not** in 2.0.0 (see draft `feature/chat-core` for a future major).
