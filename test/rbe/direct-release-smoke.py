"""Check the native source smoke commands against their configured outputs."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

import yaml


def now():
    return datetime.now(timezone.utc).isoformat()


facts = Path(os.environ["RUNNER_TEMP"], "direct-smoke")
report = facts / "records"
machine = platform.machine()
source = os.environ["SOURCE"]
expected = {"runsc", "containerd-shim-runsc-v1", "gvisor-bin/checkpointgofer", "gvisor-bin/runsc-metric-server", "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor_sentry", "gvisor-bin/gvisor-sentry-prewarmer", "gvisor-bin/gvisor-proc-umounter"}
cpu = {"x86_64": 62, "aarch64": 183}[machine]
arch = "AMD64" if cpu == 62 else "ARM64"
labels = [f":fire: Smoke tests ({arch})"] + ([":fire: Smoke race tests"] if cpu == 62 else [])
bazel = ["bazel", f"--bazelrc={facts}/cache.rc"]


def run(record, stage, args, env, minutes, tolerate_failure=False):
    log = facts / f"{record['flavor']}.{stage}.log"
    record.setdefault("stages", {})[stage] = {"startedAt": now()}
    (report / f"{record['flavor']}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(record["flavor"], stage, "started", flush=True)
    with log.open("w") as out:
        proc = subprocess.run(["timeout", "--kill-after=20s", f"{minutes}m", *args], stdout=out, stderr=subprocess.STDOUT, env=env)
    record["stages"][stage].update(exit=proc.returncode, finishedAt=now())
    if proc.returncode and not tolerate_failure:
        raise ValueError(stage)
    return log.read_text(errors="replace")


def digest(path):
    with path.open("rb") as f:
        h = hashlib.sha256()
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
        return h.hexdigest()


def observe(entry, number, target):
    """Project the original BEP and local test.log before the next invocation."""
    events = (json.loads(line) for line in Path(".build_events.json").open())
    row = {"attempt": number}
    entry.setdefault("tests", []).append(row)
    results = []
    for e in events:
        if "started" in e:
            row["invocation"] = e["started"]["uuid"]
        if "optionsParsed" in e:
            flags = e["optionsParsed"]["explicitCmdLine"]
            row["agentCache"] = flags.count(os.environ["BAZEL_REMOTE_CACHE"]) == 1
            row["quotedOption"] = flags.count("--build_metadata=SMOKE_QUALIFIER=native smoke") == 1
            row["keepSymbols"] = "--strip=never" in flags
        if "workspaceStatus" in e:
            row["stamp"] = next(i["value"] for i in e["workspaceStatus"]["item"] if i["key"] == "STABLE_VERSION")
        if "testResult" in e["id"]:
            results.append((e["id"]["testResult"], e["testResult"]))
        if e["id"].get("targetCompleted", {}).get("label") == target:
            row["builtSmoke"] = e["completed"].get("success", False)
        if "finished" in e:
            row["bazelExit"] = e["finished"]["exitCode"].get("code", 0)
    row["selectedResults"] = [i["label"] for i, _ in results]
    assert row["selectedResults"] == [target]
    identity, result = results[0]
    execution = result.get("executionInfo", {})
    row.update(identity=identity, testStart=result.get("testAttemptStart") or result.get("testAttemptStartMillisEpoch"),
               status=result["status"], strategy=execution.get("strategy"), exit=execution.get("exitCode", 0),
               localHit=result.get("cachedLocally", False), remoteHit=execution.get("cachedRemotely", False))
    log_file, = [x for x in result["testActionOutput"] if x["name"] == "test.log"]
    uri = urlsplit(log_file["uri"])
    assert uri.scheme == "file" and not uri.netloc
    test_log = Path(unquote(uri.path))
    log = test_log.read_text(errors="replace")
    row["logSHA256"] = digest(test_log)
    row["matchingSentry"] = any('in sidecar "gvisor_sentry"' in line and "matches the expected release" in line
                                and source[:12] in line for line in log.splitlines())
    row["boot"] = 'Sidecar "gvisor_sentry" found: booting sandbox' in log
    row["guestTrue"] = re.search(r'"args":\s*\[\s*"true"\s*\]', log) is not None
    row["guestRoot"] = "Changing configuration RootDir" in log
    assert row["agentCache"] and row["quotedOption"] and row["keepSymbols"]
    assert all(row[key] for key in ("invocation", "testStart", "builtSmoke", "matchingSentry", "boot", "guestTrue", "guestRoot"))
    assert source[:12] in row["stamp"] and row["status"] == "PASSED" and row["bazelExit"] == row["exit"] == 0
    assert row["strategy"] in {"local", "standalone"} and not row["localHit"] and not row["remoteHit"]


pipelines = {p.stem: [s for s in yaml.safe_load(p.read_text())["steps"] if isinstance(s, dict) and s.get("label") in labels] for p in map(Path, [".buildkite/first-pass.yaml", ".buildkite/pipeline.yaml"])}
assert os.getuid() and machine == os.environ["MACHINE"]
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
assert subprocess.check_output(["bazel", "--version"], text=True).strip() == "bazel " + Path(".bazelversion").read_text().strip()
assert set(s["label"] for s in pipelines["pipeline"]) == set(labels)
assert set(s["label"] for s in pipelines["first-pass"]) == (set(labels) if cpu == 62 else set())
guest_args = ["--alsologtostderr", "--network", "none", "--debug", "--TESTONLY-unsafe-nonroot=true", "--rootless", "do", "true"]
assert not any(os.environ.get(k) for k in ("GVISOR_SIDECAR_BINARIES_DIR", "STAGED_BINARIES"))
errors = 0
for step in pipelines["pipeline"]:
    race = "race" in step["label"]
    flavor = "release-race" if race else "release"
    release = f"//:{flavor}"
    target = "//:smoke-race" if race else "//:smoke"
    entry = {"flavor": flavor, "source": source, "proposal": os.environ["PROPOSAL"], "carrier": os.environ["GITHUB_SHA"], "worker": machine, "uid": os.getuid(), "startedAt": now(), "accepted": False}
    try:
        for other in pipelines["first-pass"] + pipelines["pipeline"]:
            if other["label"] == step["label"]:
                assert other["command"] == step["command"] and other["env"] == step["env"] and other["if"] == 'build.env("STAGED_BINARIES") == null'
        assert step["env"]["DOCKER_BUILD"] == "false" and "GLIBC_TUNABLES" not in step["env"]
        args = shlex.split(step["command"])
        assert args == ["make", "test", f"TARGETS={target}"]
        env = os.environ | step["env"]
        entry["command"] = args
        entry["builder"] = env["DOCKER_BUILD"]
        entry["crossGcc"] = shutil.which("aarch64-linux-gnu-gcc" if cpu == 62 else "x86_64-linux-gnu-gcc")
        # STARTUP_OPTIONS is a Make command-line override; cache and quoted
        # caller options are inherited just as they are from the Buildkite agent.
        env["MAKEFLAGS"] = f"STARTUP_OPTIONS=--bazelrc={facts}/cache.rc"
        config = ["--strip=never", env["BAZEL_REMOTE_CACHE"]]
        failed = False
        for number in (1, 2):
            Path(".build_events.json").unlink(missing_ok=True)
            name = f"guest{number}"
            run(entry, name, args, env, 52, tolerate_failure=True)
            try:
                observe(entry, number, target)
                assert entry["stages"][name]["exit"] == 0
            except (AssertionError, OSError, ValueError, KeyError, StopIteration) as err:
                failed = True
                trace = err.__traceback__
                while trace.tb_next:
                    trace = trace.tb_next
                entry["stages"][name]["observerError"] = [type(err).__name__, trace.tb_lineno]
                if entry["stages"][name]["exit"]:
                    text = (facts / f"{flavor}.{name}.log").read_text(errors="replace").lower()
                    entry["stages"][name]["diagnosticClasses"] = [code for code, pattern in {
                        "disk-full": "no space left on device", "gofer-create": "cannot create gofer process",
                        "permission-denied": "permission denied", "operation-not-permitted": "operation not permitted",
                        "toolchain": "no matching toolchains", "cache-unavailable": "remote cache",
                        "missing-file": "no such file or directory", "fetch-error": "error downloading",
                    }.items() if pattern in text]
            (report / f"{flavor}.json").write_text(json.dumps(entry, indent=2) + "\n")
        assert not failed
        assert len({r["invocation"] for r in entry["tests"]}) == len({r["testStart"] for r in entry["tests"]}) == 2
        preflight = run(entry, "binaryQuery", [*bazel, "cquery", *config, "--output=files", target], env, 8)
        wrapper, = [Path(s) for s in preflight.splitlines() if s.startswith("bazel-out/")]
        entry["wrapper"] = str(wrapper)
        layout_args = ["--output=starlark", "--starlark:file=tools/show_paths.bzl"]
        layout = run(entry, "copyQuery", [*bazel, "cquery", *config, *layout_args, release], env, 15)
        paths = {row[1]: Path(row[0]) for line in layout.splitlines() if len(row := line.split()) == 2 and row[0].startswith("bazel-out/")}
        entry["copyMap"] = {a: str(b) for a, b in paths.items()}
        assert set(paths) == expected, f"copy query returned {sorted(paths)}"
        attr_text = run(entry, "ruleAttributes", [*bazel, "query", "--output=xml", target], env, 4)
        xml = ET.fromstring(attr_text[attr_text.index("<?xml"):attr_text.rindex("</query>") + 8])
        rule = xml.find("rule")
        entry["targetArgs"] = [v.attrib["value"] for v in rule.findall("./list[@name='args']/string")]
        # Query XML does not limit environment key names, so retain only the
        # expected result, never an arbitrary value from a worker.
        pair = rule.find("./dict[@name='env']/pair")
        entry["configuredRaceEnvironment"] = pair is not None and [
            v.attrib["value"] for v in pair.findall("string")
        ] == ["GLIBC_TUNABLES", "glibc.pthread.rseq=0"]
        assert entry["targetArgs"] == guest_args and entry["configuredRaceEnvironment"] == race
        entry["linksToStagedRunsc"] = wrapper.is_symlink() and wrapper.samefile(paths["runsc"])
        assert entry["linksToStagedRunsc"], "smoke must execute the staged runsc"
        assert all(p.is_file() and p.stat().st_size > 0 for p in paths.values())
        names = ["//runsc:runsc", "//runsc/cmd/sentry:gvisor_sentry"]
        if race:
            names = [n + "-race" for n in names]
        run(entry, "directBuild", [*bazel, "build", *config, *names], env, 10)
        direct = run(entry, "directQuery", [*bazel, "cquery", *config, "--output=files", f"set({' '.join(names)})"], env, 4)
        outputs = {p.name: p for line in direct.splitlines() if line.startswith("bazel-out/") and (p := Path(line)).is_file()}
        assert sorted(outputs) == sorted(n.split(":")[1] for n in names)
        entry["binaries"] = {}
        for rel in ["runsc", "gvisor-bin/gvisor_sentry"]:
            data = paths[rel].read_bytes()
            name = Path(rel).name + ("-race" if race else "")
            check = entry["binaries"][rel] = {"copied": hashlib.sha256(data).hexdigest(), "direct":digest(outputs[name]), "bytes":len(data), "raceinit":b"runtime.raceinit" in data, "tsan":b"ThreadSanitizer" in data}
            assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == cpu
            assert check["copied"] == check["direct"] and check["raceinit"] == check["tsan"] == race
        entry["accepted"] = True
        # An analysis-only comparison; matching action keys do not prove
        # default release bytes match the binaries actually used by the guest.
        entry["shippingGoLink"] = {}
        for mode, options in (("make", config), ("default", config[1:])):
            stage = "linkKeys" + mode
            run(entry, stage, [*bazel, "aquery", *options, "--output=jsonproto", "--noinclude_artifacts",
                "--noinclude_commandline", f'mnemonic("GoLink", set({" ".join(names)}))'], env, 2, tolerate_failure=True)
            try:
                raw = (facts / f"{flavor}.{stage}.log").read_text()
                data, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
                keys = sorted({a["actionKey"] for a in data["actions"] if a["mnemonic"] == "GoLink"})
                entry["shippingGoLink"][mode] = keys if keys else None
            except (ValueError, KeyError):
                entry["shippingGoLink"][mode] = None
    except (AssertionError, OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as err:
        errors += 1
        entry["error"] = type(err).__name__
        entry["errorLine"] = err.__traceback__.tb_lineno
    finally:
        entry["finishedAt"] = now()
        (report / f"{flavor}.json").write_text(json.dumps(entry, indent=2) + "\n")
        print(flavor, entry["accepted"], entry.get("error", ""), flush=True)
if not errors and cpu == 62:
    # Neither client builds or analyzes a target. Record unknowns without
    # altering the primary guest result if the second client is unavailable.
    rc = facts / "metadata.rc"
    marker = "SMOKE_RC_TEST=one value"
    rc.write_text(f"common --build_metadata='{marker}'\n")
    control = {}
    for version in ("8.8.1", "9.3.1"):
        control_bep = facts / f"metadata-{version}.bep"
        try:
            proc = subprocess.run(["bazel", "--batch", "info", "release",
                                   f"--build_event_json_file={control_bep}"],
                                  env=os.environ | {"USE_BAZEL_VERSION": version, "BAZELRC": str(rc)},
                                  capture_output=True, timeout=75)
            control[version] = {"exit": proc.returncode, "versionObserved": version.encode() in proc.stdout}
            if control_bep.exists():
                events = (json.loads(line) for line in control_bep.open())
                flags = next(e["optionsParsed"]["cmdLine"] for e in events if "optionsParsed" in e)
                control[version]["singleQuotedMetadata"] = flags.count(f"--build_metadata={marker}") == 1
        except (OSError, ValueError, StopIteration, KeyError, subprocess.TimeoutExpired):
            control.setdefault(version, {})["incomplete"] = True
    (report / "metadata.json").write_text(json.dumps(control))
raise SystemExit(bool(errors))
