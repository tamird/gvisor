"""Replay the multicast regression with a native Firecracker TestRunner."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(os.environ["RUNNER_TEMP"]) / "llvm-source-presubmit"
SOURCE = "48f457b4e040312422b8e28b97081998d1d81297"
TARGET = "//runsc/sandbox:sandbox_test"
PARENT = "TestCollectLinksAndRoutes_Multicast"
CASES = {PARENT + "/disabled", PARENT + "/enabled"}
IMAGE = "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
SELECT = """select(.spawn) | .spawn | {owner:.targetLabel,mnemonic,runner,cached:(.cacheHit//false),exit:(.exitCode//0), xml:any(.args[]?; .=="--generate-xml"), props:([.platform.properties[]?|select(.name|IN("Arch","OSFamily","container-image","workload-isolation-type","dockerUser","network-enable-ipv6"))]|from_entries)}"""


def platform(arch):
    directory = ROOT / "execution-platforms"
    directory.mkdir(exist_ok=True)
    (directory / "MODULE.bazel").write_text('module(name = "multicast_workers")\n')
    (directory / "BUILD.bazel").write_text("".join(
        f'platform(name="{cpu}", parents=["@@//tools/bazeldefs:linux_{cpu}"],'
        f'exec_properties={{"OSFamily":"linux", "Arch":"{cpu}", "container-image":"{IMAGE}",'
        '"workload-isolation-type":"oci", "test.dockerUser":"root",'
        '"test.network-enable-ipv6":"true", "test.workload-isolation-type":"firecracker"},'
        'visibility=["//visibility:public"])\n'
        for cpu in ("amd64", "arm64")))
    return ["--inject_repository=multicast_workers=" + str(directory),
            "--extra_execution_platforms=@multicast_workers//:" + arch,
            "--host_platform=@multicast_workers//:" + arch]


def classify(log, arch):
    counts = collections.Counter()
    tests = []
    compiler_seen = False
    linked = False
    with subprocess.Popen([ROOT / "decoder", "print", "--raw", "--compact_execution_log=" + str(log)],
                          stdout=subprocess.PIPE) as reader:
        with subprocess.Popen(["jq", "-c", SELECT], stdin=reader.stdout, stdout=subprocess.PIPE, text=True) as rows:
            reader.stdout.close()
            for line in rows.stdout:
                row = json.loads(line)
                props = row["props"]
                if row["mnemonic"] == "TestRunner" and row["owner"].lstrip("@") == TARGET and not row["xml"]:
                    tests.append(row)
                elif row["mnemonic"] in ("GoCompilePkg", "GoLink") and props.get("Arch") == arch and props.get("workload-isolation-type") == "oci":
                    counts[(row["mnemonic"], row["runner"], row["cached"])] += 1
                    compiler_seen |= row["mnemonic"] == "GoCompilePkg"
                    linked |= row["mnemonic"] == "GoLink" and not row["cached"] and row["runner"] == "remote" and row["exit"] == 0
            assert rows.wait() == 0
        assert reader.wait() == 0
    return tests, [{"mnemonic": m, "runner": r, "cached": c, "count": n}
                   for (m, r, c), n in counts.items()], compiler_seen and linked


def run(arch):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    record = {"source": SOURCE, "arch": arch}
    receipt = ROOT / "results" / (arch + ".json")
    machine = {"amd64":"x86_64", "arm64":"aarch64"}[arch]
    bep = ROOT / (arch + ".bep")
    log = ROOT / (arch + ".bin")
    console = ROOT / (arch + ".console")
    args = [ROOT / "bazel", "--host_jvm_args=-Xmx10g", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(ROOT/"remote.rc"), "test",
            "--config=" + machine, *platform(arch), "--distdir=" + str(ROOT/"dist"), "--jobs=64", "--strip=never",
            "--remote_default_exec_properties=Arch=" + arch, "--nocache_test_results", "--flaky_test_attempts=1",
            "--modify_execution_info=^GoLink$=+no-remote-cache", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--test_arg=-test.v", "--test_arg=-test.run=^" + PARENT + "$", "--test_output=errors",
            "--remote_download_regex=.*testlogs/.*/test[.](log|xml)$", "--build_event_json_file=" + str(bep),
            "--execution_log_compact_file=" + str(log), "--", TARGET]
    with console.open("w") as stream:
        record["exit"] = subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT).returncode
    if record["exit"]:
        for s in console.read_text().splitlines():
            if re.search(r'ERROR:|FATAL:|FAIL:|FAILED:',s) and not re.search(r'header|credential|bearer|api.key',s,re.I):
                print(s[:1200],flush=True)
    for line in bep.read_text().splitlines():
        e = json.loads(line)
        if "started" in e: record["invocation"] = e["started"]["uuid"]
        if "testResult" in e and e["id"]["testResult"]["label"].lstrip("@") == TARGET:
            res = e["testResult"]
            record["test_status"] = res["status"]
            record["strategy"] = res.get("executionInfo",{}).get("strategy")
            paths = [x for x in res["testActionOutput"] if x["name"] == "test.xml"]
            if paths:
                f = Path("bazel-testlogs/runsc/sandbox/sandbox_test/test.xml")
                if f.exists():
                    data = f.read_bytes()
                    assert len(data) < 65536
                    digest = hashlib.sha256(data).hexdigest()
                    assert paths[0].get("digest", digest) == digest
                    record["xml_sha256"] = digest
                    tree = ET.fromstring(data)
                    cases = [c.get("name") for c in tree.iter("testcase") if c.get("status") not in ("skipped","notrun") and c.get("result") not in ("skipped","suppressed") and all(c.find(k) is None for k in ("failure","skipped","error"))]
                    record["passed"] = cases
                    shutil.copyfile(f, ROOT / "results" / (arch + ".xml"))
    receipt.write_text(json.dumps(record,indent=2) + "\n")
    rows, compiles, ok = classify(log, arch)
    record["test_actions"], record["go_build_actions"] = rows, compiles
    record["verified"] = (record["exit"] == 0 and record.get("test_status") == "PASSED" and
                          record.get("strategy") == "remote" and CASES <= set(record.get("passed",[])) and ok and
                          len(rows) == 1 and rows[0]["runner"] == "remote" and not rows[0]["cached"] and
                          rows[0]["props"] == {"Arch":arch,"OSFamily":"linux","container-image":IMAGE,
                          "workload-isolation-type":"firecracker","dockerUser":"root","network-enable-ipv6":"true"})
    receipt.write_text(json.dumps(record,indent=2) + "\n")
    print(arch,record.get("invocation"),record["exit"],record.get("test_status"),record["verified"],flush=True)
    if not record["verified"]:
        for s in console.read_text().splitlines():
            if re.search(r'ERROR:|FATAL:|FAIL:|FAILED:',s) and not re.search(r'header|credential|bearer|api.key',s,re.I):
                print(s[:1200],flush=True)
    return record["verified"]


if __name__ == "__main__":
    sys.exit(not run(sys.argv[1]))
