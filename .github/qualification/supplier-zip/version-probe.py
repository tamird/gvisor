"""Compare a plain py_binary action and an unpatched rules_pkg ZIP action."""
import json
import os
from pathlib import Path
import subprocess

root = Path.cwd()
consumer = root / "consumer"
consumer.mkdir()
evidence = Path(os.environ["RUNNER_TEMP"]) / "bootstrap-evidence"
evidence.mkdir()
out = evidence / "verdict.json"
variant = "2.4.5"
settings = {"script": ["--@rules_python//python/config_settings:bootstrap_impl=script"], "default": []}
required = [f"{mode}-{limit}" for mode in settings for limit in ("normal", "blocked")]
report = {"verdict": "INCOMPLETE", "expected": required, "source": "01446201fd8e758267f4e1abd9727926655110b7", "rules_python": variant, "cases": {}}
out.write_text(json.dumps(report))
(consumer / "MODULE.bazel").write_text(f'''module(name = "probe")
bazel_dep(name = "rules_python", version = "{variant}")
bazel_dep(name = "rules_pkg")
local_path_override(module_name = "rules_pkg", path = "../supplier")
''')
(consumer / "probe.bzl").write_text('''def _run(ctx):
    output = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.run(executable = ctx.attr.tool[DefaultInfo].files_to_run, arguments = [output.path], outputs = [output], mnemonic = "PlainPython")
    return [DefaultInfo(files = depset([output]))]
run = rule(implementation = _run, attrs = {"tool": attr.label(cfg = "exec", executable = True)})
''')
(consumer / "BUILD.bazel").write_text('''load("@rules_python//python:defs.bzl","py_binary")
load("@rules_pkg//pkg:zip.bzl","pkg_zip")
load(":probe.bzl","run")
py_binary(name="plain", srcs=["plain.py"])
run(name="result", tool=":plain")
pkg_zip(name="archive", srcs=["payload.txt"])
''')

def execution_records(path):
    raw = path.read_text()
    decoder = json.JSONDecoder()
    while raw.strip():
        record, end = decoder.raw_decode(raw.lstrip())
        yield record
        raw = raw.lstrip()[end:]

for mode, flags in settings.items():
    for limit in ("normal", "blocked"):
        name = f"{mode}-{limit}"
        # Each case has unique inputs, preventing an earlier action-cache hit.
        (consumer / "payload.txt").write_text(name)
        (consumer / "plain.py").write_text(f'import sys\nwith open(sys.argv[1], "w") as out:\n    out.write({name!r})\n')
        events = evidence / f"{name}.json"
        cmd = ["bazel", "build", "--jobs=4", "--keep_going", "--spawn_strategy=local",
               "--strategy=PackageZip=linux-sandbox", "--strategy=PlainPython=linux-sandbox",
               f"--execution_log_json_file={events}", *flags]
        if limit == "blocked":
            cmd.append("--sandbox_block_path=/usr/bin/env")
        p = subprocess.run([*cmd, "//:result", "//:archive"], cwd=consumer, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (evidence / f"{name}.log").write_text(p.stdout)
        actions = []
        if events.exists():
            for a in execution_records(events):
                if a.get("mnemonic") in ("PackageZip","PlainPython"):
                    exe = a["commandArgs"][0]
                    rootp = subprocess.check_output(["bazel","info","execution_root"], cwd=consumer, text=True).strip()
                    prog = Path(rootp) / exe
                    head = prog.read_bytes()[:200].splitlines()[0].decode("utf-8", "replace") if prog.is_file() else None
                    actions.append({"mnemonic": a["mnemonic"], "executable":exe,"firstLine":head,
                                    "runner":a.get("runner"), "exit": a.get("exitCode",0)})
        negative = limit == "blocked"
        diagnostics = [line.strip() for line in p.stdout.splitlines() if any(x in line.lower() for x in ("execvp","execve","permission denied","interpreter","no such file","exec format","failed to execute"))]
        good = (p.returncode != 0) == negative and len(actions) == 2 and {a["mnemonic"] for a in actions} == {"PackageZip","PlainPython"} and all(a["runner"]=="linux-sandbox" and (a["exit"] != 0) == negative and (a["firstLine"] or "").startswith("#!/usr/bin/env") for a in actions) and (not negative or bool(diagnostics))
        report["cases"][name] = {"returncode":p.returncode,"actions":actions,"diagnostics":diagnostics[-10:],"pass":good}
        print(name, report["cases"][name], flush=True)
        out.write_text(json.dumps(report,indent=2))
report["verdict"] = "PASS" if set(report["cases"]) == set(required) and all(c["pass"] for c in report["cases"].values()) else "FAIL"
out.write_text(json.dumps(report,indent=2)+"\n")
raise SystemExit(report["verdict"] != "PASS")
