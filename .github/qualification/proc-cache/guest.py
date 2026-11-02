"""Process inventory inside the restored sandbox. All intervals use the guest clock."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

WORK = Path("/work")
META = WORK / "workers.json"
WORKERS = 16


def wait_json(path):
    limit = time.monotonic() + 12
    while not path.exists():
        if time.monotonic() > limit:
            raise TimeoutError(path.name)
        time.sleep(.01)
    return json.loads(path.read_bytes())


def publish(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


def start():
    # One distinct task's comm stays open across save with its offset at 2.
    workers = [subprocess.Popen(["/usr/bin/sleep", "900"]) for _ in range(WORKERS + 1)]
    pin = workers[0].pid
    fd = os.open(f"/proc/{pin}/comm", os.O_RDONLY)
    first = os.read(fd, 2)
    assert first == b"sl", (pin, first)

    def verify_fd(*_):
        suffix = os.read(fd, 32)
        publish(WORK / "pin.json", [os.fstat(fd).st_ino, suffix.decode()])

    signal.signal(signal.SIGUSR1, verify_fd)
    publish(META, {"pin": pin, "pin_ino": os.fstat(fd).st_ino, "pids": [p.pid for p in workers[1:]]})
    while True:
        signal.pause()


def ps_scan(pids):
    start_ns = time.perf_counter_ns()
    completed = subprocess.run(["/usr/bin/ps", "-q", ",".join(map(str, pids)), "-o", "pid=,comm="],
                               capture_output=True, text=True, check=True)
    elapsed = time.perf_counter_ns() - start_ns
    rows = [line.split() for line in completed.stdout.splitlines()]
    assert sorted((int(pid), name) for pid, name in rows) == sorted((pid, "sleep") for pid in pids), rows
    return elapsed


def cpu_scan():
    start_ns = time.perf_counter_ns()
    base = Path("/sys/devices/system/cpu")
    online = (base / "online").read_text().strip()
    masks = [(p.parent.parent.name, p.read_text().strip()) for p in sorted(base.glob("cpu[0-9]*/topology/thread_siblings"))]
    elapsed = time.perf_counter_ns() - start_ns
    assert masks and online and all(int(v.replace(",", ""), 16) > 0 for _, v in masks), (online, masks)
    return {"ns": elapsed, "online": online, "masks": masks}


def sample(phase):
    meta = wait_json(META)
    pids = meta["pids"]
    assert len(set(pids)) == WORKERS and meta["pin"] not in pids
    result = {"meta": meta}
    if phase != "quiet":
        # Read the inode numbers after the real ps run so this observation
        # cannot pre-populate the first post-restore process inventory.
        result["ps_ns"] = [ps_scan(pids), ps_scan(pids)]
        result["stat_inos"] = [os.stat(f"/proc/{pid}/stat").st_ino for pid in pids]
        result["cpu"] = [cpu_scan(), cpu_scan()]
    if phase == "restored":
        os.kill(1, signal.SIGUSR1)
        result["pinned_fd"] = wait_json(WORK / "pin.json")
        assert result["pinned_fd"] == [meta["pin_ino"], "eep\n"], result
    print(json.dumps(result))


if sys.argv[1] == "init":
    start()
else:
    sample(sys.argv[1])
