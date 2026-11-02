"""Build ordinary static plugin consumers and run the existing socket case."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

from qualify import OUT, TMP, OWNER, audit, diagnostics, now, save

PRODUCT = "a24cd6513cec95981f723cf36b82f016e3fb9370"
RUNSC = "//runsc:runsc-plugin-stack"
SENTRY = "//runsc/cmd/sentry:gvisor_sentry_plugin_stack"
TEST = "//test/e2e:integration_test"
BACKTRACE = "//external/tools/plugin-stack:backtrace_test"
TARGETS = [RUNSC, SENTRY, "//:release", TEST, BACKTRACE]
RUNTIME = "runsc-qualified-plugin"
IMAGE = "ubuntu@sha256:64483f3496c1373bfd55348e88694d1c4d0c9b660dee6bfef5e12f43b9933b30"
PLATFORM = "//test/rbe/empty_worker:amd64_musl"


def command(args, log, seconds=1800):
    with log.open("w") as dst:
        return subprocess.run(["timeout", "--signal=INT", "--kill-after=20s", str(seconds), *map(str, args)],
                              stdout=dst, stderr=subprocess.STDOUT).returncode


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def outputs(ev, target):
    completed, = [e["completed"] for e in ev if e["id"].get("targetCompleted", {}).get("label") == target
                  and "aspect" not in e["id"]["targetCompleted"]]
    assert completed["success"]
    sets = {e["id"]["namedSet"]["id"]: e["namedSetOfFiles"] for e in ev if "namedSetOfFiles" in e}
    todo = [f["id"] for g in completed["outputGroup"] if g["name"] == "default" for f in g.get("fileSets", [])]
    seen, files = set(), {}
    while todo:
        key = todo.pop()
        if key in seen:
            continue
        seen.add(key)
        group = sets[key]
        todo.extend(f["id"] for f in group.get("fileSets", []))
        for f in group.get("files", []):
            uri = urlparse(f["uri"])
            assert uri.scheme == "file"
            path = Path(unquote(uri.path))
            data = path.read_bytes()
            assert len(data) == int(f["length"]) and hashlib.sha256(data).hexdigest() == f["digest"]
            assert path.name not in files or files[path.name] == path
            files[path.name] = path
    assert files
    return files


def elf(path):
    data = path.read_bytes()
    h = subprocess.check_output(["readelf", "-h", path], text=True)
    p = subprocess.check_output(["readelf", "-l", path], text=True)
    d = subprocess.check_output(["readelf", "-d", path], text=True)
    assert data.startswith(b"\x7fELF") and "Advanced Micro Devices X86-64" in h
    assert "INTERP" not in p and "(NEEDED)" not in d
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "machine": "x86_64", "interpreter": None, "needed": []}


def devices():
    code = r'''
import json,os,stat,subprocess
from pathlib import Path
result={}
for name,module,misc in [("/dev/vhost-net","vhost_net","vhost-net"),("/dev/net/tun","tun","tun")]:
    try:
        path=Path(name)
        if not path.exists():
            subprocess.run(["modprobe",module],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if not path.exists():
                major,minor=map(int,Path("/sys/class/misc",misc,"dev").read_text().split(":"))
                path.parent.mkdir(exist_ok=True)
                os.mknod(path,stat.S_IFCHR|0o660,os.makedev(major,minor))
        fd=os.open(path,os.O_RDWR|os.O_CLOEXEC)
        try:
            s=os.fstat(fd)
            assert stat.S_ISCHR(s.st_mode)
            result[name]={"opened":True,"device":[os.major(s.st_rdev),os.minor(s.st_rdev)]}
        finally: os.close(fd)
    except Exception as exc: result[name]={"opened":False,"error":type(exc).__name__}
print(json.dumps(result))
'''
    return json.loads(subprocess.check_output(["sudo", "python3", "-c", code], text=True))


def test_case(r, key, bazel, flags, target, name, extra=()):
    row = r[key] = {"status": "INCOMPLETE", "target": target, "expected": name, "started": now()}
    bep = TMP / (key + ".bep")
    save(r)
    rc = command([*bazel, "test", *flags, "--strategy=TestRunner=local", "--nocache_test_results", "--test_timeout=120",
                  "--test_sharding_strategy=disabled", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v",
                  "--test_arg=-test.run=^" + name + "$", *extra,
                  "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false", target], TMP / (key + ".log"), 600)
    row.update(exit=rc, finished=now(), status="FAIL" if rc else "INCOMPLETE")
    if rc:
        r["status"] = "FAIL"
    save(r)
    try:
        ev = events(bep)
        row["invocation"], = [e["started"]["uuid"] for e in ev if "started" in e]
        result, = [e["testResult"] for e in ev if e["id"].get("testResult", {}).get("label") == target]
        row["result"] = {k: result.get(k) for k in ("status", "cachedLocally", "executionInfo")}
        if result["status"] in ("FAILED", "TIMEOUT", "FLAKY", "REMOTE_FAILURE", "FAILED_TO_BUILD"):
            row["status"] = r["status"] = "FAIL"
        save(r)
        assert not result.get("cachedLocally") and not result.get("executionInfo", {}).get("cachedRemotely")
        assert result["executionInfo"]["strategy"] in ("local", "standalone")
        xml, = [f for f in result["testActionOutput"] if f["name"] == "test.xml"]
        uri = urlparse(xml["uri"])
        assert uri.scheme == "file"
        data = Path(unquote(uri.path)).read_bytes()
        assert hashlib.sha256(data).hexdigest() == xml["digest"] and len(data) == int(xml["length"])
        cases = [{"class": c.get("classname"), "name": c.get("name"),
                  "status": "FAIL" if c.find("failure") is not None or c.find("error") is not None else
                            "SKIP" if c.find("skipped") is not None or c.get("status") == "notrun" or c.get("result") == "suppressed" else "PASS"}
                 for c in ET.fromstring(data).iter("testcase")]
        row.update(cases=cases, xml_sha256=xml["digest"], xml_bytes=len(data))
        if any(c["status"] == "FAIL" for c in cases):
            row["status"] = r["status"] = "FAIL"
        save(r)
        assert len(cases) == 1 and cases[0]["name"] == name
        assert rc == 0 and result["status"] == "PASSED" and cases[0]["status"] == "PASS"
        row["status"] = "PASS"
    except Exception as exc:
        import traceback
        row["collection_error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    row["verdict_time"] = now()
    cases = [c for key in ("backtrace", "socket") for c in r.get(key, {}).get("cases", [])]
    r.update(case_records=len(cases), tests_executed=sum(c["status"] != "SKIP" for c in cases),
             passed=sum(c["status"] == "PASS" for c in cases), failed=sum(c["status"] == "FAIL" for c in cases),
             skipped=sum(c["status"] == "SKIP" for c in cases))
    save(r)


def main():
    OUT.mkdir(exist_ok=True)
    r = {"source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "product": PRODUCT,
         "expected": TARGETS, "target_libc": "musl (ordinary static Go transition)",
         "gnu_archive_evidence": {"run": 41364504151, "recovery_artifact": 13106931504, "distinct_configuration": True},
         "prior_static_consumer": {"run": 41366155971, "artifact": 13107584293, "status": "FAIL", "tests_executed": 0,
                                   "cause": "DPDK musl compile missing sys/queue.h"},
         "prior_header_binding": {"run": 41369346381, "artifact": 13109046368, "status": "FAIL", "tests_executed": 0,
                                  "cause": "repository rule used the header filegroup instead of its source file"},
         "status": "INCOMPLETE", "phase": "preflight", "started": now(), "tests_executed": 0}
    save(r)
    try:
        assert r["source"] == os.environ["GITHUB_SHA"]
        assert subprocess.run(["git", "diff", "--quiet", PRODUCT, "HEAD", "--", ".", ":(exclude)test/rbe", ":(exclude).github/workflows/build.yml"]).returncode == 0
        assert subprocess.run(["git", "diff", "--quiet", "HEAD"]).returncode == 0
        bazel = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
        flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt", "--host_platform=" + PLATFORM,
                 "--extra_execution_platforms=" + PLATFORM, "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                 "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
                 "--remote_accept_cached", "--noremote_upload_local_results",
                 "--jobs=400", "--remote_download_outputs=toplevel", "--noremote_local_fallback", "--spawn_strategy=remote",
                 "--color=no", "--curses=no", "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
                 "--registry=https://bcr.bazel.build"]
        query = "set(" + " ".join(TARGETS) + ")"
        qlog = TMP / "consumer-query.log"
        r["preflight_exit"] = command([*bazel, "cquery", *flags, query, "--output=label"], qlog, 300)
        found = sorted(set(re.findall(r"^(//\S+) \([0-9a-f]+\)$", qlog.read_text(), re.M)))
        r["selected"] = found
        r["preflight_diagnostics"] = diagnostics(qlog.read_text(errors="replace"))
        save(r)
        assert r["preflight_exit"] == 0 and found == sorted(TARGETS)
        bep, compact, log = TMP / "consumer.bep", TMP / "consumer.compact", TMP / "consumer.log"
        r.update(phase="build", build_started=now())
        save(r)
        rc = command([*bazel, "build", *flags, "--build_event_json_file=" + str(bep),
                      "--build_event_json_file_path_conversion=false", "--execution_log_compact_file=" + str(compact), *TARGETS], log)
        r.update(build_exit=rc, build_status="FAIL" if rc else "PASS", status="FAIL" if rc else "INCOMPLETE", build_finished=now())
        save(r)
        r.update(diagnostics(log.read_text(errors="replace")))
        ev = events(bep)
        r["invocation"], = [e["started"]["uuid"] for e in ev if "started" in e]
        r["bazel_finished"], = [e["finished"] for e in ev if "buildFinished" in e["id"]]
        r["actions"] = audit(compact, include_links=True)
        save(r)
        if rc:
            return 1
        by_target = {target: outputs(ev, target) for target in TARGETS}
        runsc, = by_target[RUNSC].values()
        sentry, = by_target[SENTRY].values()
        r["binaries"] = {RUNSC: elf(runsc), SENTRY: elf(sentry)}
        # Only exact output paths from the requested targets bind their links.
        output_base = subprocess.check_output([*bazel, "info", "execution_root"], text=True, stderr=subprocess.DEVNULL).strip()
        for target, path in ((RUNSC, runsc), (SENTRY, sentry)):
            relative = str(path.relative_to(output_base))
            link, = [a for a in r["actions"] if a["mnemonic"] == "GoLink" and a.get("output") == relative]
            assert not link["exitCode"] and link["runner"] in ("remote", "remote cache hit")
            r["binaries"][target]["link"] = link
        test_case(r, "backtrace", bazel, flags, BACKTRACE, "TestDumpStack")
        r["devices"] = devices()
        save(r)
        if not all(item["opened"] for item in r["devices"].values()):
            r.update(phase="host-devices", runtime_status="BLOCKED", vhost_traffic="not exercised")
            return 1
        install = TMP / "plugin-installed"
        (install / "gvisor-bin").mkdir(parents=True, exist_ok=True)
        for name, path in by_target["//:release"].items():
            destination = install / (name if name in ("runsc", "containerd-shim-runsc-v1") else "gvisor-bin/" + name)
            shutil.copy2(path, destination)
        shutil.copy2(runsc, install / "runsc")
        shutil.copy2(sentry, install / "gvisor-bin/gvisor_sentry_plugin_stack")
        assert elf(install / "runsc") == {k: v for k, v in r["binaries"][RUNSC].items() if k != "link"}
        assert elf(install / "gvisor-bin/gvisor_sentry_plugin_stack") == {k: v for k, v in r["binaries"][SENTRY].items() if k != "link"}
        private_logs = TMP / "plugin-runtime-logs"
        private_logs.mkdir()
        subprocess.run(["docker", "pull", IMAGE], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["docker", "tag", IMAGE, "gvisor.dev/images/basic/ubuntu"], check=True)
        subprocess.run(["sudo", install / "runsc", "install", "--runtime=" + RUNTIME, "--download-sidecars=NEVER", "--",
                        "--network=plugin", "--platform=systrap", "--debug", "--debug-log=" + str(private_logs) + "/"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["sudo", "systemctl", "reload", "docker"], check=True)
        entry = json.loads(subprocess.check_output(["sudo", "cat", "/etc/docker/daemon.json"]))["runtimes"][RUNTIME]
        assert entry["path"] == str(install / "runsc") and "--network=plugin" in entry["runtimeArgs"]
        assert "--platform=systrap" in entry["runtimeArgs"]
        r["runtime"] = {"name": RUNTIME, "runsc_sha256": r["binaries"][RUNSC]["sha256"],
                        "sentry_sha256": r["binaries"][SENTRY]["sha256"], "network": "plugin", "image": IMAGE}
        r.update(phase="socket-test", test_started=now())
        save(r)
        test_case(r, "socket", bazel, flags, TEST, "TestConnectToSelf", ["--test_arg=-runtime=" + RUNTIME])
        suites = [r["backtrace"], r["socket"]]
        if all(suite["status"] == "PASS" for suite in suites):
            r.update(status="PASS", runtime_status="PASS", phase="verified", vhost_traffic="device opened; same-container socket exchange only")
        elif any(suite["status"] == "FAIL" for suite in suites):
            r["status"] = "FAIL"
    except Exception as exc:
        import traceback
        r["collection_error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    finally:
        r["verdict_time"] = now()
        save(r)
    return 0 if r["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
