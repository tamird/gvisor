"""Compare the public production ELF and the two AMD64 libc declarations."""

import ctypes
import hashlib
import json
import mmap
import os
from pathlib import Path
import subprocess
import time


PAIRS = {name: "__vdso_" + name for name in ("clock_gettime", "getcpu", "gettimeofday", "time")}
NAMES = set(PAIRS) | set(PAIRS.values()) | {"__kernel_rt_sigreturn"}
ROOT = Path(os.environ["VDSO_RECEIPTS"])
RESULT = {"status": "INCOMPLETE", "images": {}}


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def save():
    (ROOT / "symbols.json").write_text(json.dumps(RESULT, sort_keys=True))


def inspect(label, data):
    path = ROOT / (label + ".so")
    check(data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[16:20], "little") == 0x003e0003, "expected AMD64 ET_DYN")
    def readelf(flag):
        return subprocess.check_output(["/usr/bin/readelf", "-W", flag, str(path)], text=True).splitlines()
    loads = [line.split() for line in readelf("-l") if line.strip().startswith("LOAD ")]
    base = int(loads[0][2], 16)
    segments = []
    for _, offset, addr, _, size, memsize, *flags in loads:
        off = int(offset, 16)
        check(int(addr, 16) - base == off and size == memsize and int(size, 16) <= len(data) - off, "load range")
        if "E" in flags:
            segments.append((off, off + int(size, 16)))
    symbols = {}
    for line in readelf("--dyn-syms"):
        cell = line.split()
        if len(cell) != 8 or cell[3] != "FUNC" or cell[6] == "UND":
            continue
        name, sep, version = cell[7].partition("@@")
        check(name in NAMES and sep and version == "LINUX_2.6", "unexpected versioned function")
        off, size = int(cell[1], 16) - base, int(cell[2])
        check(any(lo <= off < off + size <= hi for lo, hi in segments), "function outside executable segment")
        check(cell[4] == ("WEAK" if name in PAIRS else "GLOBAL") and cell[5] == "DEFAULT", "binding or visibility")
        symbols[name] = [off, size, cell[4], version]
    check(set(symbols) == NAMES, "missing exported function")
    for weak, strong in PAIRS.items():
        check(symbols[weak][:2] == symbols[strong][:2], "alias offset or size")
    return symbols


def realtime_fallback(data, symbols):
    # The production linker defines _params at the page before the ELF.
    # An all-zero parameter page selects the real clock_gettime syscall.
    page = mmap.PAGESIZE
    check(page == 4096 and 0 < len(data) <= 256 * 1024, "unexpected image size or page")
    size = (len(data) + page - 1) // page * page
    with mmap.mmap(-1, page + size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                   prot=mmap.PROT_READ | mmap.PROT_WRITE) as image:
        address = ctypes.addressof(ctypes.c_char.from_buffer(image))
        image[page:page + len(data)] = data
        protect = ctypes.CDLL(None, use_errno=True).mprotect
        protect.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
        check(protect(address, page, mmap.PROT_READ) == 0, "readonly parameter page")
        check(protect(address + page, size, mmap.PROT_READ | mmap.PROT_EXEC) == 0, "executable ELF")
        fn = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        for name in ("gettimeofday", "__vdso_gettimeofday"):
            call = fn(address + page + symbols[name][0])
            timeval = (ctypes.c_long * 2)(-1, -1)
            timezone = (ctypes.c_int * 2)(1234, 5678)
            before = time.time_ns() // 1000
            rc = call(ctypes.byref(timeval), ctypes.byref(timezone))
            after = time.time_ns() // 1000
            check(rc == 0 and 0 <= timeval[1] < 1_000_000, "gettimeofday result")
            check(before <= timeval[0] * 1_000_000 + timeval[1] <= after, "real-time syscall bounds")
            check(list(timezone) == [0, 0], "timezone output")


try:
    for label in ("export", "fixed_native", "parent_old_header", "fixed_old_header"):
        RESULT["phase"] = label
        save()
        raw = (ROOT / (label + ".so")).read_bytes()
        symbols = inspect(label, raw)
        item = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "symbols": symbols}
        RESULT["images"][label] = item
        if label == "export":
            check(item["sha256"] == "064981f0ae679a29334db091241c54cb9a1591c84f60713a3bbd616946fadcab", "production export identity")
        realtime_fallback(raw, symbols)
        item["calls"] = ["gettimeofday", "__vdso_gettimeofday"]
        save()
    check(RESULT["images"]["parent_old_header"]["symbols"] == RESULT["images"]["fixed_old_header"]["symbols"], "matched-build offsets or sizes changed")
    RESULT["status"] = "PASS"
except AssertionError as exc:
    RESULT["status"] = "FAIL"
    RESULT["failure"] = str(exc)
    raise
finally:
    save()
