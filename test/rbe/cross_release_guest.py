"""Check the transferred ARM64 release and boot its Sentry in a private root."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import runtime_guest as guest

SOURCE = "61ff148f0df0efa29c9f500914477525c5debe62"
PRODUCER = "dd2be7670c1baaacebfdcab31bb6145ebce39037"
INVOCATION = "d9b43462-e952-48ce-9e40-d22437114368"
MANIFEST = "31bdb511152716c0bf87d2b6b127b2dab0fa92f707be03437cfc841dcd15db85"
TARGET = "//test/rbe:cross_built_release_guest"
BUSYBOX = "52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933"
MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/"+s for s in (
    "gvisor_sentry", "checkpointgofer", "gvisor-sentry-prewarmer", "runsc-fd-parking",
    "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter")}
SENTRY = "gvisor-bin/gvisor_sentry"
PAYLOAD = Path("test/rbe/payload")
WORK = Path(os.environ["RUNNER_TEMP"]) / "cross-native-guest"


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(directory, expected, archive):
    assert expected.keys() == MEMBERS
    assert sum(f["bytes"] for f in expected.values()) < 400 << 20
    got = {}
    with tarfile.open(archive, "r|gz") as tar:
        for member in tar:
            name = member.name
            assert name in MEMBERS and name not in got and member.isreg() and member.mode == 0o755
            assert 0 < member.size == expected[name]["bytes"] < 160 << 20
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as target:
                with tar.extractfile(member) as stream:
                    shutil.copyfileobj(stream, target)
            path.chmod(0o755)
            with path.open("rb") as stream:
                hdr = stream.read(64)
            assert hdr[:6] == b"\x7fELF\x02\x01" and int.from_bytes(hdr[18:20], "little") == 183
            got[name] = digest(path)
            assert got[name] == expected[name]["sha256"]
    assert got.keys() == MEMBERS
    return got


def fetch_fixture():
    WORK.mkdir(exist_ok=True)
    guest.q.TASK = guest.r.TASK = WORK
    row = {}
    path = guest.fixture(row, "arm64")
    assert digest(path) == BUSYBOX
    shutil.copy2(path, PAYLOAD / "busybox")


def execute(row):
    assert not {"BUILDBUDDY_API_KEY", "ACCESS", "GITHUB_TOKEN", "GH_TOKEN"}.intersection(os.environ)
    assert os.geteuid() != 0 and platform.machine() == "aarch64"
    inputs = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / PAYLOAD
    archive = inputs / "release.tar.gz"
    metadata = inputs / "manifest.json"
    assert metadata.stat().st_size < 16384 and digest(metadata) == MANIFEST
    files = json.loads(metadata.read_text())
    row.update(stage="install", producerSource=PRODUCER, producerInvocation=INVOCATION,
               archiveSHA256=digest(archive), archiveBytes=archive.stat().st_size,
               manifestSHA256=MANIFEST, nativeCPU=platform.machine(), nativeUID=os.getuid())
    WORK.mkdir()
    installed = WORK / "installed"
    row.update(installed=install(installed, files, archive), stage="guest")
    box = WORK / "busybox"
    shutil.copy2(inputs / "busybox", box)
    assert digest(box) == BUSYBOX
    guest.r.TASK = guest.q.TASK = WORK
    guest.r.SOURCE = SOURCE
    guest.MEMBERS = MEMBERS
    pivot = Path("/usr/sbin/pivot_root")
    assert pivot.is_file() and os.access(pivot, os.X_OK)
    row["stage"] = "guest"
    capsule, marker = guest.capsule("capsule", box, installed)
    assert all((capsule/"release"/name).samefile(installed/name) for name in MEMBERS)
    cfg = capsule / "bundle/config.json"
    spec = json.loads(cfg.read_text())
    spec["process"]["args"] = ["/busybox", "sh", "-ec",
                               '/busybox echo "$1"; exec /busybox uname -m', "guest", marker]
    cfg.write_text(json.dumps(spec))
    smoke = {}
    result = guest.launch(capsule, smoke, pivot=str(pivot), default_release_policy=True)
    criteria = ("private_root_entered", "sentry_selected", "sentry_development_label", "sentry_stdio_witness")
    row["guest"] = {key: smoke.get(key) for key in (*criteria, "exit", "stdout_sha256", "prewarmer_selected",
                                                    "setupStages", "startupFailures")}
    row["guest"]["sameSentryInode"] = (capsule/"release"/SENTRY).samefile(installed/SENTRY)
    row["guest"]["cpu"] = "aarch64" if result.splitlines()[-1:] == [b"aarch64"] else None
    row["guest"]["marker"] = marker if result.splitlines()[:1] == [marker.encode()] else None
    diag = " ".join(smoke.get("stderr", {}).get("lines", [])).lower()
    row["guest"]["diagnostics"] = [term for term in ("not permitted", "permission denied", "no such file",
                                   "exec format error", "timed out", "unknown flag", "release version")
                                   if term in diag]
    row["verified"] = bool(smoke.get("exit") == 0 and all(smoke.get(k) for k in criteria)
                           and row["guest"]["sameSentryInode"] and result.splitlines() == [marker.encode(), b"aarch64"])
    assert row["verified"]
    row["stage"] = "complete"


def run_test():
    row = {"source": SOURCE, "verified": False, "stage": "input"}
    try:
        execute(row)
    except (OSError, ValueError, AssertionError, KeyError, tarfile.TarError) as exc:
        row["errorType"] = type(exc).__name__
        raise
    finally:
        print("CROSS_RELEASE_GUEST " + json.dumps(row, separators=(",", ":")), flush=True)


def collect():
    row = {"source": SOURCE, "target": TARGET, "verified": False, "attempts": [], "compilerActionCounts": {}, "nativeRuntimeActions": []}
    try:
        events = WORK/"guest.bep"
        if not events.is_file():
            return False
        with events.open() as stream:
            for line in stream:
                e = json.loads(line)
                mnemonic = e.get("action", {}).get("type")
                if mnemonic in {"GoCompilePkg", "GoLink", "CppCompile", "CppLink"}:
                    counts = row["compilerActionCounts"]
                    counts[mnemonic] = counts.get(mnemonic, 0) + 1
                    label = e["id"].get("actionCompleted", {}).get("label", "").lstrip("@")
                    if label in {"//runsc:runsc", "//runsc/cmd/sentry:gvisor_sentry"}:
                        row["nativeRuntimeActions"].append([label, mnemonic])
                if "finished" in e:
                    row["bazelExit"] = e["finished"].get("exitCode", {}).get("code", 0)
                if "testResult" not in e or e["id"]["testResult"]["label"] != TARGET:
                    continue
                result = e["testResult"]
                rec = {"status": result.get("status"), "cached": bool(result.get("cachedLocally")),
                       "remoteCached": bool(result.get("executionInfo", {}).get("cachedRemotely")),
                       "strategy": result.get("executionInfo", {}).get("strategy")}
                row["attempts"].append(rec)
                for item in result.get("testActionOutput", []):
                    name = item["name"]
                    if name not in ("test.log", "test.xml"):
                        continue
                    url = urlsplit(item["uri"])
                    assert url.scheme == "file" and not url.netloc
                    path = Path(unquote(url.path)).resolve(strict=True)
                    assert path == (Path("bazel-testlogs/test/rbe/cross_built_release_guest")/name).resolve(strict=True)
                    assert path.stat().st_size < 2 << 20
                    sha = digest(path)
                    assert not item.get("digest") or item["digest"] == sha
                    rec[name] = sha
                    if name == "test.log":
                        markers = re.findall(r"(?m)^CROSS_RELEASE_GUEST (\{[^\n]*\})$", path.read_text(errors="replace"))
                        if len(markers) == 1:
                            rec["guestReceipt"] = json.loads(markers[0])
                    else:
                        xml = ET.parse(path)
                        rec["xmlHasFailure"] = any(el.tag in {"failure", "error", "skipped"} for el in xml.iter())
        if len(row["attempts"]) == 1:
            rec, = row["attempts"]
            row["verified"] = bool(row.get("bazelExit") == 0 and not row["nativeRuntimeActions"] and
                                   rec["status"] == "PASSED" and rec["strategy"] == "local" and not rec["cached"] and
                                   not rec["remoteCached"] and rec.get("xmlHasFailure") is False
                                   and rec.get("guestReceipt", {}).get("verified"))
        return row["verified"]
    finally:
        (WORK/"native.json").write_text(json.dumps(row, indent=2) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["fixture"]:
        fetch_fixture()
    elif sys.argv[1:] == ["guest"]:
        run_test()
    elif sys.argv[1:] == ["collect"]:
        raise SystemExit(not collect())
    else:
        raise SystemExit(2)
