"""Compare the workspace and declared-input Gazelle checks on the Linux runner."""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

work = Path(os.environ["RUNNER_TEMP"])
bundle = work / "gazelle-qualification"
bundle.mkdir()
source = os.environ["SOURCE"]
base = os.environ["BASE"]
cases = ["master_clean", "candidate_clean", "scratch_clean", "workspace_missing", "declared_missing", "scratch_missing", "workspace_directive", "declared_directive", "scratch_directive", "restored_clean"]
records = {}
result = dict(source=source, base=base, expected=cases, results=records, status="INCOMPLETE")

def now():
    return datetime.now(timezone.utc).isoformat()

def save():
    result["missing"] = [name for name in cases if name not in records]
    (bundle / "verdict.json").write_text(json.dumps(result, indent=2) + "\n")

def command(args):
    return subprocess.check_output(args, text=True).strip()

def sanitized(text):
    return re.sub(r"(?i)((?:api-key|api.token|authorization)[:=]\s*)[^\s]+", r"\1[removed]", text)

def run(name, args, *, bad=False, contains=(), patch=None):
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    text = proc.stdout
    # Only retain the diagnostic lines relevant to this comparison; flags in a
    # full Bazel transcript can include the RBE authorization header.
    lines = [x for x in text.splitlines() if any(k in x for k in ("ERROR:", "gazelle_probe.go", "pkg/fspath", "from_ancestor", "app.buildbuddy.io/invocation/", "undeclared", "no such", "ErrDiff", "error:"))]
    (bundle / (name + ".log")).write_text(sanitized("\n".join(lines[-40:])) + "\n")
    lines = text
    if patch is not None and patch.exists():
        diff = patch.read_text()
        (bundle / (name + ".patch")).write_text(diff)
        lines += diff
    hits = {key: key in lines for key in contains}
    ok = (proc.returncode != 0 if bad else proc.returncode == 0) and all(hits.values())
    records[name] = dict(exit=proc.returncode, expected_failure=bad, matched=hits, success=ok, finished_at=now())
    save()
    if not ok:
        raise RuntimeError(f"{name}: exit={proc.returncode}, hits={hits}; see filtered diagnostic")
    return lines

def actual_packages():
    s = Path("tools/gazelle.bzl").read_text()
    node, = [a for a in ast.parse(s).body if isinstance(a, ast.Assign)]
    return ast.literal_eval(node.value)

target = "//:gazelle_diff"
def native(name, **kwargs):
    return run(name, ["bazel", "build", "--remote_download_outputs=all", target], **kwargs)

def query():
    data = command(["bazel", "aquery", "--output=jsonproto", "--include_commandline", "--include_artifacts", 'mnemonic("GazelleCheck", //:gazelle_diff)'])
    return json.loads(data)

def replay(name, **kwargs):
    graph = query()
    action, = graph["actions"]
    argv = action["arguments"]
    paths = {str(i["id"]): i for i in graph["pathFragments"]}
    def path(i):
        f = paths[str(i)]
        return path(f["parentId"]) / f["label"] if f.get("parentId") not in (None, 0, "0") else Path(f["label"])
    files = {str(a["id"]): path(a["pathFragmentId"]) for a in graph["artifacts"]}
    sets = {str(s["id"]): s for s in graph["depSetOfFiles"]}
    seen = set()
    deps = set()
    def walk(i):
        if (key := str(i)) in seen:
            return
        seen.add(key)
        group = sets[key]
        deps.update(files[str(x)] for x in group.get("directArtifactIds", []))
        for child in group.get("transitiveDepSetIds", []):
            walk(child)
    for i in action["inputDepSetIds"]:
        walk(i)
    def argument(flag):
        value, = [x[len(flag)+1:] for x in argv if x.startswith(flag+"=")]
        return Path(value)
    root = argument("-repo_root")
    config = argument("-repo_config")
    patch = argument("-patch")
    tool = Path(argv[0])
    expected = [str(root / pkg) for pkg in actual_packages()]
    assert argv[-len(expected):] == expected and len(expected) == 218
    assert "-index=none" in argv and "-r=false" in argv and "-bzlmod" in argv
    assert config in deps and tool in deps, (str(config), str(tool))
    declared = [f for f in sorted(deps) if f.is_relative_to(root) or f in (config, tool)]
    assert root / "go.mod" in declared and root / "BUILD" in declared
    if name != "scratch_clean":
        assert root / "pkg/bits/gazelle_probe.go" in declared
    if name == "scratch_directive":
        assert root / "pkg/BUILD" in declared
    execroot = Path(command(["bazel", "info", "execution_root"]))
    jail = work / (name + "-root")
    jail.mkdir()
    for f in declared:
        dest = jail / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(execroot / f, dest)
    (jail / tool).chmod(0o755)
    (jail / patch).parent.mkdir(exist_ok=True, parents=True)
    assert not (jail / "bin").exists() and not (jail / "usr").exists()
    metadata = dict(files=len(declared), packages=len(expected), gazelle=str(tool), repository_config=str(config), config_sha256=hashlib.sha256((jail / config).read_bytes()).hexdigest(), root=str(root))
    (bundle / (name + "-inputs.json")).write_text(json.dumps(metadata,indent=2)+"\n")
    return run(name, ["sudo", "/usr/bin/env", "-i", "PATH=/no-host-tools", "/usr/sbin/chroot", jail, *argv], patch=jail/patch, **kwargs)

def main():
    assert command(["git","rev-parse","HEAD"]) == source
    result["tree"] = command(["git","rev-parse","HEAD^{tree}"])
    subprocess.run(["git","switch","--detach",base],check=True)
    baseline = actual_packages()
    run("master_clean", ["bazel","run","//:gazelle_check"])
    subprocess.run(["git","switch","--detach",source],check=True)
    assert baseline == actual_packages() and len(baseline) == 218
    native("candidate_clean")
    replay("scratch_clean")
    sibling = Path("pkg/bits/gazelle_probe.go")
    ancestor = Path("pkg/BUILD")
    assert not sibling.exists() and not ancestor.exists()
    try:
        sibling.write_text('package bits\n\nimport _ "gvisor.dev/gvisor/pkg/fspath"\n')
        diagnostic = ["gazelle_probe.go","//pkg/fspath"]
        run("workspace_missing", ["bazel","run","//:gazelle_check"], bad=True, contains=diagnostic)
        native("declared_missing", bad=True)
        replay("scratch_missing", bad=True, contains=diagnostic)
        ancestor.write_text("# gazelle:resolve go gvisor.dev/gvisor/pkg/fspath //pkg:from_ancestor\n")
        diagnostic = ["gazelle_probe.go","//pkg:from_ancestor"]
        run("workspace_directive", ["bazel","run","//:gazelle_check"], bad=True, contains=diagnostic)
        native("declared_directive", bad=True)
        replay("scratch_directive", bad=True, contains=diagnostic)
    finally:
        sibling.unlink(missing_ok=True)
        ancestor.unlink(missing_ok=True)
    native("restored_clean")
    result["status"]="PASS"

save()
try:
    main()
except BaseException as err:
    result["error"] = sanitized(str(err))
    result["status"]="FAIL"
    raise
finally:
    result["finished_at"]=now()
    save()
