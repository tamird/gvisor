"""Recover the failed-log gate and check the vDSO/systrap host prerequisites."""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "nogo-receipts"
EVENTS = Path(".build_events.json")
BAD = "//pkg/tcpip/seqnum:seqnum_nogo"
EMBED = ["//pkg/sentry/loader/vdsodata:vdsodata_nogo", "//pkg/sentry/platform/systrap/sysmsg:sysmsg_nogo"]
DIAG = "self-assignment of v"
RES = f"--bazelrc={TMP}/nogo-resources.bazelrc"


def write(name, data):
    (OUT / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")


def jobs():
    commands = []
    for n in ("first-pass", "pipeline"):
        s = Path(f".buildkite/{n}.yaml").read_text().split('    label: ":golang: Nogo tests"\n')[1].splitlines()
        assert s[1:3] == ["    env:", '      DOCKER_BUILD: "false"']
        cmd = shlex.split(s[0].removeprefix("    command: "))
        assert cmd[:2] == ["bazel", "test"] and cmd[-1] == "//..."
        commands.append(cmd[:-1])
    assert commands[0] == commands[1]
    return commands[0][:1] + [RES] + commands[0][1:]


def observe(name, labels, extra, minutes):
    EVENTS.unlink(missing_ok=True)
    console = TMP / (name + ".console")
    with console.open("w") as out:
        code = subprocess.run(["timeout", "--kill-after=10s", minutes, *jobs(), *extra, *labels], stdout=out, stderr=out).returncode
    lines = console.read_text(errors="replace").splitlines()
    # Only source/action diagnostics and ordinary compiler command names.
    errors = [l[:480] for l in lines if ("ERROR:" in l or "not found" in l or "No such file" in l) and not any(s in l.lower() for s in ("header", "api-key", "token", "buildbuddy", "bearer"))][:9]
    r = {"source": os.environ["PRODUCT"], "exit": code, "errors": errors, "summaries": {}, "logs": {}}
    if EVENTS.exists():
        for line in EVENTS.read_text().splitlines():
            e = json.loads(line)
            if "testSummary" in e:
                label, summary = e["id"]["testSummary"]["label"], e["testSummary"]
                r["summaries"][label] = summary["overallStatus"]
                failed = summary.get("failed", [])
                r["logs"][label] = []
                for f in failed:
                    uri = urlsplit(f["uri"])
                    path = Path(unquote(uri.path))
                    r["logs"][label].append({"scheme": uri.scheme, "local": str(path) if uri.scheme == "file" else None, "readable": uri.scheme == "file" and path.is_file()})
            if "testResult" in e and e["id"]["testResult"]["label"] in labels:
                label = e["id"]["testResult"]["label"]
                log = Path("bazel-testlogs") / label[2:].replace(":", "/") / "test.log"
                if log.is_file():
                    text = log.read_text(errors="replace")
                    r.setdefault("runner", {})[label] = {a: a + "..." in text for a in ("amd64", "arm64")}
                    if label == BAD:
                        (OUT / (name + "-test.log")).write_text(text)
                        r["self_assignment"] = DIAG in text
    write(name, r)
    return r


def hook(name):
    scripts = Path(".buildkite/hooks/post-command").read_text()
    block = scripts.split("# Upload all relevant test failures.\n")[1].split("# Upload all profiles,")[0]
    fake = TMP / ("nogo-mock-" + name)
    fake.mkdir()
    agent = fake / "buildkite-agent"
    agent.write_text("""#!/bin/bash
set -eu
case "$1 $2" in
  'artifact upload') printf '%s\\n' "$3" >> "$MOCK/uploads"; test -r "$3";;
  'annotate --style') cat >> "$MOCK/annotations";;
  *) exit 2;;
esac
""")
    agent.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", MOCK=str(fake))
    read = subprocess.run(["make", "-s", "testlogs"], text=True, capture_output=True)
    p = subprocess.run(["bash", "-e", "-c", block], env=env, capture_output=True, text=True)
    uploads = (fake/"uploads").read_text().splitlines() if (fake/"uploads").exists() else []
    actual = [u for u in uploads if Path(u).is_file() and DIAG in Path(u).read_text(errors="replace")]
    annotation = (fake/"annotations").read_text() if (fake/"annotations").exists() else ""
    matched = len(actual) == 1 and f"[{BAD}](artifact://{actual[0].lstrip('/')})" in annotation
    record = {"reader_exit": read.returncode, "reader_labels": [l.split()[0] for l in read.stdout.splitlines() if l.split()], "reader_schemes": [urlsplit(l.split()[-1]).scheme or "path" for l in read.stdout.splitlines() if l.split()], "hook_exit": p.returncode, "upload_count": len(uploads), "readable_uploads": len(actual), "annotation_matches": matched}
    write(name + "-hook", record)
    return matched and p.returncode == 0


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["PRODUCT"]
    source = Path("pkg/tcpip/seqnum/seqnum.go")
    text = source.read_text()
    anchor = "func (v Value) LessThan(w Value) bool {\n"
    assert text.count(anchor) == 1
    source.write_text(text.replace(anchor, anchor + "\tv = v\n"))
    baseline = observe("as_proposed", [BAD], [], "22m")
    original_log = hook("as_proposed") if baseline["summaries"].get(BAD) == "FAILED" else False
    recovered = original_log
    if baseline.get("self_assignment") and not original_log:
        corrected = observe("local_event_paths", [BAD], ["--nobuild_event_json_file_path_conversion"], "5m")
        recovered = corrected.get("self_assignment") and hook("local_event_paths")
    source.write_text(text)
    binaries = {c: shutil.which(c) for c in ("aarch64-linux-gnu-gcc", "aarch64-linux-gnu-g++", "x86_64-linux-gnu-gcc", "x86_64-linux-gnu-g++")}
    write("host-compilers", binaries)
    emb = observe("embedded_archives", EMBED, ["--remote_cache=", "--remote_executor="], "24m")
    passed = emb["exit"] == 0 and all(emb["summaries"].get(l) == "PASSED" and all(emb["runner"][l].values()) for l in EMBED)
    # An actual cross-compiler diagnostic is a successful prerequisite
    # observation, not a successful analyzer. It is still a publication gate.
    missing = any("aarch64-linux-gnu-" in l and ("not found" in l or "No such file" in l) for l in emb["errors"])
    write("verdict", {"source": os.environ["PRODUCT"], "original_log_uploaded": original_log, "corrected_log_uploaded": bool(recovered), "embedded_owners_pass": passed, "missing_cross_compiler": missing, "verified": bool(recovered and (passed or missing))})
    assert recovered and (passed or missing), (recovered, passed, emb)


if __name__ == "__main__":
    main()
