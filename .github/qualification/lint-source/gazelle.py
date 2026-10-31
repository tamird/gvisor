"""Compare the workspace and declared-input Gazelle checks on the Linux runner."""

import ast
import hashlib
import json
import os
import re
import shutil
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path

work = Path(os.environ["RUNNER_TEMP"])
bundle = work / "gazelle-qualification"
bundle.mkdir()
source = os.environ["SOURCE"]
base = os.environ["BASE"]
cases = ["warning", "malformed", "unexpected_exit", "downstream_product", "downstream_wildcard", "master_clean", "candidate_clean", "scratch_clean", "workspace_missing", "declared_missing", "scratch_missing", "workspace_directive", "declared_directive", "scratch_directive", "bazelignore", "repo_ignore", "restored_clean", "public_alias"]
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
    lines = [x for x in text.splitlines() if any(k in x for k in ("ERROR:", "gazelle_probe.go", "pkg/fspath", "from_ancestor", "app.buildbuddy.io/invocation/", "undeclared", "no such", "ErrDiff", "error:", "unknown directive:", "syntax error"))]
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

target = "//:gazelle_check"
def native(name, **kwargs):
    return run(name, ["bazel", "build", "--remote_download_outputs=all", target], **kwargs)

def query():
    data = command(["bazel", "aquery", "--output=jsonproto", "--include_commandline", "--include_artifacts", 'mnemonic("GazelleCheck", //:gazelle_check)'])
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
    patch = Path(argv[argv.index("--exit-code-out") + 1])
    tool = Path(argv[argv.index("--") + 1])
    launcher = Path(argv[0])
    expected = [str(root / pkg) for pkg in actual_packages()]
    assert argv[-len(expected):] == expected and len(expected) == 218
    assert "-index=none" in argv and "-r=false" in argv and "-bzlmod" in argv
    assert config in deps and tool in deps and launcher in deps, (str(config), str(tool))
    declared = [f for f in sorted(deps) if f.is_relative_to(root) or f in (config, tool, launcher)]
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
    (jail / launcher).chmod(0o755)
    assert root / ".bazelignore" in declared
    (jail / patch).parent.mkdir(exist_ok=True, parents=True)
    assert not (jail / "bin").exists() and not (jail / "usr").exists()
    metadata = dict(files=len(declared), packages=len(expected), gazelle=str(tool), repository_config=str(config), config_sha256=hashlib.sha256((jail / config).read_bytes()).hexdigest(), root=str(root))
    (bundle / (name + "-inputs.json")).write_text(json.dumps(metadata,indent=2)+"\n")
    previous = json.loads((work / "prior" / "scratch_clean-inputs.json").read_text())
    metadata["previous_config_sha256"] = previous["config_sha256"]
    return run(name, ["sudo", "/usr/bin/env", "-i", "PATH=/no-host-tools", "/usr/bin/unshare", "--net", "/usr/sbin/chroot", jail, *argv], **kwargs)

def main():
    assert command(["git","rev-parse","HEAD"]) == source
    result["tree"] = command(["git","rev-parse","HEAD^{tree}"])
    # These unchanged oracle cases were executed on the same master and
    # Gazelle module version by the previous root-vs-workspace comparison.
    prior = json.loads((work / "prior" / "verdict.json").read_text())
    assert prior["status"] == "PASS" and prior["base"] == base
    for key in ("master_clean", "workspace_missing", "workspace_directive"):
        records[key] = dict(prior["results"][key], artifact=13045623547)
    assert len(actual_packages()) == 218
    assert 'tags = ["manual"]' in Path("BUILD").read_text()
    native("candidate_clean")
    replay("scratch_clean")
    warning = Path("pkg/BUILD")
    assert not warning.exists()
    try:
        warning.write_text("# gazelle:qualification_unknown rule\n")
        native("warning", contains=("unknown directive: gazelle:qualification_unknown",))
    finally:
        warning.unlink(missing_ok=True)
    malformed = Path("pkg/bits/BUILD")
    good = malformed.read_text()
    try:
        malformed.write_text(good + "\nunterminated_gazelle_rule(\n")
        text = native("malformed", bad=True, contains=("GazelleCheck", "pkg/bits/BUILD"))
        assert any(part in text.lower() for part in ("syntax error", "unexpected eof", "unexpected end"))
        assert "+++ pkg/bits/BUILD" not in text
    finally:
        malformed.write_text(good)
    argv, _ = query()
    prefix = argv[:argv.index("--")]
    prefix[prefix.index("--exit-code-out") + 1] = str(work / "abnormal.status")
    execroot = Path(command(["bazel", "info", "execution_root"]))
    prefix[0] = str(execroot / prefix[0])
    text = run("unexpected_exit", [*prefix, "--", sys.executable, "-c", 'import sys; print("tool diagnostic", file=sys.stderr); sys.exit(73)'], bad=True, contains=("tool diagnostic",))
    assert records["unexpected_exit"]["exit"] == 73 and (work / "abnormal.status").read_text() == "73"
    sibling = Path("pkg/bits/gazelle_probe.go")
    ancestor = Path("pkg/BUILD")
    assert not sibling.exists() and not ancestor.exists()
    try:
        sibling.write_text('package bits\n\nimport _ "gvisor.dev/gvisor/pkg/fspath"\n')
        diagnostic = ["gazelle_probe.go","//pkg/fspath"]
        text = native("declared_missing", bad=True, contains=diagnostic)
        # The entire corrective patch must be visible to the contributor in
        # Bazel's own failure output, without executing Gazelle a second time.
        diff = (work / "prior" / "scratch_missing.patch").read_text()
        assert diff.rstrip() in text
        (bundle / "declared_missing.patch").write_text(diff)
        replay("scratch_missing", bad=True, contains=diagnostic)
        ancestor.write_text("# gazelle:resolve go gvisor.dev/gvisor/pkg/fspath //pkg:from_ancestor\n")
        diagnostic = ["gazelle_probe.go","//pkg:from_ancestor"]
        native("declared_directive", bad=True, contains=diagnostic)
        replay("scratch_directive", bad=True, contains=diagnostic)
    finally:
        sibling.unlink(missing_ok=True)
        ancestor.unlink(missing_ok=True)
    original_ignore = Path(".bazelignore").read_text()
    repo_ignore = Path("REPO.bazel")
    assert not repo_ignore.exists()
    sibling.write_text('package bits\n\nimport _ "gvisor.dev/gvisor/pkg/fspath"\n')
    try:
        Path(".bazelignore").write_text(original_ignore.rstrip() + "\npkg/bits\n")
        native("bazelignore")
        Path(".bazelignore").write_text(original_ignore)
        repo_ignore.write_text('ignore_directories(["pkg/bits"])\n')
        native("repo_ignore")
    finally:
        sibling.unlink()
        Path(".bazelignore").write_text(original_ignore)
        repo_ignore.unlink(missing_ok=True)
    native("restored_clean")
    run("public_alias", ["bazel", "query", "--output=build", "@bazel_gazelle//:go_repository_config"], contains=("alias(", "bazel_gazelle_go_repository_config", "WORKSPACE"))
    assert '"bazel_gazelle_go_repository_config",' not in Path("MODULE.bazel").read_text()
    # Use an independent root module rather than assuming all dev dependencies
    # and root-only overrides remain visible to downstream packages.
    source_root = Path.cwd()
    consumer = work / "gazelle-downstream"
    consumer.mkdir()
    (consumer / "BUILD").write_text("")
    (consumer / "MODULE.bazel").write_text('module(name = "gazelle_downstream")\nbazel_dep(name = "gvisor", version = "0.0.1")\nlocal_path_override(module_name = "gvisor", path = "%s")\n' % source_root)
    shutil.copyfile(".bazelversion", consumer / ".bazelversion")
    os.chdir(consumer)
    config = ["--ignore_dev_dependency", "--platforms=@gvisor//tools/bazeldefs:linux_amd64"]
    # Run both even if a prerequisite breaks one: each check needs its own result.
    for key, cmd in (("downstream_product", ["@gvisor//pkg/abi/linux/errno:errno"]), ("downstream_wildcard", ["--nobuild", "@gvisor//:all"])):
        try:
            run(key, ["bazel", "build", *config, *cmd])
        except RuntimeError:
            pass
    os.chdir(source_root)
    if not all(records[k]["success"] for k in ("downstream_product", "downstream_wildcard")):
        baseline = work / "gazelle-parent"
        subprocess.run(["git", "worktree", "add", "--detach", str(baseline), source + "^"],check=True)
        mf=consumer/"MODULE.bazel"; mf.write_text(mf.read_text().replace(str(source_root), str(baseline)))
        os.chdir(consumer)
        for key, cmd in (("downstream_product", ["@gvisor//pkg/abi/linux/errno:errno"]), ("downstream_wildcard", ["--nobuild", "@gvisor//:all"])):
            if not records[key]["success"]:
                try: run(key+"_parent", ["bazel", "build", *config, *cmd])
                except RuntimeError: pass
        raise RuntimeError("downstream qualification incomplete; see candidate and parent diagnostics")
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
