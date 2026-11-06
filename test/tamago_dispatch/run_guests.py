"""Observe queue and owner progress in single-CPU, networkless TamaGo guests."""

import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time


def boot(kernel, serial):
    cmd = [
        "/usr/bin/qemu-system-x86_64",
        "-machine", "microvm,x-option-roms=on,pit=off,pic=off,rtc=on",
        "-enable-kvm", "-cpu", "host,invtsc=on,kvmclock=on", "-smp", "1",
        "-global", "virtio-mmio.force-legacy=false", "-m", "4G",
        "-nographic", "-monitor", "none", "-serial", "stdio", "-nic", "none",
        "-no-reboot", "-kernel", str(kernel),
    ]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as vm:
        try:
            return observe(vm, serial)
        finally:
            if vm.poll() is None:
                vm.terminate()
                try:
                    vm.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    vm.kill()


def observe(vm, serial):
    boot_deadline = time.monotonic() + 60
    peer_seen = None
    pending = b""
    events = []
    version = None
    with selectors.DefaultSelector() as selector:
        selector.register(vm.stdout, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            if peer_seen is not None and now - peer_seen >= 5:
                if vm.poll() is not None:
                    raise RuntimeError("VM exited during the observation window")
                if "contended OWNER" in events:
                    raise RuntimeError("the owner resumed but did not finish")
                outcome = "owner did not resume in five seconds"
                break
            if now >= boot_deadline:
                raise RuntimeError(f"boot or control timed out: {events}")
            if not selector.select(timeout=0.1):
                continue
            chunk = os.read(vm.stdout.fileno(), 4096)
            if not chunk:
                raise RuntimeError(f"VM exited before the trial completed: {events}")
            serial.write(chunk)
            serial.flush()
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            for raw in lines:
                line = raw.rstrip(b"\r").decode(errors="replace")
                if "contended DONE" not in events and any(
                    text in line.lower() for text in ("exception: vector", "fatal error:", "panic:", "halting")
                ):
                    raise RuntimeError(f"guest failed: {line}")
                # SeaBIOS can hand off the UART in the middle of its boot line.
                _, banner, target = line.partition("DISPATCH BOOT ")
                if banner:
                    if version is not None or not re.fullmatch(r"tamago amd64 .*1\.27\.3.* 1", target):
                        raise RuntimeError(f"unexpected target or CPU count: {line}")
                    version = banner + target
                elif line.startswith("DISPATCH "):
                    event = line.removeprefix("DISPATCH ")
                    events.append(event)
                    if event == "contended PEER":
                        peer_seen = time.monotonic()
            if "contended DONE" in events:
                outcome = "both listeners and owner finished"
                break
    expected = ["control FIRST", "control PEER", "control OWNER", "control DONE", "contended PEER"]
    if outcome == "both listeners and owner finished":
        expected += ["contended OWNER", "contended FIRST", "contended DONE"]
    if version is None or events != expected:
        raise RuntimeError(f"unexpected VM events: {version}, {events}")
    return {"runtime": version, "events": events, "outcome": outcome}


def main():
    kernels = dict(zip(("stock", "fixed"), map(Path, sys.argv[1:]), strict=True))
    out = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    results = []
    for i, arm in enumerate(("stock", "fixed", "fixed", "stock", "stock", "fixed"), 1):
        path = out / f"{i}-{arm}.serial"
        try:
            with path.open("wb") as stream:
                row = boot(kernels[arm], stream)
        except Exception:
            print(path.read_text(errors="replace")[-3000:], flush=True)
            raise
        row.update(arm=arm, trial=i)
        results.append(row)
        print(json.dumps(row), flush=True)
        (out / "trials.json").write_text(json.dumps(results, indent=2) + "\n")
    for row in results:
        completed = row["outcome"] == "both listeners and owner finished"
        if completed != (row["arm"] == "fixed"):
            raise RuntimeError("observed result differs from prediction; see trials.json")


if __name__ == "__main__":
    main()
