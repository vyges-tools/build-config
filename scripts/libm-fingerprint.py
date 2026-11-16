#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fingerprint this host's libm, so it can be recorded next to a reference capture.

A program that calls the C math library gets the HOST's results: platform libms differ by about
one ulp, and glibc on x86-64 also picks FMA or non-FMA code paths by CPU at load time. Two
captures of the same binary on different hosts can therefore differ; this script records which
libm and CPU a capture ran on, and an md5 of that libm's results on a fixed input set, so a later
capture can be compared (see docs/platform-numerics.md).

Usage:
  libm-fingerprint.py [--n N] [--json]

  --n N    inputs per function (default 100000). The inputs are generated deterministically
           (splitmix64, fixed seed), so the same N gives the same inputs on every host.
  --json   one JSON object instead of text.

The functions are called through ctypes from the C library itself (log, exp, pow, log2, hypot,
exp2, tanh), not through Python's math module, which reimplements some of them. Results are
hashed as IEEE 754 big-endian doubles. Exit 0, or 2 when the C math library cannot be loaded.
Standard library only.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import platform
import struct
import subprocess
import sys

MASK64 = (1 << 64) - 1


def splitmix64(seed: int):
    state = seed & MASK64
    while True:
        state = (state + 0x9E3779B97F4A7C15) & MASK64
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        yield (z ^ (z >> 31)) & MASK64


def uniform(gen, lo: float, hi: float) -> float:
    """A double in [lo, hi) from 53 random bits; exact and identical on every host."""
    return lo + (next(gen) >> 11) * (1.0 / (1 << 53)) * (hi - lo)


# name, C function, arity, input range(s). Ranges cover where real tools call them: ln of a
# fraction (delay thresholds), ln of a large value, exp of a negative argument (RC decay), pow,
# log2 across magnitudes, hypot of coordinates, exp2 and tanh.
PROBES = [
    ("ln_0_1", "log", 1, [(1e-12, 1.0)]),
    ("ln_wide", "log", 1, [(1.0, 1e12)]),
    ("exp", "exp", 1, [(-50.0, 50.0)]),
    ("pow", "pow", 2, [(0.1, 500.0), (-8.0, 8.0)]),
    ("log2", "log2", 1, [(1e-6, 1e12)]),
    ("hypot", "hypot", 2, [(-1e6, 1e6), (-1e6, 1e6)]),
    ("exp2", "exp2", 1, [(-100.0, 100.0)]),
    ("tanh", "tanh", 1, [(-20.0, 20.0)]),
]


def load_libm():
    names = [ctypes.util.find_library("m"), "libm.so.6", "libm.dylib", None]
    for name in names:
        try:
            lib = ctypes.CDLL(name)
            lib.log  # noqa: B018 - probe that the symbol exists
            return lib, name or "(process)"
        except (OSError, AttributeError):
            continue
    return None, None


def libc_version() -> str:
    try:
        return os.confstr("CS_GNU_LIBC_VERSION")
    except (ValueError, OSError, AttributeError):
        pass
    if sys.platform == "darwin":
        return "Apple libSystem (macOS " + platform.mac_ver()[0] + ")"
    lib, ver = platform.libc_ver()
    return f"{lib} {ver}".strip() or "unknown"


def sysctl(key: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def cpu_info() -> dict:
    info = {"machine": platform.machine(), "model": "", "fma": None, "avx2": None}
    if os.path.exists("/proc/cpuinfo"):
        flags = set()
        with open("/proc/cpuinfo") as f:
            for line in f:
                key, _, val = line.partition(":")
                key = key.strip().lower()
                if key in ("model name", "hardware", "cpu model") and not info["model"]:
                    info["model"] = val.strip()
                elif key in ("flags", "features") and not flags:
                    flags = set(val.split())
        if info["machine"] in ("x86_64", "AMD64", "i686"):
            info["fma"], info["avx2"] = "fma" in flags, "avx2" in flags
        else:
            info["fma"] = True          # aarch64 has fused multiply-add in its baseline
    elif sys.platform == "darwin":
        info["model"] = sysctl("machdep.cpu.brand_string")
        if info["machine"] == "x86_64":
            feats = (sysctl("machdep.cpu.features") + " " + sysctl("machdep.cpu.leaf7_features")).upper().split()
            info["fma"], info["avx2"] = "FMA" in feats, "AVX2" in feats
        else:
            info["fma"] = True          # arm64 has fused multiply-add in its baseline
    return info


def fingerprint(lib, n: int) -> dict:
    out = {}
    total = hashlib.md5()
    for name, cfunc, arity, ranges in PROBES:
        fn = getattr(lib, cfunc)
        fn.restype = ctypes.c_double
        fn.argtypes = [ctypes.c_double] * arity
        gen = splitmix64(0x5EED0000 + sum(map(ord, name)))
        h = hashlib.md5()
        for _ in range(n):
            args = [uniform(gen, lo, hi) for lo, hi in ranges]
            h.update(struct.pack(">d", fn(*args)))
        out[name] = h.hexdigest()
        total.update(h.digest())
    out["all"] = total.hexdigest()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=100000)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    lib, libname = load_libm()
    if lib is None:
        print("libm-fingerprint: cannot load the C math library", file=sys.stderr)
        return 2
    rec = {
        "schema": 1,
        "os": f"{platform.system()} {platform.release()}",
        "libc": libc_version(),
        "libm": libname,
        "glibc_tunables": os.environ.get("GLIBC_TUNABLES", ""),
        "cpu": cpu_info(),
        "n": a.n,
        "md5": fingerprint(lib, a.n),
    }
    if a.json:
        print(json.dumps(rec, sort_keys=True))
        return 0
    cpu = rec["cpu"]
    print(f"os            {rec['os']}")
    print(f"libc          {rec['libc']}  (libm: {rec['libm']})")
    print(f"GLIBC_TUNABLES {rec['glibc_tunables'] or '(unset)'}")
    print(f"cpu           {cpu['machine']}  {cpu['model'] or '(model unknown)'}")
    print(f"cpu fma/avx2  {cpu['fma']} / {cpu['avx2']}")
    print(f"inputs        {a.n} per function")
    for k, v in rec["md5"].items():
        print(f"md5 {k:<10} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
