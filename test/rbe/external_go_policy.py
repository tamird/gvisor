"""Compare the shared Gazelle extension in two real consumer configurations."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from zipfile import ZipFile

from check import events

MASTER = "aa767e4c43d011121271551fe24da501a56f729d"
UPGRADE = "375b2c57032fa910447d4b81217fde2171303a9b"
SPLIT = "26ef225766ba82a92fefbbc9795c3d697605489b"
AREA = Path(os.environ["RUNNER_TEMP"]) / "external-go"
SOURCE = Path.cwd()
PROTO = "@org_golang_google_protobuf//internal/detrand:rand.go"
SHIM_DEP = "@com_github_containerd_containerd_v2//pkg/protobuf/types:go_default_library"
WAITER = "@gvisor//pkg/waiter:waiter"
PATCHED = "eb7bf9d4c9be19de92651a4dc31701c80beef92e04a5c89ab14a1902aa4618f2"
RESULT = {"verdict": "INCOMPLETE", "builds": {}, "policies": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    (AREA.parent / "external-go.json").write_text(json.dumps(RESULT, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def select(sha):
    subprocess.run(["git", "checkout", "--detach", sha], check=True)
    assert git("rev-parse", "HEAD") == sha
    assert not git("status", "--porcelain", "--untracked-files=no")


def client(version):
    return [str(AREA / ("bazel-" + version)), "--nosystem_rc", "--nohome_rc",
            "--noworkspace_rc", "--bazelrc=" + os.environ["LINT_RC"]]


def manifest(work, tags=None):
    work.mkdir()
    text = ('module(name="consumer")\nbazel_dep(name="gvisor",version="0.0.1")\n'
            f'local_path_override(module_name="gvisor",path={json.dumps(str(SOURCE))})\n')
    if tags is not None:
        text += ('bazel_dep(name="gazelle",version="0.51.3",repo_name="bazel_gazelle")\n'
                 'go_deps=use_extension("@bazel_gazelle//:extensions.bzl","go_deps")\n'
                 'use_repo(go_deps,"org_golang_google_protobuf","com_github_containerd_containerd_v2")\n')
        text += tags.replace('"//tools:protobuf.', '"@gvisor//tools:protobuf.')
    (work / "MODULE.bazel").write_text(text)
    (work / "BUILD.bazel").write_text(f'alias(name="use_waiter",actual="{WAITER}")\n')


def diagnoses(log):
    return sorted({s for s in [
        "GoToolchainBinaryBuild", "GoStdlib", "No matching toolchains", "cannot find package",
        "cannot find module", "error obtaining VCS status", "failed to initialize build cache",
        "kernel_headers.configure", "no such package", "no such target", "No such file or directory",
        "does not contain symbol", "cannot execute", "undeclared inclusion", "RESOURCE_EXHAUSTED",
        "x509: certificate", "403 Forbidden", "Analysis failed",
    ] if s in log})


def details(log):
    lines = []
    context = 0
    for line in log.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
        if re.search(r"\berror\b|\bfatal\b|\bfailed\b|not found|no such |cannot |could not |unable to ", line, re.I):
            context = 3
        elif context:
            context -= 1
        else:
            continue
        if re.search(r"key[=:]|api[-_]key|--(?:remote|bes)_header(?:=|\s)|token|password|secret|credential|auth|bearer|cookie|[?&][a-z]+=|/bin/bash -c", line, re.I):
            continue
        line = re.sub(r"(?:https?|grpcs?|bytestream)://[^ ]+", "<url>", line.strip())
        line = re.sub(r"/(?:home/runner|tmp|__w)/[^ :]*?/external/", "@@", line)
        line = re.sub(r"[A-Za-z0-9_+/=-]{70,}", "<long-value>", line)[:440]
        if line and line not in lines:
            lines.append(line)
        if len(lines) == 8:
            break
    return lines


def read_events(path, collection):
    collection[path.suffix] = "missing"
    if not path.is_file():
        return
    try:
        yield from events(path)
    except (UnicodeError, ValueError):
        collection[path.suffix] = "invalid"
    else:
        collection[path.suffix] = "complete"


def build(name, sha, version, work):
    row = {"source": sha, "client": version, "started": now(), "status": "INCOMPLETE",
           "manifest_sha256": sha256((work / "MODULE.bazel").read_bytes()).hexdigest()}
    RESULT["builds"][name] = row
    save()
    bep, spawns = AREA / (name + ".bep"), AREA / (name + ".spawns")
    args = [*client(version), "build", "--jobs=24",
            "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
            "--remote_default_exec_properties=EstimatedMemory=2GB",
            "--remote_default_exec_properties=workload-isolation-type=firecracker",
            "--remote_default_exec_properties=container-image=docker://" + os.environ["GNU"],
            "--remote_download_outputs=toplevel", "--build_event_json_file=" + str(bep),
            "--execution_log_json_file=" + str(spawns), "//:use_waiter"]
    p = subprocess.run(args, cwd=work, capture_output=True, text=True, errors="replace")
    log = p.stderr + p.stdout
    row["collection"] = {}
    row["safe_error_lines"] = details(log) if p.returncode else []
    row["actual_client"] = subprocess.check_output([client(version)[0], "--version"], text=True).strip()
    row.update(exit=p.returncode, returned=now(), status="FAIL" if p.returncode else "INCOMPLETE",
               log_sha256=sha256(log.encode()).hexdigest(), diagnostics=diagnoses(log),
               forbidden_tags=sorted(set(re.findall(r'Using the "(go_deps[.][a-z_]+)" tag in a non-root Bazel module', log))))
    save()
    done = []
    for e in read_events(bep, row["collection"]):
        if "started" in e:
            row.update(invocation=e["started"]["uuid"], bazel=e["started"]["buildToolVersion"])
        if "finished" in e:
            row.update(bazel_exit=e["finished"]["exitCode"].get("code", 0))
        if "targetCompleted" in e.get("id", {}) and e.get("completed", {}).get("success"):
            done.append(e["id"]["targetCompleted"]["label"])
    row["completed"] = done
    row["waiter_actions"], row["failed_actions"] = [], []
    for s in read_events(spawns, row["collection"]):
        if len(row["failed_actions"]) < 8 and (s.get("exitCode", 0) or s.get("status") not in (None, "", "SUCCESS")):
            row["failed_actions"].append({k: s.get(k) for k in ("targetLabel", "mnemonic", "exitCode", "runner", "status")})
        if s.get("targetLabel", "").endswith("//pkg/waiter:waiter") and s.get("mnemonic") == "GoCompilePkg":
            outputs = [o for o in s.get("actualOutputs", []) if o.get("path", "").endswith((".a", ".x")) and o.get("digest", {}).get("hash")]
            row["waiter_actions"].append({"outputs": outputs, "runner": s.get("runner"),
                                          "cache_hit": s.get("cacheHit", False), "platform": s.get("platform", {})})
    row["identity_ok"] = (row.get("bazel") == version and row.get("bazel_exit") == p.returncode
                          and bool(row.get("invocation")) and git("rev-parse", "HEAD") == sha)
    expected_error = 'Using the "go_deps.gazelle_override" tag in a non-root Bazel module is forbidden, but module "gvisor" requests it.'
    row["provider_rejected_gvisor"] = (expected_error in log and row["actual_client"] == "bazel " + version and
                                        git("rev-parse", "HEAD") == sha and row.get("bazel", version) == version and
                                        row.get("bazel_exit", p.returncode) == p.returncode)
    output_base = subprocess.run([*client(version), "info", "output_base"], cwd=work,
                                 capture_output=True, text=True)
    if not output_base.returncode:
        gazelle = Path(output_base.stdout.strip()) / "external/gazelle+"
        for filename in ("MODULE.bazel", "internal/bzlmod/go_deps.bzl"):
            f = gazelle / filename
            if f.is_file():
                row.setdefault("gazelle", {})[filename] = sha256(f.read_bytes()).hexdigest()
    actions = row["waiter_actions"]
    platform = {p["name"]: p["value"] for p in actions[0]["platform"].get("properties", [])} if len(actions) == 1 else {}
    row["gnu_waiter"] = bool(len(actions) == 1 and actions[0]["outputs"] and
                             platform.get("container-image") == "docker://" + os.environ["GNU"] and
                             (actions[0]["cache_hit"] or actions[0]["runner"] == "remote"))
    if not p.returncode and row["identity_ok"] and set(row["collection"].values()) == {"complete"} and row["gnu_waiter"] and any(x == "//:use_waiter" or x.endswith("//pkg/waiter:waiter") for x in done):
        row["status"] = "PASS"
    save()
    return row


def original_policy():
    archive = AREA / "original.zip"
    digest = sha256(archive.read_bytes()).hexdigest()
    assert digest == "c1226e0a4e0fc4d8320dc8c831fa0fd88e1807243d6d2da3c1a010f4013dcef8"
    with ZipFile(archive) as z:
        assert z.namelist() == ["external-go.json"]
        original = json.loads(z.read("external-go.json"))
    assert original["verdict"] == "INCOMPLETE"
    for name, source in (("master", MASTER), ("upgrade", UPGRADE), ("split", SPLIT), ("adopting", SPLIT)):
        row = original["builds"][name]
        assert row["source"] == source and row["exit"] and not row["waiter_actions"]
        assert any("No repository visible as '@platforms' from repository" in s for s in row["safe_error_lines"])
    root, adopted = (original["policies"][key] for key in ("gvisor_root", "adopting"))
    assert root[PROTO] == adopted[PROTO] and root[PROTO]["sha256"] == PATCHED
    assert not root[PROTO]["reads_executable"] and not root[PROTO]["opens_binary"]
    assert root[SHIM_DEP] == adopted[SHIM_DEP] and root[SHIM_DEP]["file"] == "BUILD.bazel"
    assert root[SHIM_DEP]["sha256"] == "bb91276848ddf5377501f5ea045a7094ddcd5af1460310c8007986cac5bf5876"
    RESULT.update(original={"run": 41397610786, "artifact": 13119828475, "sha256": digest,
                            "verdict": original["verdict"]}, policies=original["policies"])
    save()


def close_server(version, work):
    subprocess.run([*client(version), "shutdown"], cwd=work, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)


def main():
    original_policy()
    assert git("rev-parse", "HEAD^") == "f95fd151120faa38be862f8d1f2ab5887761139d"
    assert git("rev-parse", "HEAD^^") == SPLIT and git("rev-parse", SPLIT + "^") == UPGRADE
    assert git("rev-parse", UPGRADE + "^") == MASTER
    assert git("diff", "--name-only", UPGRADE, SPLIT) == "MODULE.bazel"
    assert not git("diff", MASTER, SPLIT, "--", "go.mod", "go.sum", "pkg/waiter",
                   "tools/protobuf.disable-binary-reading-on-init.patch")
    before = git("show", UPGRADE + ":MODULE.bazel")
    tags = before[before.index("go_deps.module_override("):] + "\n"
    assert tags.count("go_deps.gazelle_override(") == 9 and tags.count("go_deps.module_override(") == 1
    assert git("show", SPLIT + ":MODULE.bazel").endswith(tags.replace("go_deps.", "root_go_deps.").rstrip())
    RESULT["policy_tag_sha256"] = sha256(tags.encode()).hexdigest()
    for name, sha, version in (("master", MASTER, "8.8.1"), ("upgrade", UPGRADE, "9.3.1"),
                               ("split", SPLIT, "9.3.1"), ("adopting", SPLIT, "9.3.1")):
        select(sha)
        work = AREA / name
        manifest(work, tags if name == "adopting" else None)
        build(name, sha, version, work)
        close_server(version, work)
    checks = {
        "inherited": all(RESULT["builds"][n]["provider_rejected_gvisor"] and RESULT["builds"][n]["exit"] and
                         RESULT["builds"][n]["forbidden_tags"] == ["go_deps.gazelle_override"] for n in ("master", "upgrade")),
        "ordinary_split_build": RESULT["builds"]["split"]["status"] == "PASS",
        "adopting_build": RESULT["builds"]["adopting"]["status"] == "PASS",
    }
    RESULT.update(checks=checks, finished=now(), verdict="PASS" if all(checks.values()) else "INCOMPLETE")
    save()
    return not all(checks.values())


if __name__ == "__main__":
    raise SystemExit(main())
