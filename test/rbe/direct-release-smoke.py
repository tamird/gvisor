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

import yaml


def now():
    return datetime.now(timezone.utc).isoformat()


facts = Path(os.environ["RUNNER_TEMP"], "direct-smoke")
report = facts / "records"
machine = platform.machine()
source = os.environ["SOURCE"]
expected = {"runsc", "containerd-shim-runsc-v1", "gvisor-bin/checkpointgofer", "gvisor-bin/runsc-metric-server", "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor_sentry", "gvisor-bin/gvisor-sentry-prewarmer", "gvisor-bin/gvisor-proc-umounter"}
config, cpu = {"x86_64": ("x86_64", 62), "aarch64": ("aarch64", 183)}[machine]
arch = "AMD64" if cpu == 62 else "ARM64"
labels = [f":fire: Smoke tests ({arch})"] + ([":fire: Smoke race tests"] if cpu == 62 else [])
bazel = ["bazel", f"--bazelrc={facts}/cache.rc"]


def run(record, stage, args, env, minutes):
    log = facts / f"{record['flavor']}.{stage}.log"
    record.setdefault("stages", {})[stage] = {"startedAt": now()}
    (report / f"{record['flavor']}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(record["flavor"], stage, "started", flush=True)
    with log.open("w") as out:
        proc = subprocess.run(["timeout", "--kill-after=20s", f"{minutes}m", *args], stdout=out, stderr=subprocess.STDOUT, env=env)
    record["stages"][stage].update(exit=proc.returncode, finishedAt=now())
    if proc.returncode:
        lines = (s for s in log.read_text(errors="replace").splitlines() if re.search(r"error|failed|exception|timeout|no such|not found|fatal|panic", s, re.I) and not re.search(r"header|api.?key|credential|authorization|token|password", s, re.I))
        record["stages"][stage]["diagnostic"] = [re.sub(r"https?://\S+", "<url>", s)[:400] for s in list(lines)[-12:]]
        raise ValueError(f"{stage} exited {proc.returncode}")
    return log.read_text(errors="replace")


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


pipelines = {p.stem: [s for s in yaml.safe_load(p.read_text())["steps"] if isinstance(s, dict) and s.get("label") in labels] for p in map(Path, [".buildkite/first-pass.yaml", ".buildkite/pipeline.yaml"])}
assert os.getuid() and machine == os.environ["MACHINE"]
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
assert subprocess.check_output(["bazel", "--version"], text=True).strip() == "bazel " + Path(".bazelversion").read_text().strip()
assert set(s["label"] for s in pipelines["pipeline"]) == set(labels)
assert set(s["label"] for s in pipelines["first-pass"]) == (set(labels) if cpu == 62 else set())
errors = 0
for step in pipelines["pipeline"]:
    race = "race" in step["label"]
    flavor = "release-race" if race else "release"
    target = f"//:{flavor}"
    entry = {"flavor": flavor, "source": source, "proposal": os.environ["PROPOSAL"], "carrier": os.environ["GITHUB_SHA"], "worker": machine, "uid": os.getuid(), "startedAt": now(), "accepted": False}
    try:
        for other in pipelines["first-pass"] + pipelines["pipeline"]:
            if other["label"] == step["label"]:
                assert other["command"] == step["command"] and other["env"] == step["env"] and other["if"] == 'build.env("STAGED_BINARIES") == null'
        assert step["env"]["DOCKER_BUILD"] == "false" and (step["env"].get("GLIBC_TUNABLES") == "glibc.pthread.rseq=0") == race
        args = shlex.split(step["command"])
        assert args[:4] == ["bazel", "run", target, "--"]
        assert args[4:] == ["--alsologtostderr", "--network", "none", "--debug", "--TESTONLY-unsafe-nonroot=true", "--rootless", "do", "true"]
        env = os.environ | step["env"]
        entry["command"] = args
        entry["builder"] = env["DOCKER_BUILD"]
        entry["crossGcc"] = shutil.which("aarch64-linux-gnu-gcc" if cpu == 62 else "x86_64-linux-gnu-gcc")
        binflags = ["--modify_execution_info=^GoStdlib$=+no-cache"]
        bep = facts / f"{flavor}.bep"
        layout_args = ["--output=starlark", "--starlark:file=tools/show_paths.bzl"]
        layout = run(entry, "copyQuery", [*bazel, "cquery", *binflags, *layout_args, target], env, 15)
        paths = {row[1]: Path(row[0]) for line in layout.splitlines() if len(row := line.split()) == 2 and row[0].startswith("bazel-out/")}
        entry["copyMap"] = {a: str(b) for a, b in paths.items()}
        assert set(paths) == expected, f"copy query returned {sorted(paths)}"
        elog = facts / f"{flavor}.native.compact"
        def guest(stage):
            try:
                return run(entry, stage, [*bazel, "run", *binflags, f"--execution_log_compact_file={elog}", f"--build_event_json_file={bep}", *args[2:]], env, 52)
            finally:
                if elog.is_file():
                    subprocess.run(["/usr/bin/python3", str(facts / "stdlib-provenance.py"), "summarize", str(elog), str(report / f"{flavor}.{stage}-producers.json")], check=True)
        try:
            log = guest("guest")
        except ValueError:
            default = (facts / f"{flavor}.guest.log").read_text(errors="replace")
            records = json.loads((report / f"{flavor}.guest-producers.json").read_text())
            bad = [a for a in records["actions"] if a["mnemonic"] == "GoStdlib" and not a["cacheHit"] and not a["status"] and a["exit"] == 0 and any(o["excludedArchives"] for o in a["outputs"])]
            if "present in stdlib GOROOT but not in source" not in default or not bad or all("sandbox" in a["runner"] for a in bad):
                raise
            # Explicitly retain the failure; only pin the failing producer.
            binflags += ["--strategy=GoStdlib=sandboxed"]
            entry["requiresExplicitProducerSandbox"] = True
            elog = facts / f"{flavor}.sandbox.compact"
            log = guest("explicitSandboxGuest")
        events = (json.loads(line) for line in bep.open())
        for e in events:
            if "started" in e:
                entry["invocation"] = e["started"]["uuid"]
            if "workspaceStatus" in e:
                entry["stamp"] = next(i["value"] for i in e["workspaceStatus"]["item"] if i["key"] == "STABLE_VERSION")
            if e["id"].get("targetCompleted",{}).get("label") == target:
                entry["builtRelease"] = e["completed"]["success"]
        assert entry.get("builtRelease") and source[:12] in entry["stamp"]
        entry["matchingSentry"] = any('in sidecar "gvisor_sentry"' in line and "matches the expected release" in line and source[:12] in line for line in log.splitlines())
        assert entry["matchingSentry"] and 'Sidecar "gvisor_sentry" found: booting sandbox' in log and "Changing configuration RootDir" in log
        assert all(p.is_file() and p.stat().st_size > 0 for p in paths.values())
        names = ["//runsc:runsc", "//runsc/cmd/sentry:gvisor_sentry"]
        if race:
            names = [n + "-race" for n in names]
        run(entry, "directBuild", [*bazel, "build", *binflags, *names], env, 10)
        direct = run(entry, "directQuery", [*bazel, "cquery", *binflags, "--output=files", f"set({' '.join(names)})"], env, 4)
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
    except (AssertionError, OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as err:
        errors += 1
        entry["error"] = f"{type(err).__name__}: {err}"
    finally:
        entry["finishedAt"] = now()
        (report / f"{flavor}.json").write_text(json.dumps(entry, indent=2) + "\n")
        print(flavor, entry["accepted"], entry.get("error", ""), flush=True)
raise SystemExit(bool(errors))
