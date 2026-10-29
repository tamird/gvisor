"""Check actual Linux epoll registration on a proc file and two overlay mounts."""
import ctypes
import errno
import json
import os
from pathlib import Path
import select
import socket
import stat
import tempfile

libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]


def mount(kind, target, data=None):
    if libc.mount(kind, os.fsencode(target), kind, 0, data) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def umount(target):
    if libc.umount2(os.fsencode(target), 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def epoll_fd(path, name=None):
    result = {}
    try:
        with path.open("rb", buffering=0) as f, select.epoll() as ep:
            assert stat.S_ISREG(os.fstat(f.fileno()).st_mode)
            try:
                ep.register(f, select.EPOLLPRI | select.EPOLLET)
            except OSError as exc:
                return {"add_errno": exc.errno}
            result["add_errno"] = 0
            ep.poll(0)
            if name:
                socket.sethostname(name)
                result["priority_event"] = any(fd == f.fileno() and mask & select.EPOLLPRI for fd, mask in ep.poll(0))
            ep.unregister(f)
            if name:
                socket.sethostname(name + b"-after")
            result["events_after_del"] = len(ep.poll(0))
    except OSError as exc:
        result["open_errno"] = exc.errno
    return result


def check_overlay(base, lower, relative, name):
    work, upper, merged = (base / n for n in ("work", "upper", "merged"))
    for p in (work, upper, merged):
        p.mkdir()
    options = f"lowerdir={lower},upperdir={upper},workdir={work}".encode()
    try:
        mount(b"overlay", merged, options)
    except OSError as exc:
        return {"mount_errno": exc.errno}
    try:
        mountinfo = [line.split(" - ", 1)[1].split()[0] for line in Path("/proc/self/mountinfo").read_text().splitlines() if line.split()[4] == str(merged)]
        assert mountinfo == ["overlay"], mountinfo
        result = {"filesystem": "overlay", **epoll_fd(merged / relative, name)}
        if relative == "file":
            os.mkfifo(lower / "fifo")
            fd = os.open(merged / "fifo", os.O_RDWR | os.O_NONBLOCK)
            try:
                with select.epoll() as ep:
                    ep.register(fd, select.EPOLLIN)
                    assert ep.poll(0) == []
                    assert os.write(fd, b"x") == 1
                    assert ep.poll(0) == [(fd, select.EPOLLIN)]
                result["fifo"] = "EPOLLIN"
            finally:
                os.close(fd)
        return result
    finally:
        umount(merged)


def main():
    if os.geteuid() or os.readlink("/proc/self/ns/uts") == os.readlink("/proc/1/ns/uts"):
        raise RuntimeError("the control requires root in its own UTS namespace")
    with tempfile.TemporaryDirectory() as path:
        base = Path(path)
        mount(b"tmpfs", base)
        try:
            result = {"kernel": os.uname().release}
            proc = Path("/proc/sys/kernel")
            result["direct_proc"] = epoll_fd(proc / "hostname", b"overlay-direct")
            lower = base / "regular-lower"
            lower.mkdir()
            (lower / "file").touch()
            for sub, source, file, host in (("regular", lower, "file", None), ("proc", proc, "hostname", b"overlay-layer")):
                layer = base / sub
                layer.mkdir()
                result[sub] = check_overlay(layer, source, file, host)
        finally:
            umount(base)
    print("LINUX_OVERLAY_POLL " + json.dumps(result, sort_keys=True), flush=True)
    assert result["direct_proc"] == {"add_errno": 0, "priority_event": True, "events_after_del": 0}, result
    assert result["regular"] == {"filesystem": "overlay", "add_errno": errno.EPERM, "fifo": "EPOLLIN"}, result


if __name__ == "__main__":
    main()
