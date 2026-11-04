"""Retain a direct July runsc build's target, ELF or first Bazel failure."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from urllib.parse import unquote, urlsplit


STOCK = "9f3e9a3fba9416ea1f5832d82923d95830479096"
FIXED = "9ffcd1079387ea27430c721bba4014782257a6f1"
OWNER = "//runsc:runsc"
TREES = {STOCK: "180da1c9cc1d04dc7e660c0a01dea54165171cb7",
         FIXED: "99656f8e2284e943cb8a96b41200cf1d4c2bc9f2"}
BLOBS = {STOCK: ("fa7fd56cb2bfc4b6f46e795cdac36587a0af37af", "1c29fb79df3712526d68daee49910b70fe2f2473"),
         FIXED: ("82a9180fdd347b508a08b0223321aed5e964ec0d", "a7f90b0d658b4d5a9e47047cb3c3940ab2309aee")}
ROOT = Path(sys.argv[1]).resolve(strict=True)
RECEIPT = ROOT / "result.json"
BAZEL = [str(ROOT / "bazel"), "--nosystem_rc", "--nohome_rc"]
# Based on 334abcfd4's native build failure projection. Never export raw command lines or logs.
FORBIDDEN = re.compile(r"(?i)authorization|credential|password|secret|access.token|api.key|gh[pousr]_|github_pat_|private key|bearer\s")
DIAGNOSTIC = re.compile(r"(?i)^\s*(?:ERROR:|FATAL:|Error in |WARNING: Download)|(?:\.go|\.bzl|\.[ch]c?|BUILD(?:\.bazel)?):\d+|(?:compilepkg|cc1|gcc|g\+\+|clang(?:\+\+)?|ld(?:\.lld)?|collect2|link|cgo):|fatal error|undefined reference|permission denied|no such file|cannot (?:find|open)|not found|no space left|unable to|failed to")
LABEL = re.compile(r"^[@+A-Za-z0-9_./:~-]{1,240}$")


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save(record):
    temporary = RECEIPT.with_suffix(".new")
    temporary.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    temporary.replace(RECEIPT)


def diagnostics(data):
    lines = []
    for line in data.splitlines():
        if not DIAGNOSTIC.search(line) or FORBIDDEN.search(line):
            continue
        line = re.sub(r"(?i)(?:[a-z][a-z0-9+.-]*://|git@)\S+", "<url>", line)
        line = re.sub(r"(error executing \w+ command)\b.*", r"\1", line)
        if re.search(r"\b[A-Za-z_]\w*=|\$[({]|--[A-Za-z]|/bin/(?:ba)?sh -c|\bexec env\b", line) or not line.isprintable():
            continue
        for path, name in ((Path.cwd(), "<workspace>"), (ROOT.parent, "<tmp>"), (Path.home(), "<home>")):
            line = line.replace(str(path), name)
        if line[:420] not in lines:
            lines.append(line[:420])
        if len(lines) == 6:
            break
    return lines


def local_file(file, output_base):
    uri = urlsplit(file.get("uri", ""))
    if uri.scheme == "file" and not uri.netloc and output_base:
        path = Path(unquote(uri.path)).resolve()
        if path.is_relative_to(output_base) and path.is_file():
            return path
    return None


def token(value):
    return value if isinstance(value, str) and LABEL.fullmatch(value) else None


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(source, arm, result):
    paths = ["pkg/tcpip/stack/" + f for f in ("bridge.go", "bridge_test.go")]
    subprocess.run(["git", "switch", "--detach", "--quiet", source], check=True)
    tree = git("rev-parse", "HEAD^{tree}")
    blobs = [git("rev-parse", "HEAD:" + path) for path in paths]
    assert tree == TREES[source] and tuple(blobs) == BLOBS[source] and not git("status", "--porcelain", "--untracked-files=no")
    assert Path(".bazelversion").read_text().strip() == "8.3.1"
    row = {"source": source, "tree": tree, "bridge_blobs": blobs, "stamp": git("describe", "--always", "--tags", "--abbrev=12", "--dirty"), "owner": OWNER, "status": "INCOMPLETE", "started_at": now()}
    result["arms"][arm] = row
    save(result)
    version = subprocess.check_output([BAZEL[0], "--version"], text=True, timeout=180).strip()
    row["bazel"] = version
    assert version == "bazel 8.3.1"
    # Bazel's canonical output base; do not follow arbitrary BEP file:// locations.
    info = subprocess.run(BAZEL + ["info", "output_base"], capture_output=True, text=True, timeout=120)
    output_base = Path(info.stdout.strip()).resolve() if info.returncode == 0 else None
    if output_base:
        assert output_base.is_relative_to(Path.home() / ".cache/bazel")
    events, console = ROOT / (arm + ".events"), ROOT / (arm + ".log")
    options = ["--config=x86_64", "-c", "opt", "--jobs=4", "--color=no", "--curses=no", "--noshow_progress",
               "--build_event_publish_all_actions", "--build_event_json_file=" + str(events), OWNER]
    with console.open("wb") as log:
        rc = subprocess.run(["timeout", "-k", "20", "1440" if arm == "stock" else "780",
                             *BAZEL, "build", *options], stdout=log, stderr=subprocess.STDOUT).returncode
    row.update(native_exit=rc, command_finished_at=now(), status="FAIL" if rc not in (0, 124, 137) else "INCOMPLETE")
    save(result)
    if rc:
        with console.open("rb") as f:
            row["console_diagnostic"] = diagnostics(f.read(524288).decode(errors="replace"))
        save(result)
    sets, defaults, files = {}, [], []
    for raw in events.open() if events.exists() else ():
        event = json.loads(raw)
        key = event.get("id", {})
        if "started" in event:
            row["invocation"] = token(event["started"].get("uuid"))
        if "finished" in event:
            row["bazel_exit"] = token(event["finished"].get("exitCode", {}).get("name"))
        if "aborted" in event and not row.get("aborted_diagnostic"):
            row["aborted_diagnostic"] = diagnostics(event["aborted"].get("description", ""))
        if "namedSetOfFiles" in event:
            sets[key["namedSet"]["id"]] = event["namedSetOfFiles"]
        if key.get("targetCompleted", {}).get("label") == OWNER and not key["targetCompleted"].get("aspect"):
            complete = event.get("completed", {})
            row["target_completed"] = complete.get("success", False)
            defaults.extend(f["id"] for g in complete.get("outputGroup", []) if g["name"] == "default" for f in g.get("fileSets", []))
            files.extend(complete.get("importantOutput", []))
        action = event.get("action")
        if not action or action.get("success") or row.get("failed_action"):
            continue
        identity = key.get("actionCompleted", {})
        argv = action.get("commandLine", [])
        detail = action.get("failureDetail", {})
        failure = {"owner": token(identity.get("label") or action.get("label")), "mnemonic": token(action.get("type")),
                   "primary_output": token(identity.get("primaryOutput")), "exit": action.get("exitCode"),
                   "tool": token(Path(argv[0]).name) if argv else None,
                   "codes": {k: token(v.get("code")) for k,v in detail.items() if token(k) and isinstance(v,dict)},
                   "diagnostic": diagnostics(detail.get("message", ""))}
        row["failed_action"] = failure
        save(result)
        projected = action.get("stderr", {})
        stderr_path = local_file(projected, output_base)
        if stderr_path:
            with stderr_path.open("rb") as f:
                failure["stderr"] = diagnostics(f.read(65536).decode(errors="replace"))
        elif len(projected.get("contents", "")) in range(1, 87385):
            failure["stderr"] = diagnostics(base64.b64decode(projected["contents"]).decode(errors="replace"))
        save(result)
    save(result)
    if rc:
        return False
    seen = set()
    while defaults:
        name = defaults.pop()
        if name not in seen:
            seen.add(name)
            group = sets[name]
            files.extend(group.get("files", []))
            defaults.extend(child["id"] for child in group.get("fileSets", []))
    outputs = {local_file(f, output_base) for f in files if Path(f.get("name", "")).name == "runsc"}
    outputs.discard(None)
    assert row.get("target_completed") and row.get("invocation") and row.get("bazel_exit") == "SUCCESS" and len(outputs) == 1
    path, = outputs
    with path.open("rb") as f:
        header = f.read(64)
    assert header[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", header, 18)[0] == 62
    assert os.access(path, os.X_OK) and 1024 * 1024 < path.stat().st_size < 200 * 1024**2
    sha = hash_file(path)
    dest = ROOT / "binaries" / (arm + "-runsc")
    shutil.copyfile(path, dest)
    assert hash_file(dest) == sha
    dest.chmod(0o755)
    row.update(status="PASS", elf={"machine": 62, "bytes": path.stat().st_size, "sha256": sha}, verified_at=now())
    save(result)
    return True


def main():
    assert os.getuid() != 0 and os.uname().machine == "x86_64"
    assert not os.access("/var/run/docker.sock", os.R_OK | os.W_OK)
    assert int(Path("/proc/self/status").read_text().split("NoNewPrivs:")[1].split()[0]) == 1
    assert os.getpid() == 1   # A private PID namespace, not the host's process view.
    result = {"scope": "native Ubuntu source build only; no tests or network fixtures", "status": "INCOMPLETE",
              "patch": "d4be3ed283a89407bdaba53c43194f7dc908744f", "arms": {},
              "ubuntu": Path("/etc/os-release").read_text().split('VERSION_ID="')[1].split('"')[0]}
    save(result)
    try:
        assert git("rev-parse", FIXED + "^") == STOCK
        assert git("diff", "--name-only", STOCK, FIXED).splitlines() == ["pkg/tcpip/stack/bridge.go", "pkg/tcpip/stack/bridge_test.go"]
        for source, arm in ((STOCK, "stock"), (FIXED, "fixed")):
            if not build(source, arm, result):
                result["status"] = result["arms"][arm]["status"]
                return 1
        assert result["arms"]["stock"]["elf"]["sha256"] != result["arms"]["fixed"]["elf"]["sha256"]
        result["status"] = "PASS"
        return 0
    except Exception as exc:
        result["collector_exception"] = type(exc).__name__
        frame = exc.__traceback__
        while frame and frame.tb_next:
            frame = frame.tb_next
        if frame:
            result["exception_line"] = frame.tb_lineno
        return 1
    finally:
        result["verified_at"] = now()
        save(result)


if __name__ == "__main__":
    raise SystemExit(main())
