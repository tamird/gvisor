"""Verify a Bazel symlink with the coordinator's path-to-host mount mapping."""

import hashlib
import os
from pathlib import Path
import stat


def observe(link, canonical, coordinator_home, host_home):
    def samefile(a, b):
        return a.exists() and b.exists() and a.samefile(b)

    record = {"present": os.path.lexists(link), "direct_samefile": samefile(link, canonical),
              "mapped_samefile": False}
    if not record["present"]:
        return record
    mode = link.lstat().st_mode
    record["kind"] = "symlink" if stat.S_ISLNK(mode) else "regular" if stat.S_ISREG(mode) else "other"
    if record["kind"] != "symlink":
        return record
    target = Path(os.readlink(link))
    if len(str(target)) > 640:
        raise ValueError("unexpected archive link target length")
    record["raw_target"] = str(target)
    record["target_kind"] = "absolute" if target.is_absolute() else "relative"
    mapped = link
    for _ in range(10):
        if not mapped.is_symlink():
            break
        raw = Path(os.readlink(mapped))
        if raw.is_absolute():
            if not raw.is_relative_to(coordinator_home):
                record["unmapped_absolute"] = True
                return record
            mapped = host_home / raw.relative_to(coordinator_home)
        else:
            mapped = Path(os.path.normpath(mapped.parent / raw))
    else:
        raise ValueError("too many generated archive links")
    record["mapped_samefile"] = samefile(mapped, canonical)
    if mapped.is_file():
        with mapped.open("rb") as src:
            checksum = hashlib.sha256()
            while block := src.read(1024 * 1024):
                checksum.update(block)
            record["mapped_sha256"] = checksum.hexdigest()
    return record
