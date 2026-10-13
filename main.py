import asyncio
import json
import re
import os
import shlex
import shutil
import time

import decky

SETTINGS_PATH = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")

# Ingest endpoints. Stream key is appended at start time.
# "fallback": alternate plain-RTMP URL (not used automatically in v1; rtmp2sink handles RTMPS).
# To re-add platforms later, add entries here and list their id in ENABLED_PLATFORMS.
# Previously drafted: kick (rtmps, AWS IVS), facebook (rtmps://live-api-s.facebook.com:443/rtmp),
# rumble/custom (user-supplied URL, "url": "").
PLATFORMS = {
    "youtube": {"label": "YouTube",
                "url": "rtmps://a.rtmps.youtube.com:443/live2",
                "fallback": "rtmp://a.rtmp.youtube.com/live2"},
    "twitch": {"label": "Twitch",
               # Twitch's recommended auto/global ingest (ingest.twitch.tv/ingests default)
               "url": "rtmp://ingest.global-contribute.live-video.net/app",
               "fallback": "rtmp://live.twitch.tv/app",
               # Twitch rejects/destabilizes >6000 kbps video; per-preset video caps (kbps)
               "max_kbps": {"1080p60": 6000, "1200p60 (16:10)": 6000, "1080p30": 4500,
                            "720p60": 4500, "720p30": 3000}},
}
ENABLED_PLATFORMS = ["youtube", "twitch"]

PRESETS = {
    "720p30": {"w": 1280, "h": 720, "fps": 30, "kbps": 4500},
    "720p60": {"w": 1280, "h": 720, "fps": 60, "kbps": 6000},
    "1080p30": {"w": 1920, "h": 1080, "fps": 30, "kbps": 6000},
    "1080p60": {"w": 1920, "h": 1080, "fps": 60, "kbps": 9000},
    "1200p60 (16:10)": {"w": 1920, "h": 1200, "fps": 60, "kbps": 9500},
}

DEFAULTS = {"platform": "youtube", "preset": "1080p60", "mic": False, "mic_gain": 50, "game_vol": 100, "av_offset": -160,
            "keys": {}, "custom_urls": {}}


def _load():
    try:
        with open(SETTINGS_PATH) as f:
            return {**DEFAULTS, **json.load(f)}
    except Exception:
        return json.loads(json.dumps(DEFAULTS))


def _save(s):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.chmod(tmp, 0o600)  # stream keys live here
    os.replace(tmp, SETTINGS_PATH)


def _user_env():
    env = dict(os.environ)
    uid = 1000
    try:
        import pwd
        uid = pwd.getpwnam(decky.DECKY_USER).pw_uid
    except Exception:
        pass
    rd = f"/run/user/{uid}"
    env["XDG_RUNTIME_DIR"] = rd  # force the deck user's session, never root's
    env["PIPEWIRE_RUNTIME_DIR"] = rd
    env["PULSE_RUNTIME_PATH"] = f"{rd}/pulse"
    env["PULSE_SERVER"] = f"unix:{rd}/pulse/native"
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={rd}/bus")
    env.pop("LD_LIBRARY_PATH", None)  # Decky's bundled libs can break system gst
    env.pop("PYTHONPATH", None)
    return env


async def _run(cmd, env):
    p = await asyncio.create_subprocess_shell(cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                              stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    return out.decode(errors="replace")


async def _pw_video_nodes(env):
    """All PipeWire Video/Source nodes as dicts {id, name, desc}."""
    if not shutil.which("pw-dump"):
        return []
    nodes = []
    try:
        for obj in json.loads(await _run("pw-dump", env) or "[]"):
            if not obj.get("type", "").endswith("Node"):
                continue
            props = (obj.get("info") or {}).get("props") or {}
            if props.get("media.class") != "Video/Source":
                continue
            nodes.append({"id": obj["id"], "name": props.get("node.name", ""),
                          "desc": props.get("node.description", "") or props.get("application.name", "")})
    except Exception as e:
        decky.logger.warning(f"pw-dump parse failed: {e}")
    return nodes


def _pick_gamescope(nodes):
    exact = [n for n in nodes if n["name"] == "gamescope"]
    if exact:
        return exact[0]
    fuzzy = [n for n in nodes if "gamescope" in (n["name"] + " " + n["desc"]).lower()]
    return fuzzy[0] if fuzzy else None


async def _find_gamescope_node(env):
    n = _pick_gamescope(await _pw_video_nodes(env))
    return n["id"] if n else None


async def _pactl(args, env):
    if not shutil.which("pactl"):
        return ""
    return (await _run(f"pactl {args}", env)).strip()


async def _resolve_audio(env):
    """Return (game_monitor_device, mic_source_device); None if unresolved."""
    sink = await _pactl("get-default-sink", env)
    src = await _pactl("get-default-source", env)
    if not sink or not src:
        for line in (await _pactl("info", env)).splitlines():
            if line.startswith("Default Sink:") and not sink:
                sink = line.split(":", 1)[1].strip()
            if line.startswith("Default Source:") and not src:
                src = line.split(":", 1)[1].strip()
    game = f"{sink}.monitor" if sink else None
    if src and src.endswith(".monitor"):
        src = None  # default source is a monitor, not a real mic
    return game, src


async def _has_element(name, env):
    return bool(await _run(f"gst-inspect-1.0 {shlex.quote(name)}", env))


ELEMENTS = ["pipewiresrc", "pulsesrc", "vapostproc", "vah264enc", "vaapih264enc", "openh264enc",
            "x264enc", "avenc_aac", "webrtcdsp", "audiocheblimit", "audiodynamic", "level", "fdkaacenc", "flvmux", "rtmp2sink", "h264parse", "audiomixer"]


async def _available(env):
    res = await asyncio.gather(*[_has_element(e, env) for e in ELEMENTS])
    return dict(zip(ELEMENTS, res))


RECONNECT_DELAYS = [2, 5, 10, 20, 30]  # then 30 s repeatedly
RECONNECT_GIVE_UP_S = 300

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gst_worker.py")
SYS_PY = "/usr/bin/python3"
_gi_ok = None


async def _gi_available(env):
    global _gi_ok
    if _gi_ok is None:
        _gi_ok = bool(os.path.exists(SYS_PY) and os.path.exists(WORKER) and "ok" in await _run(
            f"{SYS_PY} -c \"import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; print('ok')\"", env))
    return _gi_ok


async def _spawn(env, args):
    """args: kwargs for _build_pipeline minus shell. Returns (proc, mode, enc)."""
    if await _gi_available(env):
        pipe, enc = _build_pipeline(**{k: v for k, v in args.items() if k != "_duration"}, shell=False)
        proc = await asyncio.create_subprocess_exec(
            SYS_PY, WORKER, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=1 << 20)
        proc.stdin.write(json.dumps({"pipeline": pipe, "duration": args.get("_duration")}).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        return proc, "worker", enc
    pipe, enc = _build_pipeline(**{k: v for k, v in args.items() if k != "_duration"}, shell=True)
    proc = await asyncio.create_subprocess_shell(
        f"exec gst-launch-1.0 -e -m {pipe}", env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=1 << 20)
    return proc, "gst-launch", enc


async def _stop_proc(proc, timeout=10):
    if proc.returncode is not None:
        return
    try:
        proc.send_signal(2)
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    except ProcessLookupError:
        pass


def _peaks(line):
    vals = []
    for m in re.finditer(r"peak=\((?:double|GValueArray)\)[{<]([^}>]*)[}>]", line):
        for v in m.group(1).split(","):
            try:
                vals.append(float(v))
            except ValueError:
                pass
    return vals


NO_NODE_MSG = ("Gamescope capture not found. Open this from Game Mode (not Desktop Mode); "
               "if you are in Game Mode, run Diagnostics and share the result.")


def _build_pipeline(have, node, p, mic, sink_el, location, game_dev=None, mic_dev=None,
                    game_vol=100, mic_gain=50, meter=False, av_offset_ms=0, shell=True):
    """Return (pipeline string, encoder name) or raise RuntimeError.
    shell=True -> quoted for /bin/sh + gst-launch; False -> for Gst.parse_launch (worker)."""
    def qv(v):  # quote a property value
        return shlex.quote(v) if shell else '"' + v.replace('"', '\\"') + '"'
    def qc(c):  # quote caps containing ()
        return f"'{c}'" if shell else c
    gop = p["fps"] * 2  # ~2 s keyframe interval for YouTube/Twitch
    W, H, F = p["w"], p["h"], p["fps"]
    if have.get("vapostproc"):
        # GPU upload+scale (keeps aspect: add-borders letterboxes e.g. 16:10 -> 16:9), output stays in VA memory
        scale_va = (f"vapostproc add-borders=true ! " + qc(f"video/x-raw(memory:VAMemory),width={W},height={H},format=NV12")
                    + f" ! videorate name=vrate drop-only=false skip-to-first=true ! "
                    + qc(f"video/x-raw(memory:VAMemory),framerate={F}/1"))
    else:
        scale_va = (f"videoconvert ! videoscale add-borders=true ! video/x-raw,width={W},height={H},format=NV12 ! "
                    f"videorate name=vrate drop-only=false skip-to-first=true ! video/x-raw,framerate={F}/1")
    sw_scale = (f"videoconvert ! videoscale add-borders=true ! video/x-raw,width={W},height={H},format=I420 ! "
                f"videorate name=vrate drop-only=false skip-to-first=true ! video/x-raw,framerate={F}/1")
    if have.get("vah264enc"):
        enc, conv = "vah264enc", scale_va
        venc = f"vah264enc name=venc target-usage=7 rate-control=cbr bitrate={p['kbps']} cpb-size={p['kbps']} key-int-max={gop} b-frames=0"
        prof = "high"
    elif have.get("vaapih264enc"):
        enc, conv = "vaapih264enc", (f"videorate name=vrate drop-only=false skip-to-first=true ! "
                                      f"video/x-raw,framerate={F}/1 ! videoconvert ! vaapipostproc width={W} height={H}")
        venc = f"vaapih264enc name=venc rate-control=cbr bitrate={p['kbps']} cpb-length=1000 keyframe-period={gop} max-bframes=0"
        prof = "high"
    elif have.get("openh264enc"):
        enc, conv = "openh264enc", sw_scale
        venc = (f"openh264enc name=venc rate-control=bitrate bitrate={p['kbps']*1000} gop-size={gop} "
                f"complexity=low usage-type=screen")
        prof = None  # openh264 only does constrained-baseline; let it negotiate
    elif have.get("x264enc"):
        enc, conv = "x264enc", sw_scale
        venc = f"x264enc name=venc tune=zerolatency speed-preset=veryfast bitrate={p['kbps']} vbv-buf-capacity=1000 key-int-max={gop} bframes=0"
        prof = "main"
    else:
        raise RuntimeError("No H.264 encoder found (tried vah264enc, vaapih264enc, openh264enc, x264enc). "
                           "Install gstreamer1-plugins-bad (VA) or openh264.")
    aenc = "avenc_aac" if have.get("avenc_aac") else ("fdkaacenc" if have.get("fdkaacenc") else None)
    if not aenc:
        raise RuntimeError("No AAC encoder found (avenc_aac / fdkaacenc)")
    caps = f"video/x-h264,profile={prof}" if prof else "video/x-h264"
    # Video: leaky, ~0.5 s max so a stall drops old frames instead of building a backlog.
    q = "queue name=vq max-size-buffers=0 max-size-time=500000000 max-size-bytes=0 leaky=downstream"
    # Audio: never leaky (drops = gaps/drift), same 0.5 s scale.
    qa = "queue max-size-buffers=0 max-size-time=500000000 max-size-bytes=0"
    # Known-good capture config (verified -1.1 dB on device); low-latency buffer/audiorate variants
    # produced silence through the worker, so they're not used.
    psrc = "pulsesrc do-timestamp=true"
    off_ns = max(-500, min(500, int(av_offset_ms))) * 1_000_000
    # Never shift audio backwards (negative ts would be clipped -> silence). Instead delay whichever
    # side should be later: offset<0 (audio earlier) => delay video; offset>0 => delay audio.
    shift = f"identity ts-offset={off_ns} ! " if off_ns > 0 else ""
    vshift = f"identity ts-offset={-off_ns} ! " if off_ns < 0 else ""
    araw = "audioconvert ! audioresample ! audio/x-raw,format=F32LE,rate=48000,channels=2"
    gdev = f"device={qv(game_dev)}" if game_dev else "device=@DEFAULT_MONITOR@"
    gvol = max(0, min(100, int(game_vol))) / 100.0
    lvl = "level name=gamelevel interval=250000000 post-messages=true ! " if meter and have.get("level") else ""
    game = f"{psrc} {gdev} ! {araw} ! volume volume={gvol:.2f} ! {lvl}"
    aout = f"{shift}{aenc} name=aenc bitrate=160000 ! aacparse ! {qa} ! mux."
    if mic:
        mdev = f"device={qv(mic_dev)}" if mic_dev else ""
        mvol = max(0, min(100, int(mic_gain))) / 50.0  # 50% = unity, 100% = 2x
        if have.get("webrtcdsp"):
            clean = ("audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
                     "webrtcdsp echo-cancel=false noise-suppression=true noise-suppression-level=high "
                     "high-pass-filter=true gain-control=false ! ")
        else:
            clean = "audioconvert ! "
            if have.get("audiocheblimit"):
                clean += "audiocheblimit mode=high-pass cutoff=100 ! "
            if have.get("audiodynamic"):
                clean += "audiodynamic mode=expander characteristics=soft-knee threshold=0.03 ratio=3 ! "
        micp = (f"{psrc} {mdev} ! {clean}{araw} ! volume volume={mvol:.2f} ! {qa} ! amix.")
        audio = (f"audiomixer name=amix ! audioconvert ! {aout} {game}{qa} ! amix. {micp}")
    else:
        audio = f"{game}audioconvert ! {aout}"
    if sink_el == "rtmp2sink":
        sink = f"rtmp2sink name=out location={qv(location)} async-connect=true sync=false"
    else:
        sink = f"filesink name=out location={qv(location)} sync=false"
    # gamescope only sends frames on damage; keepalive just bridges long idle gaps, videorate makes CFR.
    keep = 100
    pipe = (f"flvmux name=mux streamable=true latency=100000000 ! {sink} "
            # always-copy: hand gamescope its buffer back immediately (holding it starves capture);
            # tiny leaky queue decouples capture from processing. Scaling happens only on unique
            # frames; videorate duplicates (cheap refs) after scaling to reach constant fps.
            f"pipewiresrc name=vsrc path={node} do-timestamp=true keepalive-time={keep} always-copy=true ! "
            f"queue name=capq max-size-buffers=3 max-size-time=0 max-size-bytes=0 leaky=downstream ! "
            f"{conv} ! {venc} ! h264parse config-interval=-1 ! {caps} ! {vshift}{q} ! mux. {audio}")
    return pipe, enc


def _effective_kbps(platform, preset):
    base = PRESETS[preset]["kbps"]
    cap = PLATFORMS.get(platform, {}).get("max_kbps", {}).get(preset)
    return min(base, cap) if cap else base


class Plugin:
    proc = None
    started_at = None
    last_error = ""
    session = None
    stop_ev = None
    reconnecting = 0
    stopping = False
    health = {}

    # ---------- settings ----------
    async def get_state(self):
        s = _load()
        return {
            "platforms": [{"id": k, "label": v["label"], "needs_url": not v["url"]} for k, v in PLATFORMS.items() if k in ENABLED_PLATFORMS],
            "presets": list(PRESETS.keys()),
            "bitrates": {pl: {pr: _effective_kbps(pl, pr) for pr in PRESETS} for pl in ENABLED_PLATFORMS},
            "platform": s["platform"], "preset": s["preset"], "mic": s["mic"], "mic_gain": s["mic_gain"], "game_vol": s["game_vol"], "av_offset": s["av_offset"],
            "has_key": {k: bool(v) for k, v in s["keys"].items()},
            "custom_urls": s["custom_urls"],
            "live": self._live(), "reconnecting": self.reconnecting,
            "started_at": self.started_at, "error": self.last_error,
        }

    async def save_settings(self, platform, preset, mic, mic_gain=50, game_vol=100, av_offset=0):
        s = _load()
        if platform in ENABLED_PLATFORMS:
            s["platform"] = platform
        if preset in PRESETS:
            s["preset"] = preset
        s["mic"] = bool(mic)
        s["mic_gain"] = max(0, min(100, int(mic_gain)))
        s["game_vol"] = max(0, min(100, int(game_vol)))
        s["av_offset"] = max(-500, min(500, int(av_offset)))
        _save(s)
        return True

    async def set_stream_key(self, platform, key):
        s = _load()
        s["keys"][platform] = (key or "").strip()
        _save(s)
        decky.logger.info(f"Stream key updated for {platform}")  # never log the key
        return True

    async def set_custom_url(self, platform, url):
        s = _load()
        s["custom_urls"][platform] = (url or "").strip()
        _save(s)
        return True

    # ---------- streaming ----------
    async def start_stream(self):
        if self._live():
            return {"ok": False, "error": "Already live"}
        self.last_error = ""
        self.stopping = False
        s = _load()
        plat = s["platform"] if s["platform"] in ENABLED_PLATFORMS else ENABLED_PLATFORMS[0]
        key = s["keys"].get(plat, "")
        base = PLATFORMS[plat]["url"] or s["custom_urls"].get(plat, "")
        if not base:
            return self._fail("No ingest URL set for this platform")
        if not key and plat not in ("custom", "rumble"):
            return self._fail("No stream key set")
        env = _user_env()
        if not shutil.which("gst-launch-1.0"):
            return self._fail("gst-launch-1.0 not found on this system")
        p = dict(PRESETS[s["preset"]], kbps=_effective_kbps(plat, s["preset"]))
        decky.logger.info(f"Video bitrate {p['kbps']} kbps CBR ({plat}/{s['preset']})")
        have = await _available(env)
        if not have.get("rtmp2sink"):
            return self._fail("rtmp2sink missing (gstreamer1-plugins-bad)")
        node = await _find_gamescope_node(env)
        if node is None:
            return self._fail(NO_NODE_MSG)
        location = base.rstrip("/") + ("/" + key if key else "")
        ctx = {"env": env, "have": have, "s": s, "p": p, "location": location, "base": base, "key": key}
        try:
            proc, mode = await self._launch(ctx)
        except RuntimeError as e:
            return self._fail(str(e))
        self.started_at = time.time()  # kept across reconnects
        self.reconnecting = 0
        self.stop_ev = asyncio.Event()
        self.session = asyncio.get_event_loop().create_task(self._session(proc, mode, ctx))
        await self._emit_status()
        return {"ok": True}

    async def _launch(self, ctx):
        """(Re)resolve capture node + audio and spawn the pipeline. Raises RuntimeError."""
        env, s = ctx["env"], ctx["s"]
        node = await _find_gamescope_node(env)
        if node is None:
            raise RuntimeError(NO_NODE_MSG)
        game_dev, mic_dev = await _resolve_audio(env)
        decky.logger.info(f"Audio: game={game_dev} mic={mic_dev if s['mic'] else 'off'}")
        proc, mode, enc = await _spawn(env, dict(
            have=ctx["have"], node=node, p=ctx["p"], mic=s["mic"], sink_el="rtmp2sink",
            location=ctx["location"], game_dev=game_dev, mic_dev=mic_dev, game_vol=s["game_vol"],
            mic_gain=s["mic_gain"], av_offset_ms=s["av_offset"]))
        decky.logger.info(f"Started ({mode}, encoder {enc}, preset {s['preset']}) -> {ctx['base']}/<redacted>")
        self.proc = proc
        self.health = {"mode": mode}
        return proc, mode

    async def _emit_status(self):
        await decky.emit("stream_status", {"live": self._live(), "started_at": self.started_at,
                                           "error": self.last_error, "reconnecting": self.reconnecting})

    def _live(self):
        return self.session is not None and not self.session.done()

    async def _session(self, proc, mode, ctx):
        attempts, outage_start = 0, None
        try:
            while True:
                t0 = time.time()
                if proc is not None:
                    rc, msg = await self._watch(proc, ctx["key"], mode)
                    if self.stopping:
                        break
                    self.last_error = msg or f"Pipeline exited ({rc})"
                if time.time() - t0 > 60:  # it had been streaming fine: fresh backoff budget
                    attempts, outage_start = 0, None
                outage_start = outage_start or time.time()
                if time.time() - outage_start > RECONNECT_GIVE_UP_S:
                    self.last_error = f"Gave up reconnecting after {RECONNECT_GIVE_UP_S // 60} min: {self.last_error}"
                    decky.logger.error(self.last_error)
                    break
                delay = RECONNECT_DELAYS[min(attempts, len(RECONNECT_DELAYS) - 1)]
                attempts += 1
                self.reconnecting = attempts
                self.proc = None
                decky.logger.warning(f"Stream dropped ({self.last_error}); reconnect #{attempts} in {delay}s")
                await self._emit_status()
                try:
                    await asyncio.wait_for(self.stop_ev.wait(), delay)
                    break  # user pressed Stop during backoff
                except asyncio.TimeoutError:
                    pass
                try:
                    proc, mode = await self._launch(ctx)
                    self.reconnecting = 0
                    self.last_error = ""
                    await self._emit_status()
                except Exception as e:
                    proc = None
                    self.last_error = str(e)[:300]
        finally:
            self.proc = None
            self.reconnecting = 0
            self.started_at = None
            if self.stopping:
                self.last_error = ""
            self.session_done()

    def session_done(self):
        asyncio.get_event_loop().create_task(decky.emit(
            "stream_status", {"live": False, "started_at": None, "error": self.last_error, "reconnecting": 0}))

    async def _watch(self, proc, key, mode):
        """Pump worker output until the process exits. Returns (rc, redacted error message)."""
        errtask = asyncio.ensure_future(proc.stderr.read())
        last_log = 0
        worker_err = ""
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if mode != "worker" or not text.startswith("{"):
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if "stats" in msg:
                self.health = {"mode": mode, **msg["stats"]}
                await decky.emit("stream_health", self.health)
                if time.time() - last_log >= 10:
                    last_log = time.time()
                    decky.logger.info(f"Health: {json.dumps(self.health)}")
            elif "error" in msg:
                worker_err = msg["error"]
        err = (await errtask).decode(errors="replace").strip()
        rc = await proc.wait()
        msg = worker_err or (err.splitlines()[-1] if err else "")
        if key:
            msg = msg.replace(key, "<redacted>")
            err = err.replace(key, "<redacted>")
        if self.stopping:
            decky.logger.info(f"Stream stopped by user (rc={rc})")
        else:
            decky.logger.error(f"Pipeline exited rc={rc}: {err[-2000:]}")
        return rc, msg[:300]

    async def get_health(self):
        return self.health if self._live() else {}

    async def run_diagnostics(self, seconds=20):
        if self._live():
            return {"ok": False, "error": "Stop the stream before running diagnostics"}
        env = _user_env()
        out = {"ok": False, "elements": {}, "nodes": [], "node": None, "encoder": None,
               "file": None, "size": 0, "error": "", "mode": None,
               "game_source": None, "mic_source": None, "game_peak_db": None,
               "seconds": seconds, "frames": None, "expected_frames": None, "file_duration": None,
               "max_gap_ms": None, "dropped": None, "duplicated": None, "src_fps": None, "media_duration": None, "wall_seconds": None, "stall": None, "warnings": []}
        if not shutil.which("gst-launch-1.0"):
            out["error"] = "gst-launch-1.0 not found"
            return out
        have = await _available(env)
        out["elements"] = have
        nodes = await _pw_video_nodes(env)
        out["nodes"] = [f"{n['id']}: {n['name']} {n['desc']}".strip() for n in nodes]
        pick = _pick_gamescope(nodes)
        if not pick:
            out["error"] = NO_NODE_MSG
            return out
        out["node"] = f"{pick['id']} ({pick['name']})"
        home = getattr(decky, "DECKY_USER_HOME", os.path.expanduser("~"))
        os.makedirs(os.path.join(home, "Videos"), exist_ok=True)
        path = os.path.join(home, "Videos", "decky-live-test.flv")
        if os.path.exists(path):
            os.remove(path)
        st = _load()
        p = PRESETS[st["preset"]]
        game_dev, mic_dev = await _resolve_audio(env)
        out["game_source"] = game_dev or "@DEFAULT_MONITOR@ (unresolved)"
        out["mic_source"] = (mic_dev or "default (unresolved)") if st["mic"] else "off"
        out["av_offset"] = st["av_offset"]
        try:
            proc, mode, enc = await _spawn(env, dict(
                have=have, node=pick["id"], p=p, mic=st["mic"], sink_el="filesink", location=path,
                game_dev=game_dev, mic_dev=mic_dev, game_vol=st["game_vol"], mic_gain=st["mic_gain"],
                meter=True, av_offset_ms=st["av_offset"], _duration=seconds))
        except RuntimeError as e:
            out["error"] = str(e)
            return out
        out["encoder"], out["mode"] = enc, mode
        errtask = asyncio.ensure_future(proc.stderr.read())
        peaks, last, werr = [], {}, ""

        async def pump():
            nonlocal last, werr
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                t = line.decode(errors="replace").strip()
                if mode == "worker" and t.startswith("{"):
                    try:
                        m = json.loads(t)
                    except ValueError:
                        continue
                    if "level" in m:
                        peaks.append(m["level"])
                    elif "stats" in m:
                        last = m["stats"]
                    elif "error" in m:
                        werr = m["error"]
                else:
                    peaks.extend(_peaks(t))
        reader = asyncio.ensure_future(pump())
        if mode != "worker":  # gst-launch has no duration option: SIGINT after N seconds
            try:
                await asyncio.wait_for(proc.wait(), seconds)
            except asyncio.TimeoutError:
                await _stop_proc(proc)
        try:
            await asyncio.wait_for(proc.wait(), seconds + 15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        await reader
        err = (await errtask).decode(errors="replace").strip()
        out["game_peak_db"] = round(max(peaks), 1) if peaks else None
        out["file"] = path
        out["size"] = os.path.getsize(path) if os.path.exists(path) else 0
        out["expected_frames"] = seconds * p["fps"]
        if last:
            out["frames"] = last.get("vframes")
            out["max_gap_ms"] = last.get("max_gap_ms")
            out["dropped"] = last.get("dropped")
            out["duplicated"] = last.get("duplicated")
            out["src_fps"] = last.get("src_fps_avg")
            out["media_duration"] = last.get("media_s")
        if shutil.which("gst-discoverer-1.0") and out["size"]:
            disc = await _run(f"gst-discoverer-1.0 {shlex.quote(path)}", env)
            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", disc)
            if m:
                out["file_duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 2)
        w = out["warnings"]
        if out["frames"] is not None and out["frames"] < 0.9 * out["expected_frames"]:
            w.append(f"Only {out['frames']}/{out['expected_frames']} frames encoded (pipeline can't keep up)")
        if out["max_gap_ms"] is not None and out["max_gap_ms"] > 500:
            w.append(f"Video stalled up to {out['max_gap_ms']} ms")
        wall = last.get("elapsed") if last else None
        out["wall_seconds"] = wall
        if out["frames"] is not None and out["frames"] < 0.9 * out["expected_frames"] and p["fps"] >= 60:
            w.append("Encoder can't sustain 60 fps here - try 1080p30 or 720p60")
        if out["file_duration"] is None:
            out["file_duration"] = out["media_duration"]
        if out["src_fps"] is not None and out["src_fps"] < 0.5 * p["fps"]:
            w.append(f"Capture delivers only {out['src_fps']} fps from gamescope (videorate duplicates the rest)")
        ref = wall or seconds
        if out["file_duration"] is not None and abs(out["file_duration"] - ref) > 1.5:
            w.append(f"File is {out['file_duration']} s for {ref} s wall-clock "
                     f"({round(100 * out['file_duration'] / ref)}% of realtime)")
        out["stall"] = bool(w)
        if out["game_peak_db"] is not None and out["game_peak_db"] < -90:
            w.append("Game audio is digital silence; is a game/video playing?")
        out["stall"] = bool(w)
        out["ok"] = out["size"] > 50_000 and (not werr or werr == "EOS timeout")
        if werr == "EOS timeout":
            w.append("EOS timed out; file was finalized by forced shutdown")
        if not out["ok"]:
            out["error"] = (werr or (err.splitlines()[-1] if err else "Test file empty or too small"))[:300]
        decky.logger.info(f"Diagnostics: {json.dumps(out)}")
        return out

    def _fail(self, msg):
        self.last_error = msg
        decky.logger.error(msg)
        return {"ok": False, "error": msg}

    async def stop_stream(self):
        if not self._live():
            return True
        self.stopping = True  # user-initiated: exit is not an error; cancels reconnect backoff
        self.stop_ev.set()
        if self.proc:
            await _stop_proc(self.proc, 10)  # SIGINT -> EOS, SIGKILL after 10 s
        try:
            await asyncio.wait_for(asyncio.shield(self.session), 5)
        except Exception:
            pass
        return True

    async def _main(self):
        decky.logger.info("Deck Live loaded")

    async def _unload(self):
        await self.stop_stream()
        decky.logger.info("Deck Live unloaded")

    async def _uninstall(self):
        await self.stop_stream()
