import asyncio
import json
import re
import os
import shlex
import shutil
import time
import urllib.parse

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

DEFAULTS = {"platform": "youtube", "preset": "1080p60", "mic": False, "mic_gain": 50, "game_vol": 100, "av_offset": -160, "encoder": "auto",
            "keys": {}, "custom_urls": {}}


def _load():
    try:
        os.chmod(SETTINGS_PATH, 0o600)  # tighten files left 0644 by older builds
    except OSError:
        pass
    try:
        with open(SETTINGS_PATH) as f:
            return {**DEFAULTS, **json.load(f)}
    except Exception:
        return json.loads(json.dumps(DEFAULTS))


def _save(s):
    """Atomic, durable write. The file holds stream keys: it is created 0600 (never briefly
    world-readable) and fsync'd before and after the rename."""
    d = os.path.dirname(SETTINGS_PATH)
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)  # only this plugin reads its settings dir
    except OSError:
        pass
    tmp = SETTINGS_PATH + ".tmp"
    try:
        os.unlink(tmp)  # a stale tmp could have looser permissions; O_CREAT keeps existing modes
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)  # in case the umask was odd
        json.dump(s, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SETTINGS_PATH)
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


_URL_RE = re.compile(r"\brtmps?://[^\s'\"<>]+", re.I)


def _redact(text, key=None):
    """Remove the stream key (raw and percent-encoded) and any RTMP(S) URL from log/error text."""
    if not text:
        return text
    if key:
        text = text.replace(key, "<redacted>")
        for enc in {urllib.parse.quote(key, safe=""), urllib.parse.quote(key)}:
            if enc and enc != key:
                text = text.replace(enc, "<redacted>")
    return _URL_RE.sub("<redacted-url>", text)


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


async def _run(cmd, env, timeout=None):
    p = await asyncio.create_subprocess_shell(cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                              stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill_group(p)
        await p.wait()
        return ""
    return out.decode(errors="replace")


def _kill_group(proc):
    try:
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


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


async def _has_element(name, env, min_version=None):
    """Registry lookup (`gst-inspect-1.0 --exists`). Unlike a full gst-inspect, this does not load
    the plugin, so checking nvcodec/va elements does not open a CUDA context or a VA display."""
    args = ["gst-inspect-1.0", "--exists"] + ([f"--atleast-version={min_version}"] if min_version else []) + [name]
    try:
        p = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return False
    try:
        return await asyncio.wait_for(p.wait(), 15) == 0
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return False


ELEMENTS = ["pipewiresrc", "pulsesrc", "vapostproc", "vah264enc", "vaapih264enc", "openh264enc",
            "nvh264enc", "nvautogpuh264enc", "cudaupload", "cudaconvertscale",
            "x264enc", "avenc_aac", "webrtcdsp", "audiocheblimit", "audiodynamic", "level", "fdkaacenc", "flvmux", "rtmp2sink", "h264parse", "audiomixer"]


async def _available(env):
    res = await asyncio.gather(*[_has_element(e, env) for e in ELEMENTS])
    have = dict(zip(ELEMENTS, res))
    if have.get("nvh264enc"):
        # 1.22+ nvh264enc is GstNvEncoder (preset=p1..p7, tune=...). Older GstNvBaseEnc only knows
        # preset=hp/hq/low-latency-hq and has no tune/repeat-sequence-header.
        have["nvenc_new"] = await _has_element("nvh264enc", env, "1.22")
    return have


# ---------- encoder selection ----------
VAAPI_ENCS = ["vah264enc", "vaapih264enc"]
NVENC_ENCS = ["nvh264enc", "nvautogpuh264enc"]
SW_ENCS = ["x264enc", "openh264enc"]  # openh264 = last resort (baseline only)
ENC_FAMILY = {**{e: "vaapi" for e in VAAPI_ENCS}, **{e: "nvenc" for e in NVENC_ENCS},
              **{e: "software" for e in SW_ENCS}}
ENCODER_PREFS = ["auto", "vaapi", "nvenc", "software"]
_VENDORS = {"0x10de": "nvidia", "0x8086": "intel", "0x1002": "amd"}


def encoder_order(pref, display_gpu=None):
    """Candidate encoders, best first. An explicit family is the only thing probed (so Software never
    opens the GPU and VA-API never opens CUDA); Auto walks the full chain and stops at the first
    encoder that works."""
    if pref == "software":
        return list(SW_ENCS)
    if pref == "vaapi":
        return list(VAAPI_ENCS)
    if pref == "nvenc":
        return list(NVENC_ENCS)
    if display_gpu == "nvidia":
        return NVENC_ENCS + VAAPI_ENCS + SW_ENCS
    return VAAPI_ENCS + NVENC_ENCS + SW_ENCS


def fallback_order(pref, display_gpu=None, bad=()):
    """Order after an encoder failed while live: the chosen family, then the rest of the Auto
    chain (hardware -> x264). Software never promotes to hardware."""
    order = encoder_order(pref, display_gpu)
    if pref != "software":
        order += [e for e in encoder_order("auto", display_gpu) if e not in order]
    return [e for e in order if e not in bad]


class NoEncoderError(RuntimeError):
    pass


def _no_encoder_msg(pref, tried):
    hint = {"nvenc": " NVENC isn't usable right now; pick Auto or Software.",
            "vaapi": " VA-API isn't usable here; pick Auto or Software."}.get(pref, "")
    return (f"No working H.264 encoder (tried {tried or 'none'}).{hint} Run Diagnostics; install "
            "gstreamer1-plugins-bad (VA/NVENC) or x264/openh264.")


def pick_encoder(pref, display_gpu, works):
    for e in encoder_order(pref, display_gpu):
        if works.get(e):
            return e
    raise NoEncoderError(_no_encoder_msg(pref, ", ".join(encoder_order(pref, display_gpu))))


def gpu_info(drm="/sys/class/drm", proc="/proc"):
    """{'gpus': {card: vendor}, 'display_gpu': vendor|None, 'nvidia': bool, 'source': str}.
    Heuristic for which GPU gamescope composites on: an explicit --prefer-vk-device on the
    gamescope command line wins; otherwise the GPU driving the connected (internal first) display."""
    gpus, display, source = {}, None, "unknown"
    try:
        for card in sorted(os.listdir(drm)):
            if re.fullmatch(r"card\d+", card):
                try:
                    with open(os.path.join(drm, card, "device", "vendor")) as f:
                        gpus[card] = _VENDORS.get(f.read().strip(), "other")
                except OSError:
                    pass
        conns = []
        for c in os.listdir(drm):
            m = re.fullmatch(r"(card\d+)-(.+)", c)
            if not m:
                continue
            try:
                with open(os.path.join(drm, c, "status")) as f:
                    if f.read().strip() != "connected":
                        continue
            except OSError:
                continue
            conns.append((0 if m.group(2).startswith("eDP") else 1, m.group(1)))
        if conns:
            display = gpus.get(sorted(conns)[0][1])
            source = "display connector"
    except OSError:
        pass
    try:
        for pid in os.listdir(proc):
            if not pid.isdigit():
                continue
            try:
                with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                    args = f.read().decode(errors="replace").split("\0")
            except OSError:
                continue
            if args and os.path.basename(args[0]).startswith("gamescope") and "--prefer-vk-device" in args:
                vid = args[args.index("--prefer-vk-device") + 1].split(":")[0].lower()
                v = _VENDORS.get("0x" + vid.replace("0x", ""))
                if v:
                    display, source = v, "gamescope --prefer-vk-device"
                break
    except OSError:
        pass
    nvidia = "nvidia" in gpus.values() or os.path.exists("/dev/nvidia0")
    return {"gpus": gpus, "display_gpu": display, "nvidia": nvidia, "source": source}


# ---------- VRAM ----------
LOW_VRAM_MIB = 4096          # 4 GB-class cards (RTX 3050 4GB): Steam already OOM'd here
MIN_FREE_VRAM_MIB = 512      # don't open an NVENC/CUDA session below this
LIGHT_PRESET = "720p30"      # light default on low-VRAM GPUs (1280x720, 30 fps, 4500 kbps)
_vram_total = None


async def _vram_mib(env=None):
    """(free, total) MiB of the first NVIDIA GPU from nvidia-smi (NVML, no CUDA context), or (None, None)."""
    global _vram_total
    if not shutil.which("nvidia-smi"):
        return None, None
    try:
        out = await _run("nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader,nounits",
                         env if env is not None else _user_env(), timeout=3)
        free_s, total_s = out.strip().splitlines()[0].split(",")[:2]
        free, total = int(float(free_s)), int(float(total_s))
    except Exception:  # missing/odd nvidia-smi must never break get_state or Go Live
        return None, None
    _vram_total = total
    return free, total


async def _vram_total_mib(env=None):
    if _vram_total is None:
        await _vram_mib(env)
    return _vram_total


def low_vram(total_mib):
    return total_mib is not None and 0 < total_mib <= LOW_VRAM_MIB


def effective_preset(s, total_mib):
    """User's preset, or the light preset on a <=4 GB GPU until the user picks one themselves."""
    if low_vram(total_mib) and not s.get("preset_set"):
        return LIGHT_PRESET
    return s["preset"] if s.get("preset") in PRESETS else DEFAULTS["preset"]


# ---------- encoder probe ----------
PROBE_FRAMES = 10
PROBE_TIMEOUT_S = 8
_probe_cache = {}  # (encoder, w, h, fps, nvenc_new) -> {"present", "works", "error"}


def _probe_key(have, enc, p):
    return (enc, p["w"], p["h"], p["fps"], bool(have.get("nvenc_new", True)) if enc in NVENC_ENCS else None)


def _probe_pipeline(have, enc, p, shell=False):
    """Short test encode with the *same* upload/scale chain and encoder properties as the real
    pipeline, at the real stream resolution and fps, so a probe pass means the broadcast string
    parses and the device opens at that size."""
    _, conv, venc, prof = _video_chain(have, p, enc, shell)
    caps = f"video/x-h264,profile={prof}" if prof else "video/x-h264"
    src = f"videotestsrc num-buffers={PROBE_FRAMES} ! video/x-raw,width={p['w']},height={p['h']},framerate={p['fps']}/1"
    return f"{src} ! {conv} ! {venc} ! h264parse ! {caps} ! fakesink sync=false"


async def _probe_one(env, have, enc, p, force=False):
    """Probe one encoder at preset p. One short-lived process per probe: the pipeline is set to
    NULL and released inside it, and the process exit frees any CUDA/VA context for certain."""
    k = _probe_key(have, enc, p)
    if not force and k in _probe_cache:
        return _probe_cache[k]
    if not have.get(enc):
        res = {"present": False, "works": False, "error": "not installed"}
        _probe_cache[k] = res
        return res
    if ENC_FAMILY.get(enc) == "nvenc":
        free, _ = await _vram_mib(env)
        if free is not None and free < MIN_FREE_VRAM_MIB:  # transient: not cached
            return {"present": True, "works": False,
                    "error": f"only {free} MiB GPU memory free (need {MIN_FREE_VRAM_MIB})"}
    try:
        gi = await _gi_available(env)
        pipe = _probe_pipeline(have, enc, p, shell=not gi)
    except RuntimeError as e:
        res = {"present": True, "works": False, "error": str(e)[:200]}
        _probe_cache[k] = res
        return res
    if gi:
        res = await _probe_worker(env, pipe)
    else:  # no key in a probe, so gst-launch is fine here
        out = await _run(f"timeout -k 2 {PROBE_TIMEOUT_S} gst-launch-1.0 -q {pipe} >/dev/null 2>&1 && echo OK",
                         env, timeout=PROBE_TIMEOUT_S + 5)
        res = {"works": "OK" in out, "error": "" if "OK" in out else "test encode failed"}
    res = {"present": True, "works": bool(res.get("works")), "error": str(res.get("error") or "")[:200]}
    _probe_cache[k] = res
    decky.logger.info(f"Encoder probe {enc} {p['w']}x{p['h']}@{p['fps']}: "
                      + ("ok" if res["works"] else f"failed ({res['error']})"))
    return res


async def _probe_worker(env, pipe):
    try:
        proc = await asyncio.create_subprocess_exec(
            SYS_PY, WORKER, "--probe", env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return {"works": False, "error": str(e)}
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(json.dumps({"pipeline": pipe, "timeout": PROBE_TIMEOUT_S}).encode()),
            PROBE_TIMEOUT_S + 5)
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return {"works": False, "error": "probe timed out"}
    try:
        return json.loads(out.decode(errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"works": False, "error": f"probe crashed (rc={proc.returncode})"}


async def probe_encoders(env, have, p, names, force=False):
    """Probe each name in order (no early stop). For Diagnostics/tests; Go Live uses _choose."""
    return {e: await _probe_one(env, have, e, p, force) for e in names}


def probe_snapshot(p=None):
    """Cached probe results, optionally only for preset p. Never opens a device."""
    return {f"{k[0]} {k[1]}x{k[2]}@{k[3]}": v for k, v in _probe_cache.items()
            if p is None or k[1:4] == (p["w"], p["h"], p["fps"])}


_ENC_FAULT_RE = re.compile(r"\bvenc\b|cuda|nvenc|nvcodec|nvh264|nvautogpu|out of memory|memory|\bvaapi|"
                           r"vapostproc|\bva\b|negotiat|no property|encod", re.I)


def is_encoder_fault(enc, msg, ran_s, health=None):
    """Did a live hardware pipeline die because of the encoder/GPU (not the network)?"""
    if ENC_FAMILY.get(enc) not in ("nvenc", "vaapi"):
        return False
    msg = msg or ""
    if msg.startswith("out:"):  # rtmp2sink (named "out"): network/ingest problem
        return False
    if _ENC_FAULT_RE.search(msg):
        return True
    return ran_s < 15 and not (health or {}).get("vframes")


RECONNECT_DELAYS = [2, 5, 10, 20, 30]  # then 30 s repeatedly
RECONNECT_GIVE_UP_S = 300

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gst_worker.py")
SYS_PY = "/usr/bin/python3"
_gi_ok = False

NO_GI_MSG = ("Streaming needs PyGObject (python3-gobject) and the GStreamer typelibs so your stream key "
             "stays private. Install python3-gobject, or update Bazzite/SteamOS, then reload Decky.")


async def _gi_available(env):
    """True when system python3 + PyGObject + gst_worker.py can run. Only success is cached,
    so installing python3-gobject works without restarting Decky."""
    global _gi_ok
    if not _gi_ok:
        _gi_ok = bool(os.path.exists(SYS_PY) and os.path.exists(WORKER) and "ok" in await _run(
            f"{SYS_PY} -c \"import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; print('ok')\"", env))
    return _gi_ok


async def _spawn(env, args):
    """args: kwargs for _build_pipeline minus shell. Returns (proc, mode, enc).
    Network output only ever runs through the worker, which reads the pipeline (and so the
    stream key) from stdin. gst-launch would put the key in argv, readable by any local process
    via /proc/<pid>/cmdline, so it is refused (fail closed) and kept for local Diagnostics only."""
    if await _gi_available(env):
        pipe, enc = _build_pipeline(**{k: v for k, v in args.items() if k != "_duration"}, shell=False)
        proc = await asyncio.create_subprocess_exec(
            SYS_PY, WORKER, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=1 << 20)
        proc.stdin.write(json.dumps({"pipeline": pipe, "duration": args.get("_duration")}).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        return proc, "worker", enc
    if args.get("sink_el") != "filesink":
        raise RuntimeError(NO_GI_MSG)
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


def _video_chain(have, p, encoder=None, shell=True):
    """(encoder, convert/scale chain, encoder element string, h264 profile) for preset p."""
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
    if encoder is None:  # legacy: first installed encoder in the default chain
        encoder = next((e for e in VAAPI_ENCS + NVENC_ENCS + SW_ENCS if have.get(e)), None)
    k = p["kbps"]
    if encoder == "vah264enc":
        enc, conv = encoder, scale_va
        venc = f"vah264enc name=venc target-usage=7 rate-control=cbr bitrate={k} cpb-size={k} key-int-max={gop} b-frames=0"
        prof = "high"
    elif encoder == "vaapih264enc":
        enc, conv = encoder, (f"videorate name=vrate drop-only=false skip-to-first=true ! "
                              f"video/x-raw,framerate={F}/1 ! videoconvert ! vaapipostproc width={W} height={H}")
        venc = f"vaapih264enc name=venc rate-control=cbr bitrate={k} cpb-length=1000 keyframe-period={gop} max-bframes=0"
        prof = "high"
    elif encoder in NVENC_ENCS:
        enc = encoder
        if have.get("cudaupload") and have.get("cudaconvertscale"):
            # system-memory capture -> CUDA upload, GPU convert+scale (keeps aspect), stays in CUDA memory
            conv = ("cudaupload ! cudaconvertscale add-borders=true ! "
                    + qc(f"video/x-raw(memory:CUDAMemory),width={W},height={H},format=NV12")
                    + " ! videorate name=vrate drop-only=false skip-to-first=true ! "
                    + qc(f"video/x-raw(memory:CUDAMemory),framerate={F}/1"))
        else:  # nvh264enc also accepts system-memory NV12 (uploads internally)
            conv = (f"videoconvert ! videoscale add-borders=true ! video/x-raw,width={W},height={H},format=NV12 ! "
                    f"videorate name=vrate drop-only=false skip-to-first=true ! video/x-raw,framerate={F}/1")
        if encoder == "nvautogpuh264enc" or have.get("nvenc_new", True):  # GstNvEncoder (1.22+)
            venc = (f"{encoder} name=venc rc-mode=cbr bitrate={k} vbv-buffer-size={k} gop-size={gop} bframes=0 "
                    f"preset=p4 tune=low-latency zerolatency=true repeat-sequence-header=true")
        else:  # legacy GstNvBaseEnc nicks (no p1..p7, no tune); h264parse config-interval adds SPS/PPS
            venc = (f"{encoder} name=venc rc-mode=cbr bitrate={k} vbv-buffer-size={k} gop-size={gop} bframes=0 "
                    f"preset=low-latency-hq zerolatency=true")
        prof = "high"
    elif encoder == "x264enc":
        enc, conv = encoder, sw_scale
        venc = f"x264enc name=venc tune=zerolatency speed-preset=veryfast bitrate={k} vbv-buf-capacity=1000 key-int-max={gop} bframes=0"
        prof = "main"
    elif encoder == "openh264enc":
        enc, conv = encoder, sw_scale
        venc = (f"openh264enc name=venc rate-control=bitrate bitrate={k*1000} gop-size={gop} "
                f"complexity=low usage-type=screen")
        prof = None  # openh264 only does constrained-baseline; let it negotiate
    else:
        raise RuntimeError("No H.264 encoder found (tried vah264enc, vaapih264enc, nvh264enc, "
                           "nvautogpuh264enc, x264enc, openh264enc).")
    return enc, conv, venc, prof


def _build_pipeline(have, node, p, mic, sink_el, location, game_dev=None, mic_dev=None,
                    game_vol=100, mic_gain=50, meter=False, av_offset_ms=0, shell=True, encoder=None):
    """Return (pipeline string, encoder name) or raise RuntimeError.
    shell=True -> quoted for /bin/sh + gst-launch; False -> for Gst.parse_launch (worker)."""
    def qv(v):  # quote a property value
        return shlex.quote(v) if shell else '"' + v.replace('"', '\\"') + '"'
    enc, conv, venc, prof = _video_chain(have, p, encoder, shell)
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
    starting = False
    health = {}
    bad_encoders = frozenset()  # encoders that failed while live during this Go Live session

    # ---------- settings ----------
    async def get_state(self):
        s = _load()
        vram = await _vram_total_mib()
        return {
            "platforms": [{"id": k, "label": v["label"], "needs_url": not v["url"]} for k, v in PLATFORMS.items() if k in ENABLED_PLATFORMS],
            "presets": list(PRESETS.keys()),
            "bitrates": {pl: {pr: _effective_kbps(pl, pr) for pr in PRESETS} for pl in ENABLED_PLATFORMS},
            "platform": s["platform"], "preset": effective_preset(s, vram), "mic": s["mic"], "mic_gain": s["mic_gain"], "game_vol": s["game_vol"], "av_offset": s["av_offset"], "encoder": s.get("encoder", "auto"), "encoder_prefs": ENCODER_PREFS,
            "vram_total_mib": vram, "light_defaults": low_vram(vram) and not s.get("preset_set"),
            "has_key": {k: bool(v) for k, v in s["keys"].items()},
            "custom_urls": s["custom_urls"],
            "live": self._live(), "reconnecting": self.reconnecting,
            "started_at": self.started_at, "error": self.last_error,
        }

    async def save_settings(self, platform, preset, mic, mic_gain=50, game_vol=100, av_offset=0, encoder=None):
        s = _load()
        if platform in ENABLED_PLATFORMS:
            s["platform"] = platform
        if preset in PRESETS and preset != effective_preset(s, await _vram_total_mib()):
            s["preset"] = preset
            s["preset_set"] = True  # an explicit choice overrides the low-VRAM light default
        s["mic"] = bool(mic)
        s["mic_gain"] = max(0, min(100, int(mic_gain)))
        s["game_vol"] = max(0, min(100, int(game_vol)))
        s["av_offset"] = max(-500, min(500, int(av_offset)))
        if encoder in ENCODER_PREFS:
            s["encoder"] = encoder
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
        if self._live() or self.starting:
            return {"ok": False, "error": "Already live"}
        self.starting = True  # a double tap must not start two pipelines (two NVENC sessions)
        try:
            return await self._start_stream()
        finally:
            self.starting = False

    async def _start_stream(self):
        self.last_error = ""
        self.bad_encoders = frozenset()
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
        vram = await _vram_total_mib(env)
        s["preset"] = effective_preset(s, vram)
        if low_vram(vram) and not s.get("preset_set"):
            decky.logger.info(f"{vram} MiB GPU: using light default preset {LIGHT_PRESET} (pick a preset to override)")
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
        if not await _gi_available(env):  # fail closed before any encoder probe touches the GPU
            raise RuntimeError(NO_GI_MSG)
        node = await _find_gamescope_node(env)
        if node is None:
            raise RuntimeError(NO_NODE_MSG)
        game_dev, mic_dev = await _resolve_audio(env)
        decky.logger.info(f"Audio: game={game_dev} mic={mic_dev if s['mic'] else 'off'}")
        enc_name, ginfo = await self._choose_encoder(env, ctx["have"], s.get("encoder", "auto"), ctx["p"],
                                                     bad=self.bad_encoders)
        proc, mode, enc = await _spawn(env, dict(encoder=enc_name,
            have=ctx["have"], node=node, p=ctx["p"], mic=s["mic"], sink_el="rtmp2sink",
            location=ctx["location"], game_dev=game_dev, mic_dev=mic_dev, game_vol=s["game_vol"],
            mic_gain=s["mic_gain"], av_offset_ms=s["av_offset"]))
        decky.logger.info(f"Started ({mode}, encoder {enc}, preset {s['preset']}) -> {ctx['base']}/<redacted>")
        self.proc = proc
        self.encoder = enc
        self.health = {"mode": mode, "encoder": enc}
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
                    if is_encoder_fault(self.encoder, msg, time.time() - t0, self.health):
                        # Don't reconnect the same failing GPU encoder for 5 minutes: mark it bad
                        # for this session and switch to the next one (VA-API / x264) right away.
                        # The whole family: a second NVENC element on an out-of-memory GPU fails the same way.
                        fam = ENC_FAMILY.get(self.encoder)
                        self.bad_encoders = self.bad_encoders | {e for e, f in ENC_FAMILY.items() if f == fam}
                        self.proc = None
                        decky.logger.warning(f"Encoder {self.encoder} failed while live ({self.last_error}); "
                                             f"skipping {fam} for this session")
                        try:
                            proc, mode = await self._launch(ctx)
                            self.last_error = ""
                            await self._emit_status()
                            continue
                        except NoEncoderError as e:
                            self.last_error = _redact(str(e), ctx["key"])[:300]
                            decky.logger.error(self.last_error)
                            break
                        except Exception as e:
                            proc = None
                            self.last_error = _redact(str(e), ctx["key"])[:300]
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
                except NoEncoderError as e:
                    self.last_error = _redact(str(e), ctx["key"])[:300]
                    decky.logger.error(self.last_error)
                    break
                except Exception as e:
                    proc = None
                    self.last_error = _redact(str(e), ctx["key"])[:300]
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
                self.health = {"mode": mode, "encoder": self.encoder, **msg["stats"]}
                await decky.emit("stream_health", self.health)
                if time.time() - last_log >= 10:
                    last_log = time.time()
                    decky.logger.info(f"Health: {json.dumps(self.health)}")
            elif "error" in msg:
                worker_err = msg["error"]
        err = (await errtask).decode(errors="replace").strip()
        rc = await proc.wait()
        msg = worker_err or (err.splitlines()[-1] if err else "")
        msg, err = _redact(msg, key), _redact(err, key)
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
        vram = await _vram_total_mib(env)
        st["preset"] = effective_preset(st, vram)
        p = PRESETS[st["preset"]]
        out["preset"] = st["preset"]
        game_dev, mic_dev = await _resolve_audio(env)
        out["game_source"] = game_dev or "@DEFAULT_MONITOR@ (unresolved)"
        out["mic_source"] = (mic_dev or "default (unresolved)") if st["mic"] else "off"
        out["av_offset"] = st["av_offset"]
        try:
            out["encoder_pref"] = st.get("encoder", "auto")
            try:
                enc_name, ginfo = await self._choose_encoder(env, have, out["encoder_pref"], p, force_probe=True)
            finally:
                out["gpu"] = dict(gpu_info(), vram_total_mib=vram)
                out["encoder_probe"] = {k.split(" ")[0]: v["works"] for k, v in probe_snapshot(p).items()}
            proc, mode, enc = await _spawn(env, dict(encoder=enc_name,
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
        msg = _redact(msg)
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

    encoder = None

    async def _choose_encoder(self, env, have, pref, p, force_probe=False, bad=frozenset()):
        """Lazy probe at Go Live: test only the encoders this setting allows, in order, at the real
        resolution/fps, and stop at the first that works. Results are cached per encoder+size+fps."""
        ginfo = gpu_info()
        order = fallback_order(pref, ginfo["display_gpu"], bad) if bad else encoder_order(pref, ginfo["display_gpu"])
        tried = []
        for e in order:
            r = await _probe_one(env, have, e, p, force_probe)
            if r["works"]:
                decky.logger.info(f"Encoder: pref={pref} display_gpu={ginfo['display_gpu']} ({ginfo['source']}) "
                                  f"{p['w']}x{p['h']}@{p['fps']} -> {e}")
                return e, ginfo
            tried.append(f"{e}: {r['error']}" if r["present"] else f"{e}: not installed")
        tried += [f"{e}: failed while live" for e in bad]
        raise NoEncoderError(_no_encoder_msg(pref, "; ".join(tried)))

    async def get_encoders(self):
        """Cached probe results only; never opens an encoder (no VRAM use while a game runs)."""
        return {"probe": probe_snapshot(), "gpu": dict(gpu_info(), vram_total_mib=await _vram_total_mib()),
                "families": ENC_FAMILY, "bad": sorted(self.bad_encoders)}

    async def _main(self):
        decky.logger.info("Deck Live loaded")

    async def _unload(self):
        await self.stop_stream()
        decky.logger.info("Deck Live unloaded")

    async def _uninstall(self):
        await self.stop_stream()
