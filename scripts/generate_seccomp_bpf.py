"""Regenerate the pinned amd64 seccomp BPF for the declared Linux 5.4 baseline.

Requires libseccomp 2.5.3. Usage:
    python scripts/generate_seccomp_bpf.py PROFILE OUT.bpf
"""
import ctypes
import json
import os
import sys

SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = lambda errno: 0x00050000 | errno
OPS = {
    "SCMP_CMP_NE": 1, "SCMP_CMP_LT": 2, "SCMP_CMP_LE": 3,
    "SCMP_CMP_EQ": 4, "SCMP_CMP_GE": 5, "SCMP_CMP_GT": 6,
    "SCMP_CMP_MASKED_EQ": 7,
}

class ArgCmp(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint), ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64),
    ]


class Version(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint), ("minor", ctypes.c_uint),
                ("micro", ctypes.c_uint)]

lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
lib.seccomp_version.restype = ctypes.POINTER(Version)
version = lib.seccomp_version().contents
if (version.major, version.minor, version.micro) != (2, 5, 3):
    raise SystemExit(
        f"libseccomp 2.5.3 required, got {version.major}.{version.minor}.{version.micro}")
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_release.argtypes = [ctypes.c_void_p]
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
lib.seccomp_rule_add_array.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ctypes.POINTER(ArgCmp),
]
lib.seccomp_rule_add_array.restype = ctypes.c_int
lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.seccomp_export_bpf.restype = ctypes.c_int

profile = json.load(open(sys.argv[1]))
ctx = lib.seccomp_init(SCMP_ACT_ERRNO(profile.get("defaultErrnoRet", 1)))
if not ctx:
    raise SystemExit("seccomp_init failed")

def applies(rule):
    inc, exc = rule.get("includes", {}), rule.get("excludes", {})
    if inc.get("arches") and "amd64" not in inc["arches"]:
        return False
    if exc.get("arches") and "amd64" in exc["arches"]:
        return False
    if inc.get("caps"):  # cap-drop=ALL
        return False
    if inc.get("minKernel"):
        required = tuple(map(int, inc["minKernel"].split(".")))
        current = (5, 4)
        if current < required:
            return False
    return True

try:
    for rule in profile["syscalls"]:
        if not applies(rule):
            continue
        action = (SCMP_ACT_ALLOW if rule["action"] == "SCMP_ACT_ALLOW"
                  else SCMP_ACT_ERRNO(rule.get("errnoRet", 1)))
        comparisons = []
        for item in rule.get("args", []):
            comparisons.append(ArgCmp(
                item["index"], OPS[item["op"]], item["value"],
                item.get("valueTwo", 0)))
        array = ((ArgCmp * len(comparisons))(*comparisons)
                 if comparisons else None)
        for name in rule["names"]:
            nr = lib.seccomp_syscall_resolve_name(name.encode())
            if nr < 0:  # future syscall unknown to host libseccomp/kernel: skip
                continue
            rc = lib.seccomp_rule_add_array(ctx, action, nr, len(comparisons), array)
            if rc not in (0, -17):  # duplicate rule can be EEXIST
                raise OSError(-rc, f"seccomp_rule_add_array {name}")
    out = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        rc = lib.seccomp_export_bpf(ctx, out)
        if rc:
            raise OSError(-rc, "seccomp_export_bpf")
    finally:
        os.close(out)
finally:
    lib.seccomp_release(ctx)
