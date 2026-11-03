"""Observe the outstanding native coretag topology using the qualified v1 runner."""

from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from xml.etree import ElementTree
from zipfile import ZipFile

import cas_inputs
import unit_v1 as v1

ELF = "5edd7fcc9d5ad36d8d3c0063d4fa4daa0152b38c6f89bb21a5a2bb1f5f700ca7"
LOG = "7277242acda914426e221b9d4fe846df0e76d5e61e258fe2f3b61efd500e08e1"
INVOCATION = "654ed628-99f5-4216-8668-6eab1a7682cf"


def topology(data: bytes) -> dict:
    lines = data.decode(errors="replace").splitlines()
    siblings = [dict(cpu=int(match[1]), mask=match[2]) for line in lines
                if (match := re.fullmatch(r"Guest CPU ([01]) thread siblings: ([0-9,-]{1,64})", line))]
    vendors = [match[1] for line in lines
               if (match := re.fullmatch(r"Guest CPU vendor: (AuthenticAMD|GenuineIntel)", line))]
    online = [match[1] for line in lines
              if (match := re.fullmatch(r"Guest CPUs online: ([0-9,-]{1,64})", line))]
    features = [match[1] == "true" for line in lines
                if (match := re.fullmatch(r"Guest topoext: (true|false)", line))]
    return dict(sha256=v1.sha(data), bytes=len(data), siblings=siblings, vendors=vendors,
                online=online, topoext=features, config_sched_core="CONFIG_SCHED_CORE=y" in lines,
                test_enable_started="=== RUN   TestEnable" in lines,
                topology_rejected="Guest CPUs are not SMT siblings" in lines,
                amd_hyperthreading_warning=any("This family of AMD CPU doesn't support hyperthreading" in line for line in lines),
                topoext_recommendation=any("Please configure -smp options properly or try enabling topoext feature" in line for line in lines),
                guest_completed="phase=guest_status_read status=0" in data.decode(errors="replace"))


def original(row: dict) -> None:
    raw = (v1.TMP / "original.zip").read_bytes()
    assert v1.sha(raw) == "9306c56cdfd525fb1038012e3436ffb016109747f2f746fd2e0cee42f09f842b"
    with ZipFile(BytesIO(raw)) as archive:
        assert archive.namelist() == ["verdict.json"]
        old = json.loads(archive.read("verdict.json"))
    assert old["carrier"] == "e63f7214f134d91caf93d58d0047582e2a502612" and old["product"] == v1.PRODUCT
    assert old["status"] == "ORIGINAL_FAIL_DIAGNOSTICS_RECORDED"
    prior = old["original"]["phases"]["coretag_native_kvm"]
    assert prior["invocation"] == INVOCATION and prior["native_exit"] == 3
    assert prior["original_elf"] == dict(sha256=ELF, size=6675876, owner=v1.CORETAG)
    row.update(artifact=13238877799, zip_sha256=v1.sha(raw), original_status=old["original"]["status"],
               invocation=INVOCATION, native_exit=3, original_elf=prior["original_elf"],
               bulk_repeated=False, cgroup_owner_repeated=False)
    v1.save()
    data = cas_inputs.blob(dict(hash=LOG, sizeBytes=34324), INVOCATION)
    row["topology"] = topology(data)
    assert row["topology"]["siblings"] == [dict(cpu=0, mask="0")]
    assert row["topology"]["config_sched_core"] and not row["topology"]["test_enable_started"]


def inputs(row: dict) -> None:
    # These exact original bytes were already built and verified. Do not
    # rebuild the product or repeat any owner from the 353-target run.
    data = cas_inputs.blob(dict(hash=ELF, sizeBytes=6675876), INVOCATION)
    row["elf"] = dict(sha256=v1.sha(data), bytes=len(data), machine=int.from_bytes(data[18:20], "little"))
    assert data[:6] == b"\x7fELF\x02\x01" and row["elf"] == dict(sha256=ELF, bytes=6675876, machine=62)
    hook = v1.git("show", v1.PRODUCT + ":.buildkite/hooks/pre-command")
    start = b"  if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then\n"
    assert hook.count(start) == 1
    block = hook[hook.index(start):].split(b"  fi\n", 1)[0] + b"  fi\n"
    assert v1.sha(block) == v1.HOOK
    Path(v1.WORKER).mkdir(parents=True)
    for name, body in {"BUILD": Path(v1.PACKAGE, "guest.BUILD").read_bytes(),
                       "payload.sh": Path(v1.PACKAGE, "payload.sh").read_bytes(),
                       "hook.bash": block, "selected-test": data}.items():
        Path(v1.WORKER, name).write_bytes(body)
    Path(v1.WORKER, "payload.sh").chmod(0o755)
    Path(v1.WORKER, "selected-test").chmod(0o755)
    v1.save()


def guest(row: dict, target: str) -> None:
    owner = "//" + v1.WORKER + ":" + target
    directory = Path("bazel-testlogs", v1.WORKER, target)
    try:
        v1.execute(row, target, ["--config=rbe", "--config=rbe-local-tests",
                   "--//tools/bazeldefs:local_test_architecture=amd64", "--jobs=16", "--test_timeout=420", owner],
                   {owner}, 25, remote=False)
    finally:
        log = directory / "test.log"
        if log.is_file() and log.stat().st_size < 2 * 1024**2:
            row["topology"] = topology(log.read_bytes())
            v1.save()
    cases = list(ElementTree.parse(directory / "test.xml").iter("testcase"))
    assert len(cases) == 1 and cases[0].attrib["name"] == "TestEnable" and not list(cases[0])
    assert cases[0].get("status") != "notrun" and cases[0].get("result") != "suppressed"
    observed = row["topology"]
    assert observed["config_sched_core"] and observed["guest_completed"] and observed["test_enable_started"]
    assert observed["siblings"] == [dict(cpu=cpu, mask="0-1") for cpu in (0, 1)]
    assert observed["online"] == ["0-1"] and len(observed["vendors"]) == len(observed["topoext"]) == 1
    if target == "guest_topoext_test":
        assert observed["vendors"] == ["AuthenticAMD"] and observed["topoext"] == [True]
    row["guest"] = v1.modes(directory / "test.outputs/outputs.zip", "guest/outputs/")
    assert row["guest"]["elf_sha256"] == ELF
    row["native_kvm_smt"] = True


def run() -> None:
    assert v1.git("rev-parse", "HEAD").decode().strip() == v1.SOURCE
    assert v1.git("rev-parse", "HEAD^").decode().strip() == v1.GUEST
    assert os.uname().machine == "x86_64" and os.getuid() != 0
    cpuinfo = Path("/proc/cpuinfo").read_text()
    vendors = sorted(set(re.findall(r"(?m)^vendor_id\s*:\s*(AuthenticAMD|GenuineIntel)$", cpuinfo)))
    online = Path("/sys/devices/system/cpu/online").read_text().strip()
    assert len(vendors) == 1 and re.fullmatch(r"[0-9,-]{1,64}", online)
    assert all(subprocess.run(["sudo", "-n", "test", flag, "/dev/kvm"]).returncode == 0 for flag in ("-c", "-r", "-w"))
    v1.record.update(scope="One unchanged coretag ELF in native KVM; original 353-owner and cgroup results retained",
                     coordinator=dict(machine=os.uname().machine, kernel=os.uname().release, uid=os.getuid(),
                                      vendor=vendors[0], online=online, topoext=bool(re.search(r"\btopoext\b", cpuinfo))))
    v1.save()
    if not v1.phase("original", original):
        return
    if not v1.phase("original_elf", inputs):
        return
    passed = v1.phase("original_cpu", lambda row: guest(row, "guest_test"))
    observed = v1.record["phases"]["original_cpu"].get("topology", {})
    if not passed and vendors == ["AuthenticAMD"] and observed.get("vendors") == vendors and observed.get("topoext") == [False] and observed.get("topology_rejected"):
        assert len(observed["siblings"]) == 2 and observed["config_sched_core"]
        passed = v1.phase("topoext_cpu", lambda row: guest(row, "guest_topoext_test"))
    else:
        v1.record["phases"]["topoext_cpu"] = dict(status="NOT_RUN", reason="original passed or AMD topology-extension discriminator absent")
    for name in ("original_cpu", "topoext_cpu"):
        row = v1.record["phases"][name]
        row["observation"] = ("NOT_RUN" if row["status"] == "NOT_RUN" else
                              "NATIVE_KVM_SMT_PASS" if row.get("native_kvm_smt") else
                              "TOPOLOGY_REJECTED" if row.get("topology", {}).get("topology_rejected") else
                              "TEST_FAILURE" if row.get("suite_failures") else "MISSING_NATIVE_TEST_EVIDENCE")
    functional_failure = any(row.get("suite_failures") for name, row in v1.record["phases"].items()
                             if name in ("original_cpu", "topoext_cpu"))
    v1.record.update(status="CORETAG_NATIVE_KVM_PASS" if passed else "FAIL" if functional_failure else "INCOMPLETE",
                     original_bulk_status="FAIL", whole_unit_suite_qualified=False)


if __name__ == "__main__":
    v1.save()
    try:
        with tempfile.TemporaryDirectory(dir=v1.TMP) as directory:
            v1.prepare(Path(directory))
            del os.environ["BUILDBUDDY_API_KEY"]
            rc = v1.TMP / "v1.bazelrc"
            rc.write_text(f"common --credential_helper=remote.buildbuddy.io={directory}/helper\n"
                          "build --remote_executor=grpcs://remote.buildbuddy.io\nbuild --remote_cache=grpcs://remote.buildbuddy.io\n"
                          "build --bes_backend=grpcs://remote.buildbuddy.io\nbuild --bes_results_url=https://app.buildbuddy.io/invocation/\n"
                          "build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io\n")
            run()
    except Exception as error:
        v1.record.update(error_class=type(error).__name__, error_sha256=v1.sha(str(error).encode()))
    v1.save()
    sys.exit(0 if v1.record["status"] == "CORETAG_NATIVE_KVM_PASS" else 1)
