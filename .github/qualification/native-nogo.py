"""Check the previously blocked Nogo input generators with declared LLVM."""

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
EMBED = ["//pkg/sentry/loader/vdsodata:vdsodata_nogo", "//pkg/sentry/platform/systrap/sysmsg:sysmsg_nogo"]
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
    write(name, r)
    return r



def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["PRODUCT"]
    compilers = {n: shutil.which(n) for n in ("x86_64-linux-gnu-gcc", "aarch64-linux-gnu-gcc", "aarch64-linux-gnu-g++")}
    write("native-compilers", compilers)
    assert compilers["aarch64-linux-gnu-gcc"] is None
    result = observe("llvm-nogo", EMBED, [], "47m")

    def elf_machines(pattern):
        out = set()
        for p in Path("bazel-out").glob(pattern):
            with p.open("rb") as f:
                header = f.read(20)
            if header.startswith(b"\x7fELF"):
                out.add(int.from_bytes(header[18:20], "little" if header[5] == 1 else "big"))
        return sorted(out)

    vdsos = elf_machines("*/bin/pkg/sentry/loader/vdsodata/vdso_*.so")
    stubs = elf_machines("*/bin/pkg/sentry/platform/systrap/sysmsg/sighandler.built-in.bin.o")
    actions = []
    if EVENTS.exists():
        with EVENTS.open() as events:
            for line in events:
                e = json.loads(line)
                action = e.get("action", {})
                label = action.get("label", "")
                if action.get("type") == "Genrule" and label in ("//vdso:vdso", "//pkg/sentry/platform/systrap/sysmsg:sysmsg_lib"):
                    # Preserve only input tool names, not arbitrary command lines.
                    args = " ".join(action.get("commandLine", []))
                    actions.append({"label":label, "success":action.get("success", False), "clang":"clang" in args})
    markers = result.get("runner", {})
    ok = result["exit"] == 0 and all(result["summaries"].get(l) == "PASSED" and all(markers.get(l, {}).get(a) for a in ("amd64","arm64")) for l in EMBED)
    write("verdict", {"verified": ok, "qualified_source": os.environ["PRODUCT"], "product":"e86e84eb8", "compiler":"0afd5b37e", "passed_owners":result["summaries"], "arch_markers":markers, "vdso_elf_machines":vdsos, "systrap_elf_machines":stubs, "genrules":actions})
    assert ok, result


if __name__ == "__main__":
    main()
