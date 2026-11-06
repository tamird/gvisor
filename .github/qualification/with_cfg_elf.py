"""Inspect the linked executable, coverage counters and Go race symbols."""
import hashlib
import struct
import subprocess


def inspect_elf(path):
    with path.open("rb") as binary:
        header = binary.read(64)
        if len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01":
            return {"elf64_le": False}
        kind, machine = struct.unpack_from("<HH", header, 16)
        offset, = struct.unpack_from("<Q", header, 32)
        entry_size, count = struct.unpack_from("<HH", header, 54)
        size = path.stat().st_size
        if entry_size != 56 or not 0 < count < 1024 or offset + count * entry_size > size:
            return {"valid_program_header_table": False}
        binary.seek(offset)
        segments = [struct.unpack("<IIQQQQQQ", binary.read(entry_size)) for _ in range(count)]
        executable = any(t == 1 and flags & 1 and length > 0 and begin + length <= size
                         for t, flags, begin, _, _, length, _, _ in segments)
        binary.seek(0)
        digest = hashlib.sha256()
        for block in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(block)
    result = {"elf64_le": True, "type": kind, "machine": machine, "exec_load": executable,
              "mode": path.stat().st_mode & 0o777, "size": size, "sha256": digest.hexdigest()}
    # Retain symbol evidence separately from the target's successful Go link.
    nm = subprocess.Popen(["nm", "--defined-only", str(path)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    coverage = race = 0
    for line in nm.stdout:
        name = line.rsplit(None, 1)[-1].strip() if line.strip() else b""
        if b"goCover_" in name or b".GoCover_" in name:
            coverage += 1
        if name in (b"__tsan_init", b"runtime.raceinit"):
            race += 1
    result.update(nm_exit=nm.wait(), go_cover_symbols=coverage, race_runtime_symbols=race)
    return result
