"""Observe Linux inotify identity after one private-VM dentry reclaim."""

import ctypes
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys

OPEN = 0x20
EVENT = struct.Struct("=iIII")
LIBC = ctypes.CDLL(None, use_errno=True)


def check(ret: int) -> int:
    if ret < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return ret


def add(fd: int, path: Path) -> int:
    return check(LIBC.inotify_add_watch(fd, os.fsencode(path), OPEN))


def events(fd: int) -> list[list[int]]:
    seen: list[list[int]] = []
    while True:
        try:
            buf = os.read(fd, 8192)
        except BlockingIOError:
            return seen
        assert buf, "unexpected EOF on inotify descriptor"
        pos = 0
        while pos < len(buf):
            wd, mask, cookie, n = EVENT.unpack_from(buf, pos)
            seen.append([wd, mask, cookie, n])
            pos += EVENT.size + n
        assert pos == len(buf), (pos, len(buf))


def ident(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_dev, st.st_ino]


def opened(path: Path) -> None:
    os.close(os.open(path, os.O_RDONLY | os.O_CLOEXEC))


def run(output: Path) -> None:
    virt = subprocess.check_output(["systemd-detect-virt", "--vm"], text=True).strip()
    assert (os.environ.get("KERNFS_HOSTED") == "github-hosted"
            and os.environ.get("GITHUB_REPOSITORY") == "tamird/gvisor"
            and os.environ.get("GITHUB_REF") == "refs/heads/qualification/kernfs-watched-native-reclaim"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and virt not in ("", "none") and not Path("/.dockerenv").exists()), "not the private hosted VM"
    record = {"verified": False, "runner": "ubuntu-24.04", "virt": virt,
              "kernel": platform.release(), "source": os.environ["GITHUB_SHA"]}
    try:
        fd = check(LIBC.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC))
        alias = os.dup(fd)
        target, control = (Path(f"/proc/self/fdinfo/{n}") for n in (fd, alias))
        wd = add(fd, target)
        assert wd == 1, wd
        opened(control)
        assert events(fd) == [], "unwatched alias produced an event"
        opened(target)
        warm = events(fd)
        expect = [[wd, OPEN, 0, 0]]
        before = [ident(target), ident(control)]
        again = [ident(target), ident(control)]
        warm_wd = add(fd, target)
        record.update(before=before, repeat_before=again, warm_events=warm,
                      warm_wd=wd, warm_readd=warm_wd)
        assert warm == expect and before == again and warm_wd == wd, record
        assert before[0][0] == before[1][0] and before[0][1] != before[1][1], before

        # Only the inotify descriptor and its dup stay open. No fdinfo dentry
        # (nor its parent) is held by this process across the shrink.
        os.sync()
        subprocess.run(["sudo", "-n", "/usr/bin/tee", "/proc/sys/vm/drop_caches"],
                       input=b"2\n", stdout=subprocess.DEVNULL, check=True)
        between = events(fd)
        after = [ident(target), ident(control)]
        opened(target)
        after_events = events(fd)
        new_wd = add(fd, target)
        # If the name now resolves to another inode, does the old watch still exist?
        rm_old = LIBC.inotify_rm_watch(fd, wd)
        old_errno = ctypes.get_errno() if rm_old < 0 else 0
        remove_events = events(fd)
        record.update(after=after, events_during_reclaim=between, reopen_events=after_events,
                      readd=new_wd, remove_old=rm_old, remove_errno=old_errno,
                      remove_events=remove_events)
        if before[1][0] != after[1][0] or before[1][1] == after[1][1]:
            raise RuntimeError("unwatched sibling did not establish eviction")
        assert before[0][0] == after[0][0], (before, after)
        if before[0] == after[0] and not between and after_events == expect and new_wd == wd:
            outcome = "same_inode_and_watch"
        elif before[0] != after[0] and not between and after_events == [] and new_wd > wd:
            outcome = "new_inode_without_old_watch"
        else:
            raise RuntimeError("unexpected combination of observed inode, events and watch descriptor")
        assert rm_old == 0 and remove_events == [[wd, 0x8000, 0, 0]], record
        record.update(outcome=outcome, verified=True)
    finally:
        output.write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
