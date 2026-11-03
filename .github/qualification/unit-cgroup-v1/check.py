"""Run one unchanged native unit executable inside the maintained AMD64 guest."""

import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from xml.etree import ElementTree
from zipfile import ZipFile

PRODUCT = "96fadde722e14d482a62276b80237398f8b62fbf"
GUEST = "38745c527784376e0b5d915fe5ebd85e1d1385fd"
SOURCE = os.environ["GITHUB_SHA"]
OWNER = "//runsc/cgroup:cgroup_test"
PACKAGE = ".github/qualification/unit-cgroup-v1"
GUEST_OWNER = f"//{PACKAGE}:guest_test"
# From 156 actual TestCase elements in native AMD64 artifact 13166333421.
CASES = "1dc0ec022386ff7afa8ec8ac88b8cd8e08a1c9d00483622f61a10e0974eec6e6"
HOOK = "399816e7c61acc27168b85a5a4d189e95bf86ea6e2eaaf46ffa17da447c916dd"
CONTROLLERS = set("cpuset cpu cpuacct blkio memory devices freezer net_cls perf_event net_prio hugetlb pids".split())
TMP = Path(os.environ["RUNNER_TEMP"])
RESULT = TMP / "unit-cgroup-v1.json"
record = {"status": "INCOMPLETE", "phase": "bootstrap", "product": PRODUCT, "carrier": SOURCE, "guest_source": GUEST,
          "owner": OWNER, "outer_owner": GUEST_OWNER, "production_hook": "hierarchy detection block only",
          "complete_buildkite_v1_lane": False, "native_exits": {}}
secret = os.environ["BUILDBUDDY_API_KEY"].encode()

sys.path.insert(0, str(Path("test/rbe").resolve()))
from credential_artifacts import prepare
from qualification_report import xml_cases


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write(phase):
    record.update(phase=phase, updated_utc=now())
    data = json.dumps(record, sort_keys=True).encode()
    if not secret or secret in data:
        raise ValueError("private verdict")
    tmp = RESULT.with_suffix(".tmp")
    tmp.write_bytes(data + b"\n")
    tmp.replace(RESULT)
    print(phase, flush=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def command(phase, cmd, **kwargs):
    write(phase)
    with (TMP / f"cgroup-v1-{phase}.log").open("wb") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    record["native_exits"][phase] = proc.returncode
    if proc.returncode:
        contents = (TMP / f"cgroup-v1-{phase}.log").read_bytes().lower()
        markers = {"repo-fetch": b"error downloading", "unavailable": b"unavailable",
                   "authorization": b"unauthenticated", "oom": b"out of memory",
                   "disk-full": b"no space left on device", "package": b"no such package",
                   "missing-input": b"missing input file", "test-failed": b"failed in",
                   "timeout": b"timed out", "permission": b"permission denied"}
        record["failure_markers"] = [key for key, value in markers.items() if value in contents]
    write(phase)
    return proc.returncode


def events(path):
    return [json.loads(line) for line in path.read_bytes().splitlines() if line]


def run(credentials):
    assert git("rev-parse", "HEAD^").strip().decode() == GUEST
    assert git("rev-parse", "HEAD").strip().decode() == SOURCE
    fork_changes = set(git("diff", "--name-only", GUEST, SOURCE).decode().splitlines())
    assert fork_changes == {f"{PACKAGE}/{name}" for name in ("BUILD", "payload.sh", "check.py")} | {
        ".github/workflows/native-unit-amd64-cgroup-v1.yml"}
    assert os.uname().machine == "x86_64"
    assert command("host-kvm", ["sudo", "-n", "sh", "-c", "test -c /dev/kvm && test -r /dev/kvm && test -w /dev/kvm"]) == 0
    git("fetch", "--quiet", "--depth=1", "origin", PRODUCT)
    git("switch", "--quiet", "--detach", PRODUCT)
    assert git("rev-parse", "HEAD").strip().decode() == PRODUCT
    full = Path(".buildkite/hooks/pre-command").read_bytes()
    start = b"  if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then\n"
    assert full.count(start) == 1
    block = full[full.index(start):].split(b"  fi\n", 1)[0] + b"  fi\n"
    assert sha(block) == HOOK
    hook_file = TMP / "native-unit-hook.bash"
    hook_file.write_bytes(block)
    record["hook"] = {"file_sha256": sha(full), "block_sha256": HOOK}
    host_mode = subprocess.check_output(["bash", "-c", 'unset CGROUPV2; source "$1"; echo "$CGROUPV2"', "_", hook_file]).strip().decode()
    assert host_mode == "true"
    assert any(" /sys/fs/cgroup " in line and " - cgroup2 " in line for line in Path("/proc/self/mountinfo").read_text().splitlines())
    record["native_host_mode"] = host_mode
    # --strip=never comes from the test:buildkite config used by the unit job.
    assert "test:buildkite --config=ci --strip=never " in Path(".bazelrc").read_text()
    assert "test:buildkite-unit --config=buildkite --config=unit --test_env=CGROUPV2 " in Path(".bazelrc").read_text()
    assert 'label: ":test_tube: Unit tests (cgroupv1)"\n    command: bazel test --config=buildkite-unit' in Path(".buildkite/pipeline.yaml").read_text()
    version = Path(".bazelversion").read_text().strip()
    assert subprocess.check_output(["bazel", "--version"]).decode().strip() == f"bazel {version}"
    local = ["--config=ci", "--strip=never", "--jobs=4"]
    assert command("original-build", ["bazel", "build", *local, OWNER]) == 0
    files = subprocess.check_output(["bazel", "cquery", *local, "--output=files", OWNER], stderr=subprocess.DEVNULL).decode().splitlines()
    assert len(files) == 1 and Path(files[0]).name == "cgroup_test"
    binary = TMP / "native-cgroup-test"
    shutil.copy(files[0], binary)
    header = binary.read_bytes()
    assert header[:4] == b"\x7fELF" and int.from_bytes(header[18:20], "little") == 62
    record["owner_elf"] = {"sha256": sha(header), "size": len(header)}
    git("switch", "--quiet", "--detach", SOURCE)
    shutil.copy(binary, Path(PACKAGE, "selected-test"))
    assert os.access(Path(PACKAGE, "selected-test"), os.X_OK)
    shutil.copyfile(hook_file, Path(PACKAGE, "hook.bash"))
    rc = TMP / "native-unit-guest.bazelrc"
    rc.write_text(f"common --credential_helper=remote.buildbuddy.io={credentials}/helper\n"
                  "build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io\n"
                  "build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io\n"
                  "build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io\n"
                  "build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/\n")
    guest_bep = TMP / "cgroup-v1-guest.events"
    result = command("guest", ["bazel", f"--bazelrc={rc}", "test", "--config=rbe", "--config=rbe-local-tests",
                               "--//tools/bazeldefs:local_test_architecture=amd64", "--jobs=80",
                               "--zip_undeclared_test_outputs", "--test_timeout=420", "--test_output=errors",
                               f"--build_event_json_file={guest_bep}", GUEST_OWNER])
    record["guest_finished_utc"] = now()
    write("guest-results")
    guest_events = events(guest_bep)
    for e in guest_events:
        if "started" in e and re.fullmatch(r"[0-9a-f-]{36}", e["started"].get("uuid", "")):
            record["guest_bazel_invocation"] = e["started"]["uuid"]
    rows = [e for e in guest_events if "testResult" in e and e["id"]["testResult"]["label"] == GUEST_OWNER]
    record["guest_attempts"] = [{"status": r["testResult"]["status"], "cached_local": bool(r["testResult"].get("cachedLocally")),
                                 "cached_remote": bool(r["testResult"].get("executionInfo", {}).get("cachedRemotely")),
                                 "strategy": r["testResult"].get("executionInfo", {}).get("strategy", "")} for r in rows]
    assert result == 0 and len(rows) == 1
    attempt, = record["guest_attempts"]
    assert attempt["status"] == "PASSED" and not attempt["cached_local"] and not attempt["cached_remote"] and attempt["strategy"] == "local"
    testdir = Path("bazel-testlogs", PACKAGE, "guest_test")
    assert "phase=guest_status_read status=0" in (testdir / "test.log").read_text()
    xml = testdir / "test.xml"
    counts = xml_cases(xml)
    assert (counts["passed"], counts["failed"], counts["skipped"]) == (156, 0, 0)
    names = sorted(e.attrib["name"] for e in ElementTree.parse(xml).iter("testcase"))
    assert sha(("\n".join(names) + "\n").encode()) == CASES
    record["xml"] = {"sha256": sha(xml.read_bytes()), "names_sha256": CASES, "passed": 156, "failed": 0, "skipped": 0}
    with ZipFile(testdir / "test.outputs/outputs.zip") as z:
        def read(name):
            info = z.getinfo(f"guest/outputs/{name}")
            assert info.file_size < 64000
            return z.read(info).decode().splitlines()
        mode, uid, kernel = read("owner-mode")
        assert mode == "false" and int(uid) > 0 and kernel == "6.8.0-138-generic"
        assert read("owner-inputs.sha256") == [HOOK, record["owner_elf"]["sha256"]]
        assert any(" - cgroup2 " in x and " /sys/fs/cgroup " in x for x in read("mounts-before.txt"))
        memberships, controllers = {}, {}
        for line in read("v1-membership.txt"):
            if ":" in line:
                hid, controller, path = line.split(":", 2)
                if controller in CONTROLLERS | {"name=systemd"}:
                    assert path == "/"
                    memberships[controller] = int(hid)
            elif not line.startswith("#"):
                controller, hid, groups, enabled = line.split()
                if controller in CONTROLLERS:
                    assert int(groups) >= 1 and enabled == "1"
                    controllers[controller] = int(hid)
        mounts = {}
        for line in read("v1-mounts.txt"):
            prefix, suffix = line.split(" - ")
            fields = prefix.split()
            kind, dev, opts = suffix.split()
            if kind == "cgroup" and fields[4].startswith("/sys/fs/cgroup/"):
                name = fields[4].removeprefix("/sys/fs/cgroup/")
                controller = "name=systemd" if name == "systemd" else name
                assert fields[3] == "/" and controller in opts.split(",") and controller in CONTROLLERS | {"name=systemd"}
                mounts[controller] = {"mountpoint": fields[4], "fs": kind, "hierarchy": memberships[controller]}
        assert mounts.keys() == memberships.keys() == CONTROLLERS | {"name=systemd"}
        assert controllers.keys() == CONTROLLERS and all(controllers[c] == memberships[c] for c in CONTROLLERS)
        assert len(set(memberships.values())) == 13 and min(memberships.values()) > 0
        record["guest"] = {"native_mode": mode, "kernel": kernel, "owner_uid": int(uid), "mounts": mounts}
    write("product-cache-selection")
    git("switch", "--quiet", "--detach", PRODUCT)
    target_file = TMP / "unit-cgroup-one-target"
    target_file.write_text(OWNER + "\n")
    bep = TMP / "unit-cgroup-cache-selection.events"
    status = command("product-cache-selection", ["bazel", "test", "--config=buildkite-unit", f"--target_pattern_file={target_file}",
                     "--jobs=4", "--nobuild", f"--build_event_json_file={bep}"], env=dict(os.environ, CGROUPV2=mode))
    assert status == 0
    selection = events(bep)
    selected = {e["id"]["targetConfigured"]["label"] for e in selection if "targetConfigured" in e["id"]}
    assert selected == {OWNER}
    testenv = {
        option["optionValue"]
        for e in selection if "structuredCommandLine" in e
        for section in e["structuredCommandLine"].get("sections", [])
        for option in section.get("optionList", {}).get("option", [])
        if option.get("optionName") == "test_env" and "optionValue" in option
    }
    old_options = {value for e in selection if "optionsParsed" in e for value in e["optionsParsed"].get("cmdLine", [])}
    assert "CGROUPV2" in testenv or "--test_env=CGROUPV2" in old_options
    record["product_cache_selection"] = {"actual_guest_value": mode, "test_env": "CGROUPV2",
                                         "configured": sorted(selected), "analysis_only": True}
    record["status"] = "PASS"


if __name__ == "__main__":
    try:
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            prepare(Path(directory))
            del os.environ["BUILDBUDDY_API_KEY"]
            run(directory)
    except Exception as exc:
        record["status"] = "FAIL" if any(record["native_exits"].values()) else "INCOMPLETE"
        record["error_class"] = type(exc).__name__
        write(record["phase"])
        sys.exit(1)
    write("complete")
