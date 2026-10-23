#!/usr/bin/env python3
"""Safely exec gst-launch with its pipeline supplied over stdin."""
import ctypes
import errno
import json
import os
import shutil
import sys


# Linux PR_SET_DUMPABLE.
PR_SET_DUMPABLE = 4


def _disable_proc_access():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def main():
    if sys.argv[1:] == ["--check"]:
        _disable_proc_access()
        print("ok")
        return

    cfg = json.load(sys.stdin)
    pipeline = cfg.get("pipeline")
    if not isinstance(pipeline, str) or not pipeline:
        raise ValueError("Missing GStreamer pipeline")
    gst = shutil.which("gst-launch-1.0")
    if not gst:
        raise FileNotFoundError(errno.ENOENT, "gst-launch-1.0 not found")

    _disable_proc_access()
    os.execvpe(gst, [gst, "-e", "-m", pipeline], os.environ)


if __name__ == "__main__":
    main()
