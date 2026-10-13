#!/usr/bin/env python3
"""Runs a GStreamer pipeline with the *system* python3 + PyGObject and prints JSON lines:
{"stats": {...}} every 2 s, {"level": dB}, {"eos": true}, {"error": "..."}.
Input (stdin, JSON): {"pipeline": str, "duration": seconds|null}. Pipeline is read from stdin so the
stream key never appears in argv / ps."""
import json, signal, sys, time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)
cfg = json.loads(sys.stdin.read())
pipe = Gst.parse_launch(cfg["pipeline"])
loop = GLib.MainLoop()
st = {"cap_overruns": 0, "src": 0, "first_pts": None, "last_pts": None, "vframes": 0, "aframes": 0, "bytes": 0, "q_overruns": 0, "last_v": None, "max_gap": 0.0,
      "lag_ms": None, "t0": time.monotonic()}
win = {"vframes": 0, "bytes": 0, "max_gap": 0.0, "t": time.monotonic()}
rc = [0]
eos_sent = [False]


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def on_video(pad, info):
    now = time.monotonic()
    if st["last_v"] is not None:
        gap = now - st["last_v"]
        st["max_gap"] = max(st["max_gap"], gap)
        win["max_gap"] = max(win["max_gap"], gap)
    st["last_v"] = now
    st["vframes"] += 1
    win["vframes"] += 1
    return Gst.PadProbeReturn.OK


def on_src(pad, info):
    st["src"] += 1
    return Gst.PadProbeReturn.OK


def on_audio(pad, info):
    st["aframes"] += 1
    return Gst.PadProbeReturn.OK


def on_out(pad, info):
    buf = info.get_buffer()
    n = buf.get_size()
    st["bytes"] += n
    if buf.pts != Gst.CLOCK_TIME_NONE:
        if st["first_pts"] is None:
            st["first_pts"] = buf.pts
        st["last_pts"] = buf.pts
    win["bytes"] += n
    clock = pipe.get_clock()
    if clock and buf.pts != Gst.CLOCK_TIME_NONE:
        running = clock.get_time() - pipe.get_base_time()
        st["lag_ms"] = round((running - buf.pts) / 1e6)
    return Gst.PadProbeReturn.OK


def probe(name, padname, cb):
    el = pipe.get_by_name(name)
    if el:
        el.get_static_pad(padname).add_probe(Gst.PadProbeType.BUFFER, cb)
    return el


probe("vsrc", "src", on_src)
probe("venc", "src", on_video)
probe("aenc", "src", on_audio)
probe("out", "sink", on_out)
vq = pipe.get_by_name("vq")
if vq:
    vq.connect("overrun", lambda *_: st.__setitem__("q_overruns", st["q_overruns"] + 1))
capq = pipe.get_by_name("capq")
if capq:
    capq.connect("overrun", lambda *_: st.__setitem__("cap_overruns", st["cap_overruns"] + 1))
vrate = pipe.get_by_name("vrate")


def report():
    now = time.monotonic()
    dt = max(now - win["t"], 1e-3)
    out({"stats": {
        "fps": round(win["vframes"] / dt, 1), "kbps": round(win["bytes"] * 8 / dt / 1000),
        "vframes": st["vframes"], "aframes": st["aframes"], "bytes": st["bytes"],
        "dropped": (vrate.get_property("drop") if vrate else 0),
        "duplicated": (vrate.get_property("duplicate") if vrate else 0),
        "q_overruns": st["q_overruns"] + st["cap_overruns"], "lag_ms": st["lag_ms"],
        "gap_ms": round(win["max_gap"] * 1000), "max_gap_ms": round(st["max_gap"] * 1000),
        "src_frames": st["src"], "src_fps_avg": round(st["src"] / max(now - st["t0"], 1e-3), 1),
        "media_s": (round((st["last_pts"] - st["first_pts"]) / 1e9, 2) if st["first_pts"] is not None else None),
        "elapsed": round(now - st["t0"], 1)}})
    win.update(vframes=0, bytes=0, max_gap=0.0, t=now)
    return True


def _finish(reason):
    report()
    if reason:
        out({"error": reason})
    loop.quit()
    return False


def _eos_to_mux():
    """Fallback: live sources (pipewiresrc) may not forward EOS; push it straight into the
    muxer's sink pads so flvmux/filesink still finalize."""
    mux = pipe.get_by_name("mux")
    if mux:
        for pad in mux.sinkpads:
            pad.send_event(Gst.Event.new_eos())
    GLib.timeout_add_seconds(4, _finish, "EOS timeout")
    return False


def send_eos(*_):
    if not eos_sent[0]:
        eos_sent[0] = True
        it = pipe.iterate_sources()
        while True:
            ok, el = it.next()
            if ok != Gst.IteratorResult.OK:
                break
            el.send_event(Gst.Event.new_eos())
        GLib.timeout_add_seconds(3, _eos_to_mux)
    return True


def on_msg(bus, msg):
    t = msg.type
    if t == Gst.MessageType.EOS:
        report()
        out({"eos": True})
        loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        out({"error": f"{msg.src.get_name()}: {err.message}"})
        sys.stderr.write(f"{err.message}\n{dbg}\n")
        rc[0] = 1
        loop.quit()
    elif t == Gst.MessageType.ELEMENT:
        s = msg.get_structure()
        if s and s.get_name() == "level":
            try:
                peaks = s.get_value("peak")
            except Exception:  # GValueArray not introspectable on some versions -> parse text
                import re
                peaks = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?",
                         (re.search(r"peak=\([^)]*\)[<{]([^>}]*)", s.to_string()) or [None, ""])[1])]
            if peaks:
                out({"level": max(peaks)})


bus = pipe.get_bus()
bus.add_signal_watch()
bus.connect("message", on_msg)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, send_eos)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, send_eos)
GLib.timeout_add_seconds(2, report)
if cfg.get("duration"):
    GLib.timeout_add(int(cfg["duration"] * 1000), lambda: send_eos() and False)
if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    out({"error": "Pipeline failed to start"})
    sys.exit(1)
loop.run()
pipe.set_state(Gst.State.NULL)
sys.exit(rc[0])
