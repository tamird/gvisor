"""Build ordinary static plugin consumers and run the existing socket case."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

import qualification_report

from qualify import NATIVE, OUT, TMP, OWNER, audit, diagnostics, now, save

PRODUCT = "acf4a712219b1b4917d033c4a108d88d142b3861"
RUNSC = "//runsc:runsc-plugin-stack"
SENTRY = "//runsc/cmd/sentry:gvisor_sentry_plugin_stack"
TEST = "//test/e2e:integration_test"
BACKTRACE = "//external/tools/plugin-stack:backtrace_test"
TARGETS = [RUNSC, SENTRY, "//:release", TEST, BACKTRACE]
RUNTIME = "runsc-qualified-plugin"
IMAGE = "ubuntu@sha256:64483f3496c1373bfd55348e88694d1c4d0c9b660dee6bfef5e12f43b9933b30"
PLATFORM = "//test/rbe/empty_worker:amd64_musl"
PROBE = "@@+_repo_rules+libpluginstack//:vfio_command_probe"


def command(args, log, seconds=1800):
    with log.open("w") as dst:
        return subprocess.run(["timeout", "--signal=INT", "--kill-after=20s", str(seconds), *map(str, args)],
                              stdout=dst, stderr=subprocess.STDOUT).returncode


def compiler_preflight(r, bazel, flags):
    """Check the real recursive Make command before building all consumers."""
    p = r["compiler_preflight"] = {"status": "INCOMPLETE", "started": now(), "target": PROBE,
        "scope": "dry-run pci_vfio.c recipe plus an actually compiled unistd.h declaration probe"}
    bep, compact, log = TMP / "vfio-probe.bep", TMP / "vfio-probe.compact", TMP / "vfio-probe.log"
    p["exit"] = command([*bazel, "build", *flags, "--platforms=@llvm//platforms:linux_x86_64_musl",
                         "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false",
                         "--execution_log_compact_file=" + str(compact), PROBE], log, 600)
    p.update(finished=now(), diagnostics=diagnostics(log.read_text(errors="replace")))
    if p["exit"]:
        p["status"] = r["status"] = "FAIL"
    save(r)
    ev = events(bep) if bep.exists() else []
    if ev:
        p["invocation"], = [e["started"]["uuid"] for e in ev if "started" in e]
    if compact.exists():
        p["actions"] = audit(compact)
    save(r)
    if p["exit"]:
        return False
    action, = [a for a in p["actions"] if a["targetLabel"] == PROBE]
    p["action"] = action
    assert action["mnemonic"] == "CcMakeMakeRule" and action["runner"] == "remote"
    assert not action["cacheHit"] and not action["exitCode"] and not action["status"]
    assert {x["name"]: x["value"] for x in action["platform"]} == {
        "Arch": "amd64", "OSFamily": "linux", "container-image": NATIVE,
        "workload-isolation-type": "oci", "recycle-runner": "false"}
    files = outputs(ev, PROBE)
    assert set(files) == {"pci-vfio-dry-run.txt", "vfio-api-command.txt"}
    p["outputs"] = {name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                    for name, path in files.items()}
    commands = []
    for line in files["pci-vfio-dry-run.txt"].read_text().splitlines():
        if "cmd_linux/pci_vfio.o = " in line:
            words = list(shlex.shlex(line, posix=True, punctuation_chars=True))
            commands += [w.split(" = ", 1)[1] for w in words if w.startswith("cmd_linux/pci_vfio.o = ")]
    expected, = commands
    actual = files["vfio-api-command.txt"].read_text().strip()
    assert actual.startswith("cmd_linux/vfio_api_probe.o = ")
    actual = actual.split(" = ", 1)[1]

    def flags_only(text, source, output):
        args = shlex.split(text)
        assert args[0].endswith("/bin/clang")
        assert args[args.index("-c") + 1].endswith("/" + source)
        assert args[args.index("-o") + 1] == output
        assert "-D_LARGEFILE64_SOURCE" in args
        assert ("-target" in args and args[args.index("-target") + 1] == "x86_64-linux-musl") or "--target=x86_64-linux-musl" in args
        ignored = {args.index("-o"), args.index("-o") + 1, args.index("-c"), args.index("-c") + 1}
        return [a for i, a in enumerate(args) if i not in ignored and not a.startswith("-Wp,-MD,")]

    expected_args = flags_only(expected, "pci_vfio.c", "linux/pci_vfio.o")
    actual_args = flags_only(actual, "vfio_api_probe.c", "linux/vfio_api_probe.o")
    assert actual_args == expected_args
    p.update(status="PASS", same_effective_flags=True, large_file_api=True, target_triple="x86_64-linux-musl",
             normalized_command_sha256=hashlib.sha256(json.dumps(actual_args).encode()).hexdigest(),
             probe_source_sha256=hashlib.sha256(Path("test/rbe/plugin_programs/vfio_api_probe.c").read_bytes()).hexdigest(),
             verified=now())
    save(r)
    return True


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
        captured = TMP / (key + ".captured.jsonl")
        retained = qualification_report.capture(bep, captured, Path(unquote(uri.path)).resolve().parent,
                                                 TMP / (key + "-reports"))
        assert not retained["errors"]
        report, = [f for f in retained["reports"] if f["name"] == "test.xml"]
        assert report["identity"]["label"] == target
        data = (captured.parent / report["path"]).read_bytes()
        row["capture_status"] = "PASS"
        # Timing is ancillary to the original runtime verdict.
        row["timing_status"] = "INCOMPLETE"
        try:
            if result.get("testAttemptStart") is not None and result.get("testAttemptDuration") is not None:
                start = qualification_report.timestamp(result["testAttemptStart"])
                duration = result["testAttemptDuration"]
                assert re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", duration)
                elapsed = float(duration[:-1])
            else:
                start = datetime.datetime.fromtimestamp(int(result["testAttemptStartMillis"]) / 1000,
                                                        datetime.timezone.utc)
                elapsed = int(result["testAttemptDurationMillis"]) / 1000
            assert 0 <= elapsed <= 600
            row.update(timing_status="PASS", execution_started=start.isoformat(),
                       execution_finished=(start + datetime.timedelta(seconds=elapsed)).isoformat())
        except (KeyError, ValueError, TypeError, AssertionError, OverflowError) as exc:
            row["timing_error"] = type(exc).__name__
        cases = [{"class": c.get("classname"), "name": c.get("name"),
                  "status": "FAIL" if c.find("failure") is not None or c.find("error") is not None else
                            "SKIP" if c.find("skipped") is not None or c.get("status") == "notrun" or c.get("result") == "suppressed" else "PASS"}
                 for c in ET.fromstring(data).iter("testcase")]
        row.update(cases=cases, xml_sha256=report["sha256"], xml_bytes=len(data))
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
         "control": {"product": "a24cd6513cec95981f723cf36b82f016e3fb9370", "artifact": 13109817821,
                     "sha256": "2e11f161ba40ff2def7b2412439163f4bed60669b679ac1e63901688a297d24f",
                     "named_cases_passed": 2, "vfio_enabled": False},
         "prior_vfio_attempt": {"product": "8671cdbf4e13082f7bb76a46966a3dfb063b5a87",
                                "run": 41373683054, "artifact": 13110665894,
                                "sha256": "ebbd4525663c0735013c425cb61065ea68ef5189e4affc463caaa097d033887e",
                                "status": "FAIL", "build_exit": 1, "tests_executed": 0,
                                "cause": "pci_vfio.c uses undeclared pread64/pwrite64 in the selected musl build"},
         "prior_flag_attempt": {"product": "4094887b23e0879ea0318837593dadd1ead182b5", "run": 41374521256,
                                "artifact": 13110643061, "sha256": "b9547f283425311b66ccfe66bbdbfe0a448988f85fc7abc43b4c7c70fc1ad472",
                                "status": "FAIL", "build_exit": 1, "tests_executed": 0,
                                "cause": "vendor dpdk/Makefile replaces TLDK's EXTRA_CFLAGS with DPDK_EXTRA_CFLAGS"},
         "expected": TARGETS, "target_libc": "musl (ordinary static Go transition)",
         "gnu_archive_evidence": {"run": 41364504151, "recovery_artifact": 13106931504, "distinct_configuration": True},
         "prior_static_consumer": {"run": 41366155971, "artifact": 13107584293, "status": "FAIL", "tests_executed": 0,
                                   "cause": "DPDK musl compile missing sys/queue.h"},
         "prior_header_binding": {"run": 41369346381, "artifact": 13109046368, "status": "FAIL", "tests_executed": 0,
                                  "cause": "repository rule used the header filegroup instead of its source file"},
         "status": "INCOMPLETE", "phase": "preflight", "started": now(), "tests_executed": 0}
    control = TMP / "uapi-control.json"
    if control.exists():
        r["control"]["uapi_script"] = json.loads(control.read_text())
    save(r)
    try:
        assert r["source"] == os.environ["GITHUB_SHA"]
        build_path = "external/tools/plugin-stack/plugin-stack.BUILD"
        original_build = subprocess.check_output(["git", "show", PRODUCT + ":" + build_path])
        probe_build = Path("test/rbe/plugin_programs/vfio_probe.BUILD").read_bytes()
        assert Path(build_path).read_bytes() == original_build + b"\n" + probe_build
        assert subprocess.run(["git", "diff", "--quiet", PRODUCT, "HEAD", "--", ".", ":(exclude)test/rbe", ":(exclude).github/workflows/build.yml", ":(exclude)" + build_path]).returncode == 0
        assert subprocess.run(["git", "diff", "--quiet", "HEAD"]).returncode == 0
        bazel = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
        flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt", "--host_platform=" + PLATFORM,
                 "--extra_execution_platforms=" + PLATFORM, "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                 "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
                 "--remote_accept_cached", "--noremote_upload_local_results",
                 "--jobs=400", "--remote_download_outputs=toplevel", "--noremote_local_fallback", "--spawn_strategy=remote",
                 "--color=no", "--curses=no", "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
                 "--registry=https://bcr.bazel.build"]
        r["phase"] = "compiler-preflight"
        if not compiler_preflight(r, bazel, flags):
            return 1
        r["phase"] = "target-preflight"
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
        # The original configured owner's single GoLink binds the output.
        # consumer_inputs independently verifies its declared output and digest.
        for target, path in ((RUNSC, runsc), (SENTRY, sentry)):
            link, = [a for a in r["actions"] if a["mnemonic"] == "GoLink" and
                     a["targetLabel"] in (target, target + "_/" + target.rsplit(":", 1)[1])]
            assert not link["exitCode"] and link["runner"] in ("remote", "remote cache hit", "disk cache hit")
            r["binaries"][target]["link"] = link
            parts = path.parts
            assert parts.count("bazel-out") == 1
            r["binaries"][target]["output_path"] = str(Path(*parts[parts.index("bazel-out"):]))
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
        assert elf(install / "runsc") == {k: v for k, v in r["binaries"][RUNSC].items() if k not in ("link", "output_path")}
        assert elf(install / "gvisor-bin/gvisor_sentry_plugin_stack") == {k: v for k, v in r["binaries"][SENTRY].items() if k not in ("link", "output_path")}
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
        marker = 'Sidecar "gvisor_sentry_plugin_stack" found: booting sandbox with ' + str(install / "gvisor-bin/gvisor_sentry_plugin_stack")
        observed = {"plugin_sentry": False, "plugin_initialization": False}
        for path in private_logs.iterdir():
            text = subprocess.check_output(["sudo", "tail", "-c", "1000000", path]).decode(errors="replace")
            observed["plugin_sentry"] |= marker in text
            observed["plugin_initialization"] |= "Initializing plugin network stack, config:" in text
        r["runtime"]["observed"] = observed
        assert all(observed.values())
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
