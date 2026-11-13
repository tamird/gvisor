"""Run complete sandbox suites on the native guest for each test architecture."""
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
SOURCE = "11d1ff8eb6909a10815059a5b0250bdadf37109c"
TARGETS = {
    "//runsc/sandbox:sandbox_test": "runsc-sandbox",
}
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
    with subprocess.Popen([ROOT / "decoder", "print", "--raw", "--compact_execution_log=" + str(log)],
                          stdout=subprocess.PIPE) as reader:
        with subprocess.Popen(["jq", "-c", SELECT], stdin=reader.stdout, stdout=subprocess.PIPE, text=True) as rows:
            reader.stdout.close()
            for line in rows.stdout:
                row = json.loads(line)
                props = row["props"]
                if row["mnemonic"] == "TestRunner" and row["owner"].lstrip("@") in TARGETS and not row["xml"]:
                    tests.append(row)
                elif row["mnemonic"] in ("GoCompilePkg", "GoLink") and props.get("Arch") == arch and props.get("workload-isolation-type") == "oci":
                    counts[(row["mnemonic"], row["runner"], row["cached"])] += 1
            assert rows.wait() == 0
        assert reader.wait() == 0
    return tests, [{"mnemonic": m, "runner": r, "cached": c, "count": n}
                   for (m, r, c), n in counts.items()]


def run(arch):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    record = {"source": SOURCE, "arch": arch, "targets": {}}
    receipt = ROOT / "results" / (arch + ".json")
    machine = {"amd64":"x86_64", "arm64":"aarch64"}[arch]
    bep = ROOT / (arch + ".bep")
    log = ROOT / (arch + ".bin")
    console = ROOT / (arch + ".console")
    args = [ROOT / "bazel", "--host_jvm_args=-Xmx10g", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(ROOT/"remote.rc"), "test",
            "--config=" + machine, *platform(arch), "--distdir=" + str(ROOT/"dist"), "--jobs=64", "--strip=never",
            "--remote_default_exec_properties=Arch=" + arch, "--nocache_test_results", "--flaky_test_attempts=1",
            "--keep_going", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--test_output=errors",
            "--remote_download_regex=.*testlogs/.*/test[.](log|xml)$", "--build_event_json_file=" + str(bep),
            "--execution_log_compact_file=" + str(log), "--", *TARGETS]
    with console.open("w") as stream:
        record["exit"] = subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT).returncode
    if record["exit"]:
        for s in console.read_text().splitlines():
            if re.search(r'ERROR:|FATAL:|FAIL:|FAILED:',s) and not re.search(r'header|credential|bearer|api.key',s,re.I):
                print(s[:1200],flush=True)
    for line in bep.read_text().splitlines():
        event = json.loads(line)
        if "started" in event:
            record["invocation"] = event["started"]["uuid"]
        if "testResult" not in event:
            continue
        target = event["id"]["testResult"]["label"].lstrip("@")
        if target not in TARGETS:
            continue
        result = event["testResult"]
        outputs = {out["name"]: out for out in result["testActionOutput"]}
        observed = {
            "bazel_status": result["status"],
            "strategy": result.get("executionInfo", {}).get("strategy"),
        }
        record["targets"][target] = observed
        results_suffix = target[2:].replace(":", "/")
        for filename in ("test.xml", "test.log"):
            if filename not in outputs:
                continue
            candidates = [Path("bazel-testlogs") / results_suffix / filename]
            candidates.extend(Path("bazel-out").glob("*/testlogs/" + results_suffix + "/" + filename))
            for local in candidates:
                if not local.is_file():
                    continue
                data = local.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if digest == outputs[filename].get("digest", digest):
                    break
            else:
                continue
            observed[filename] = digest
            if filename == "test.xml":
                assert len(data) <= 4 * 1024 * 1024
                shutil.copyfile(local, ROOT / "results" / (arch + "-" + TARGETS[target] + ".xml"))
                cases = list(ET.fromstring(data).iter("testcase"))
                observed["cases"] = len(cases)
                executed = {c.get("name") for c in cases if not any(
                    c.find(tag) is not None for tag in ("skipped", "failure", "error"))}
                wanted = {"TestCollectLinksAndRoutes_" + suffix for suffix in
                          ("SingleInterface", "MultipleInterfaces", "IPv6Disabled")}
                wanted.update(("TestLoopbackLinkNetgoferProxyLoopback", "TestLoopbackLinkDefaultRoute",
                               "TestPrimaryInterfacePerLinkDefaults", "TestRouteCollectionPreservesDefaults",
                               "TestCollectedDefaultsAcrossInterfaces", "TestPrimaryInterfaceAddressFamily",
                               "TestCollectedDefaultWithoutFamilyAddress"))
                observed["missing_checks"] = sorted(wanted - executed)
                for tag in ("failure", "skipped", "error"):
                    observed[tag] = [case.get("name") for case in cases if case.find(tag) is not None]
            else:
                lines = data.decode(errors="replace").splitlines()
                observed["diagnostics"] = [s[:800] for s in lines if re.search(
                    r"FAILED|FAIL:|--- SKIP:|unittest|Ran [0-9]+ tests|^OK|Error:|Traceback|panic:|"
                    r"denied|operation not permitted|not supported|cannot |failed to |read-only file|"
                    r"file or directory|missing|unexpected|timed out|SIG[A-Z]+|permission",
                    s, re.I
                ) and not re.search(r"credential|bearer|api.key|authorization:|password", s, re.I)][:150]
    receipt.write_text(json.dumps(record, indent=2) + "\n")
    rows, compiles = classify(log, arch)
    record["test_actions"], record["go_build_actions"] = rows, compiles
    expected = {"Arch":arch, "OSFamily":"linux", "container-image":IMAGE,
                "workload-isolation-type":"firecracker", "dockerUser":"root", "network-enable-ipv6":"true"}
    routed = (len(rows) == len(TARGETS) and {r["owner"].lstrip("@") for r in rows} == set(TARGETS)
              and all(r["runner"] == "remote" and not r["cached"] and r["props"] == expected for r in rows))
    record["routing_verified"] = routed
    receipts = record["targets"]
    record["verified"] = (routed and record["exit"] == 0 and set(receipts) == set(TARGETS) and
                          all(r["bazel_status"] == "PASSED" and r.get("cases", 0) > 0
                              and not r.get("missing_checks", True) and "test.log" in r for r in receipts.values()))
    receipt.write_text(json.dumps(record, indent=2) + "\n")
    print(arch, record.get("invocation"), record["exit"], routed,
          {k:v["bazel_status"] for k,v in receipts.items()}, flush=True)
    return record["verified"]


if __name__ == "__main__":
    sys.exit(not run(sys.argv[1]))
