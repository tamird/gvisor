"""Build a public gVisor library from a clean external module."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess


def verify():
    source = Path.cwd()
    work = Path(os.environ["RUNNER_TEMP"]) / "lint-consumer"
    work.mkdir()
    (work / ".bazelversion").write_text((source / ".bazelversion").read_text())
    manifest = (
        'module(name = "consumer")\n'
        'bazel_dep(name = "gvisor", version = "0.0.1")\n'
        f'local_path_override(module_name = "gvisor", path = {json.dumps(str(source))})\n'
    )
    (work / "MODULE.bazel").write_text(manifest)
    (work / "BUILD.bazel").write_text(
        'alias(name = "detect", actual = "@gvisor//pkg/gvisordetect")\n'
    )
    def bazel(*args):
        return subprocess.run(
            ["bazel", "--bazelrc=" + os.environ["CPPLINT_RC"], *args],
            cwd=work, capture_output=True, text=True,
        )

    def mapping(repo):
        p = bazel("mod", "dump_repo_mapping", repo)
        if p.returncode:
            return None
        return json.loads(p.stdout)

    events = work / "events.jsonl"
    execs = work / "exec.json"
    result = bazel(
        "build", "--jobs=24", "--lockfile_mode=update", "--noremote_accept_cached",
        "--build_event_json_file=" + str(events), "--execution_log_json_file=" + str(execs),
        "//:detect",
    )
    log = result.stderr + result.stdout
    # Pin a known failure by its cause, not by the nonzero exit code.
    external_gazelle = '" tag in a non-root Bazel module is forbidden' in log and '"go_deps.' in log
    root_mapping = mapping("")
    gvisor_mapping = mapping(root_mapping["gvisor"]) if root_mapping and "gvisor" in root_mapping else None
    lint = {"buildifier_prebuilt", "cpplint", "lint_inputs"}
    lock_path = work / "MODULE.bazel.lock"
    hashes = json.loads(lock_path.read_text()).get("registryFileHashes", {}) if lock_path.exists() else {}
    hits = sorted(url for url in hashes if any("/modules/" + name + "/" in url for name in lint))
    direct = sorted(lint.intersection(gvisor_mapping)) if gvisor_mapping is not None else None
    event_rows = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    invocation = next((e["started"]["uuid"] for e in event_rows if "started" in e), None)
    targets = [e["id"]["targetCompleted"]["label"] for e in event_rows
               if "targetCompleted" in e.get("id", {}) and e.get("completed", {}).get("success")]
    from check import rows
    actions = [
        s for s in rows(execs)
        if "gvisordetect" in s.get("targetLabel", "") and s.get("mnemonic") == "GoCompilePkg"
    ]
    files = [
        f["path"] for s in actions for f in s.get("actualOutputs", [])
        if f.get("path", "").endswith((".a", ".x")) and f.get("digest")
    ]
    compiled = result.returncode == 0 and bool(targets) and bool(files)
    boundary = bool(gvisor_mapping) and direct == [] and len(hashes) > 0 and hits == []
    return {
        "target": "@gvisor//pkg/gvisordetect", "consumer_manifest": manifest,
        "exit": result.returncode, "invocation": invocation, "compiled": compiled,
        "compile_outputs": sorted(files),
        "known_gazelle_failure": external_gazelle,
        "diagnostics": [x for x in ("go_deps", "non-root", "root module", "GoStdlib", "GoToolchainBinaryBuild", "GoCompilePkg", "visibility", "not visible", "no such target") if x.lower() in log.lower()],
        "diagnostic_sha256": sha256(log.encode()).hexdigest(),
        "root_repo_mapping_size": len(root_mapping or {}), "gvisor_repo_mapping_size": len(gvisor_mapping or {}),
        "lint_names_in_gvisor_mapping": direct, "registry_hash_count": len(hashes),
        "formatter_registry_files": hits, "formatter_boundary_proven": boundary,
        "characterized": bool(invocation) and boundary and (compiled or external_gazelle),
    }
