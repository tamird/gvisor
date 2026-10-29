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

# Replay the emitted commands without Bazel in a private mount namespace. Only
# /usr/bin/env is inaccessible; the workspace, runfiles, and toolchain remain.
replay = evidence / "replay.py"
replay.write_text("""import errno, json, os, subprocess, sys
from pathlib import Path
actionfile, response, uid, gid = sys.argv[1:]
os.setgroups([])
os.setgid(int(gid))
os.setuid(int(uid))
assert subprocess.run(["/bin/true"]).returncode == 0
def launch(args, env):
    try:
        p = subprocess.run(args, env=env, capture_output=True)
        return {"exit": p.returncode, "error": p.stderr.decode(errors="replace")[:300]}
    except OSError as e:
        return {"errno": e.errno, "error": str(e)}
actions=json.loads(Path(actionfile).read_text())
ret={"envControl":launch(["/usr/bin/env","true"], os.environ), "actions":{}}
for a in actions:
    env={pair["name"]:pair["value"] for pair in a.get("environmentVariables",[])}
    ret["actions"][a["mnemonic"]]=launch(a["commandArgs"],env)
Path(response).write_text(json.dumps(ret))
""")
mask = evidence / "no-env"
mask.touch(mode=0)

for mode, flags in settings.items():
    (consumer / "payload.txt").write_text(mode)
    (consumer / "plain.py").write_text(f'import sys\nwith open(sys.argv[1], "w") as out:\n    out.write({mode!r})\n')
    events = evidence / f"{mode}.spawns"
    p = subprocess.run(["bazel", "build", "--jobs=4", "--keep_going", "--spawn_strategy=local",
                        f"--execution_log_json_file={events}", *flags, "//:result", "//:archive"],
                       cwd=consumer, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (evidence / f"{mode}.log").write_text(p.stdout)
    rootp = Path(subprocess.check_output(["bazel","info","execution_root"], cwd=consumer,text=True).strip())
    records = [a for a in execution_records(events) if a.get("mnemonic") in ("PackageZip","PlainPython")] if events.exists() else []
    actions = []
    for a in records:
        prog = rootp / a["commandArgs"][0]
        head = prog.open("rb").readline(180).decode(errors="replace").strip() if prog.is_file() else None
        actions.append({"mnemonic":a["mnemonic"],"firstLine":head,"exit":a.get("exitCode",0)})
    good = p.returncode == 0 and {a["mnemonic"] for a in actions} == {"PackageZip","PlainPython"} and all(a["exit"] == 0 and (a["firstLine"] or "").startswith("#!/usr/bin/env") for a in actions)
    report["cases"][f"{mode}-normal"] = {"pass":good,"exit":p.returncode,"actions":actions}
    source, result = evidence / f"{mode}.actions", evidence / f"{mode}.restricted.json"
    source.write_text(json.dumps(records))
    result.touch()
    ns = subprocess.run(["sudo","unshare","--mount","--propagation","private","/bin/bash","-ec",
                         'mount --bind "$1" /usr/bin/env; shift; exec /usr/bin/python3 "$@"',
                         "replay",str(mask),str(replay),str(source),str(result),str(os.getuid()),str(os.getgid())],
                        cwd=rootp,capture_output=True,text=True)
    observed = json.loads(result.read_text() or "{}")
    # EACCES from execve means the kernel found the launcher but could not
    # execute its shebang interpreter. The /bin/true control runs normally.
    good = (ns.returncode == 0 and observed.get("envControl",{}).get("errno")==13 and
            set(observed.get("actions",{}))=={"PackageZip","PlainPython"} and
            all(a.get("errno")==13 for a in observed["actions"].values()))
    report["cases"][f"{mode}-blocked"]={"pass":good,"observed":observed,"harness":ns.stderr[:300]}
    out.write_text(json.dumps(report,indent=2)+"\n")
    print(mode, json.dumps(report["cases"]), flush=True)
report["verdict"] = "PASS" if set(report["cases"]) == set(required) and all(c["pass"] for c in report["cases"].values()) else "FAIL"
out.write_text(json.dumps(report,indent=2)+"\n")
raise SystemExit(report["verdict"] != "PASS")
