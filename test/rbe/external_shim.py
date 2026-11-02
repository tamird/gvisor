"""Build the public gVisor shim and observe protobuf startup in each root."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from zipfile import ZipFile

from external_go_policy import AREA, MASTER, PATCHED, SOURCE, client, details, git, manifest, now, read_events, select

SPLIT = "03763b0448f9e15a2247ba0a50e8b710cf4ba0e9"
RAW = "c01386e38186772e042196f4b03ce25eed8993ea8ffb8c012f43a2286b7752a5"
SHIM = "//shim:containerd-shim-runsc-v1"
EXT = "//pkg/shim/v1/extension:extension"
RESULT = {"verdict": "INCOMPLETE", "builds": {}}
BAZEL = [s for s in client("8.8.1") if s != "--noworkspace_rc"]


def save():
    (AREA.parent / "external-go.json").write_text(json.dumps(RESULT, indent=2) + "\n")


def selected(row, sha, version):
    return row["source"] == sha and row["actual_client"] == "bazel " + version and row["identity_ok"]


def original():
    archive = AREA / "original.zip"
    digest = sha256(archive.read_bytes()).hexdigest()
    assert digest == "76b231f008583f2bfa077a7a79bf32f21139f2b09079ed5775ea598dd789e61e"
    with ZipFile(archive) as z:
        assert z.namelist() == ["external-go.json"]
        prior = json.loads(z.read("external-go.json"))
    assert prior["verdict"] == "PASS" and all(prior["checks"].values())
    assert selected(prior["builds"]["master"], MASTER, "8.8.1")
    assert selected(prior["builds"]["upgrade"], "375b2c57032fa910447d4b81217fde2171303a9b", "9.3.1")
    assert prior["policies"]["gvisor_root"] == prior["policies"]["adopting"]
    RESULT["prior"] = {"run": 41398089026, "artifact": 13120208022, "sha256": digest,
                       "completed": prior["finished"], "checks": prior["checks"]}
    save()


def startup(name, exe, digest):
    observed = {"state": "INCOMPLETE"}
    RESULT["builds"][name]["startup"] = observed
    save()
    if not exe.is_file() or sha256(exe.read_bytes()).hexdigest() != digest:
        return
    exe = exe.resolve()
    trace = AREA / (name + ".trace")
    strace = shutil.which("strace")
    args = [strace, "-qq", "-f", "-yy", "-s", "0", "-e", "trace=pread64", "-o", str(trace)] if strace else []
    p = subprocess.run([*args, str(exe), "-v"], capture_output=True, text=True, timeout=30)
    observed.update(exit=p.returncode, size=exe.stat().st_size, sha256=digest, strace=bool(strace),
                    version_ok=all(s in p.stdout for s in ("containerd-shim-runsc-v1:", "Version:", "Revision:", "Go version:")))
    save()
    if p.returncode or not observed["version_ok"] or not trace.is_file():
        return
    offsets = [int(x) for x in re.findall(r"pread64\(\d+<" + re.escape(str(exe)) +
                                          r'>, [^\n]*?, 64, (\d+)\)\s+= 64\b', trace.read_text())]
    expected = [i * observed["size"] // 8 for i in range(8)]
    observed.update(state="OBSERVED", self_pread64_offsets=offsets,
                    eight_protobuf_samples=offsets == expected)
    save()


def actions(path, collection):
    collection[path.suffix] = "missing"
    if not path.is_file():
        return
    # The full shim has many transitive Go compilations. Project one spawn at
    # a time instead of keeping the entire execution log in memory.
    filt = '''select((.exitCode // 0) != 0 or ((.targetLabel // "") |
        endswith("//shim:containerd-shim-runsc-v1") or endswith("//pkg/shim/v1/extension:extension") or
        contains("/internal/detrand:") or contains("//pkg/protobuf/types:"))) |
        {targetLabel,mnemonic,exitCode,runner,cacheHit,status,platform,actualOutputs,
         inputs: [.inputs[]? | select(.path | endswith("/internal/detrand/rand.go"))]}'''
    with subprocess.Popen(["jq", "-c", filt, str(path)], stdout=subprocess.PIPE, text=True) as p:
        for line in p.stdout:
            yield json.loads(line)
        collection[path.suffix] = "complete" if p.wait() == 0 else "invalid"


def build(name, sha, cwd, ignore):
    root = cwd == SOURCE
    shim, ext = (SHIM, EXT) if root else ("//:shim", "//:extension")
    row = RESULT["builds"][name] = {"source": sha, "started": now(), "consumer": not root,
                                    "ignore_dev_dependency": ignore, "state": "INCOMPLETE", "collection": {}}
    save()
    bep, spawns = AREA / (name + ".bep"), AREA / (name + ".spawns")
    args = [*BAZEL, "build", "--jobs=32", "--keep_going",
            "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
            "--remote_default_exec_properties=EstimatedMemory=2GB",
            "--remote_default_exec_properties=workload-isolation-type=firecracker",
            "--remote_default_exec_properties=container-image=docker://" + os.environ["GNU"],
            "--remote_download_outputs=toplevel", "--build_event_json_file=" + str(bep),
            "--execution_log_json_file=" + str(spawns)]
    if ignore:
        args.append("--ignore_dev_dependency")
    p = subprocess.run([*args, shim, ext], cwd=cwd, capture_output=True, text=True, errors="replace")
    log = p.stderr + p.stdout
    row.update(exit=p.returncode, returned=now(), safe_errors=details(log) if p.returncode else [],
               log_sha256=sha256(log.encode()).hexdigest())
    save()
    completed = {}
    for e in read_events(bep, row["collection"]):
        if "started" in e:
            row.update(bazel=e["started"]["buildToolVersion"], invocation=e["started"]["uuid"])
        if "finished" in e:
            row["bazel_exit"] = e["finished"]["exitCode"].get("code", 0)
        if "targetCompleted" in e.get("id", {}):
            completed[e["id"]["targetCompleted"]["label"]] = e.get("completed", {}).get("success", False)
    row.update(completed=completed, actions=[], protobuf_sources=[], containerd_types=[], failures=[])
    save()
    for s in actions(spawns, row["collection"]):
        label = s.get("targetLabel") or ""
        mnemonic = s.get("mnemonic")
        if s.get("exitCode", 0) and len(row["failures"]) < 10:
            row["failures"].append([label, mnemonic, s["exitCode"], s.get("status")])
        if mnemonic == "GoCompilePkg" and "//pkg/protobuf/types:" in label and "com_github_containerd_containerd_v2" in label:
            row["containerd_types"].append({"label": label, "exit": s.get("exitCode") or 0,
                                           "outputs": s.get("actualOutputs") or []})
        if mnemonic == "GoCompilePkg":
            for f in s.get("inputs", []) if "/internal/detrand:" in label else ():
                if f.get("path", "").endswith("/internal/detrand/rand.go"):
                    row["protobuf_sources"].append(f.get("digest", {}))
        if not (label.endswith(SHIM) and mnemonic == "GoLink" or label.endswith(EXT) and mnemonic == "GoCompilePkg"):
            continue
        outputs = [f for f in s.get("actualOutputs") or [] if f.get("digest", {}).get("hash") and
                   (Path(f["path"]).name == "containerd-shim-runsc-v1" or f["path"].endswith((".a", ".x")))]
        props = {k["name"]: k["value"] for k in (s.get("platform") or {}).get("properties", [])}
        row["actions"].append({"owner": label, "mnemonic": mnemonic, "outputs": outputs,
                               "exit": s.get("exitCode") or 0, "runner": s.get("runner"),
                               "cached": s.get("cacheHit", False), "image": props.get("container-image")})
    row["identity_ok"] = (git("rev-parse", "HEAD") == sha and row.get("bazel") == "8.8.1" and
                          row.get("bazel_exit") == p.returncode and bool(row.get("invocation")))
    save()
    links = [(a, f) for a in row["actions"] if a["mnemonic"] == "GoLink" and not a["exit"] for f in a["outputs"]]
    if row["identity_ok"] and len(links) == 1:
        a, f = links[0]
        if (a["cached"] or a["runner"] == "remote") and a["image"] == "docker://" + os.environ["GNU"]:
            startup(name, cwd / f["path"], f["digest"]["hash"])
    if row["identity_ok"] and row["collection"].get(".bep") == "complete" and (
            row["collection"].get(".spawns") == "complete" or p.returncode and row["collection"].get(".spawns") == "missing"):
        if p.returncode and (row["safe_errors"] or row["failures"]):
            row["state"] = "BUILD_FAILED"
        elif not p.returncode and row.get("startup", {}).get("state") == "OBSERVED" and all(
                any(ok and (label == alias or label.endswith(owner)) for label, ok in completed.items())
                for alias, owner in ((shim, SHIM), (ext, EXT))):
            row["state"] = "BUILT"
    save()


def main():
    assert git("rev-parse", "HEAD^") == SPLIT and git("rev-parse", SPLIT + "^") == MASTER
    assert git("diff", "--name-only", MASTER, SPLIT) == "MODULE.bazel"
    original()
    module = git("show", MASTER + ":MODULE.bazel")
    tags = module[module.index("go_deps.module_override("):] + "\n"
    assert tags.count("go_deps.gazelle_override(") == 9 and tags.count("go_deps.module_override(") == 1
    assert git("show", SPLIT + ":MODULE.bazel").endswith(tags.replace("go_deps.", "root_go_deps.").rstrip())
    RESULT["policy_tag_sha256"] = sha256(tags.encode()).hexdigest()
    for name, sha, root, adopt in (("stock_ignore", MASTER, True, False), ("split_ignore", SPLIT, True, False),
                                   ("ordinary", SPLIT, False, False), ("adopting", SPLIT, False, True)):
        select(sha)
        cwd = SOURCE if root else AREA / name
        if not root:
            manifest(cwd, tags if adopt else None)
            (cwd / "BUILD.bazel").write_text(f'alias(name="shim",actual="@gvisor{SHIM}")\n'
                                             f'alias(name="extension",actual="@gvisor{EXT}")\n')
        try:
            build(name, sha, cwd, root)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            RESULT["builds"][name]["collection_error"] = type(exc).__name__
            save()
        subprocess.run([*BAZEL, "shutdown"], cwd=cwd, capture_output=True)
    states = [r["state"] for r in RESULT["builds"].values()]
    checks = {}
    for name, r in RESULT["builds"].items():
        if r["state"] != "BUILT":
            continue
        expected = PATCHED if name in ("stock_ignore", "adopting") else RAW
        src = {d.get("hash") for d in r["protobuf_sources"]}
        obs = r["startup"]
        checks[name] = bool(r["containerd_types"]) and src == {expected} and (
            obs["eight_protobuf_samples"] if expected == RAW else not obs["self_pread64_offsets"])
    RESULT.update(finished=now(), observed_link_and_startup=checks,
                  verdict="PASS" if len(states) == 4 and all(s == "BUILT" for s in states)
                          and len(checks) == 4 and all(checks.values()) else "INCOMPLETE")
    save()
    return RESULT["verdict"] == "INCOMPLETE"


if __name__ == "__main__":
    raise SystemExit(main())
