"""Preserve the actual local GoStandardLibraryAnalysis result for each client."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lean_release_client as lean

OWNER = "//tools/nogo:stdlib"
MNEMONIC = "GoStandardLibraryAnalysis"
ORDINARY = "ubuntu@sha256:78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d"
ORIGINAL = {"amd64": "2609d70954127a4b236f69605e4bbcd45f05f4304c5c8775dcb6bb91cf32983c",
            "arm64": "cab9490943c849485c5af1ed5b67680a8f8e385c14cfb99780ee04264731cc4a"}
ORIGINAL_CONFIG = {
    "amd64": "972a6199381616c3a7b20ca091ab10d987df377ff45b20957a5a70b4162ca3c0",
    "arm64": "10478b6bef2f2fb27f55cc9071696fa6fe0d843eeb24baf6169fb650fa9e6d44",
}
OUT = lean.STAGE.parent / "nogo-owner.json"
report = {"source": lean.SOURCE, "tree": lean.TREE, "ordinary_image": ORDINARY,
          "original_artifact": 13581077103, "original_stderr": ORIGINAL, "arms": {}}


def persist():
    data = json.dumps(report, indent=2)
    assert len(data) < 95000
    OUT.write_text(data + "\n")


def clean_line(line):
    # This is the original stderr from only the named analyzer in the
    # uncredentialed image. Preserve error text; remove machine identifiers.
    line = re.sub(r"/home/runner/\.cache/bazel/_bazel_[^/ ]+/[^/ ]+/", "<output-base>/", line)
    line = re.sub(r"https?://[^\s]+", "<url>", line)
    line = re.sub(r"\b0x[0-9a-fA-F]{7,}\b", "<address>", line)
    if re.search(r"(?i)authorization|password|github_pat_|gh[pousr]_|access.token|secret|api.key", line):
        return "<redacted line>"
    return "".join(c if c.isprintable() else " " for c in line)[:550]


def evidence(content):
    lines = content.decode(errors="replace").splitlines()
    selected = list(dict.fromkeys([*lines[:16], *lines[-4:]]))
    return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            "lines": [clean_line(s) for s in selected][:20]}


def container(mode, slot):
    command = lean.container(slot)
    if mode == "ordinary":
        assert command.pop() == "lean-client:probe"
        for name in ("probe", "trust"):
            command.extend(lean.mount(lean.STAGE / "image" / name, "/" + name, True))
        # The two ordinary distro tool directories and distribution libraries
        # come from the fixed public image, never the CI runner's filesystem.
        command.extend(["-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"])
        command.extend(lean.mount(lean.STAGE / "image/etc/ssl/certs/ca-certificates.crt",
                                  "/etc/ssl/certs/ca-certificates.crt", True))
        command.append(report["ordinary_image_id"])
    return command


def inspect_go(cfg, argv, env):
    roots = list((lean.STAGE / "home/.cache/bazel").glob("_bazel_*/*/execroot/_main"))
    if len(roots) != 1:
        return {"execroot_count": len(roots)}
    root = Path("/home/runner") / roots[0].relative_to(lean.STAGE / "home")
    row = {"exists": True}
    for arg in [x for x in argv if x.startswith(("-go=", "-GOVERSION-mod-file="))]:
        name, value = arg.split("=", 1)
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            continue
        try:
            f = lean.container_file(root / path)
            row[name[1:]] = {"path": lean.public_path(value), "sha256": lean.digest(f)}
        except (OSError, AssertionError):
            row[name[1:]] = {"path": lean.public_path(value), "visible": False}
    goroot = env.get("GOROOT", "")
    row["GOROOT"] = lean.public_path(goroot)
    if goroot and not Path(goroot).is_absolute() and ".." not in Path(goroot).parts:
        archive_dir = roots[0] / goroot / "pkg" / ("linux_" + cfg)
        if archive_dir.is_dir():
            archives = sorted(p for p in archive_dir.rglob("*.a"))
            row["stdlib_archives"] = {
                "count": len(archives), "symlinks": sum(p.is_symlink() for p in archives),
                "names_sha256": hashlib.sha256("\n".join(str(p.relative_to(archive_dir))
                                                         for p in archives).encode()).hexdigest()}
    row["source_count"] = sum(a.endswith(".go") for a in argv)
    row["source_names_sha256"] = hashlib.sha256("\n".join(a for a in argv if a.endswith(".go")).encode()).hexdigest()
    return row


def arm(mode, arch):
    slot = lean.STAGE / (mode + "-" + arch)
    slot.mkdir()
    row = report["arms"][slot.name] = {}
    persist()
    client = container(mode, slot)
    bazel = [*client, "/probe/bazel", "--batch", "--nosystem_rc", "--nohome_rc",
             "--host_jvm_args=-Xmx5g", "--host_jvm_args=-Djava.io.tmpdir=/jvm-tmp",
             "--host_jvm_args=-DZstdTempFolder=/jvm-tmp",
             "--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts",
             "--host_jvm_args=-Djavax.net.ssl.trustStoreType=JKS"]
    flags = ["--config=" + lean.ARCHES[arch][0], "-c", "opt", "--jobs=4", "--spawn_strategy=local",
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain", "--symlink_prefix=/",
             "--repository_cache=/downloads", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=",
             "--disk_cache=", "--bes_backend=", "--color=no", "--curses=no", "--noshow_progress"]
    with (slot / "console").open("wb") as out:
        row["build_exit"] = subprocess.run([*bazel, "build", *flags, "--build_event_publish_all_actions",
            "--build_event_json_file=/data/bep", OWNER], stdout=out, stderr=out, timeout=2100).returncode
    row["console_sha256"] = lean.digest(slot / "console")
    persist()
    cpu = {"amd64": "k8", "arm64":"aarch64"}[arch]
    configs = {}
    actions = []
    outputs = []
    for line in (slot / "bep").open():
        e = json.loads(line)
        ident = e["id"]
        if "configuration" in e:
            configs[ident["configuration"]["id"]] = e["configuration"].get("cpu")
        if ident.get("targetCompleted", {}).get("label") == OWNER and "completed" in e:
            outputs.append([ident["targetCompleted"]["configuration"]["id"], e["completed"].get("success", False)])
        if "finished" in e:
            row["finished"] = e["finished"].get("exitCode", {}).get("code", 0)
        if "buildMetrics" in e:
            row["runner_counts"] = {x["name"]: int(x["count"]) for x in e["buildMetrics"].get(
                "actionSummary", {}).get("runnerCount", []) if x.get("count")}
        a = e.get("action", {})
        i = ident.get("actionCompleted", {})
        if i.get("label") == OWNER and a.get("type") == MNEMONIC:
            act = {"owner": OWNER, "mnemonic": MNEMONIC, "primary": lean.public_path(i["primaryOutput"]),
                   "success": a.get("success", False), "configuration": i["configuration"]["id"],
                   "exit": a.get("exitCode", 0)}
            act["same_original_config_and_primary"] = (
                act["configuration"] == ORIGINAL_CONFIG[arch]
                and act["primary"] == f"bazel-out/{cpu}-opt/bin/tools/nogo/stdlib.facts")
            args = a.get("commandLine", [])
            act["bep_argv_sha256"] = hashlib.sha256(json.dumps(args).encode()).hexdigest() if args else None
            f = a.get("stderr", {})
            if "uri" in f:
                p = lean.home_file(f["uri"])
                assert p.stat().st_size < 16 << 20
                act["stderr"] = evidence(p.read_bytes())
                act["matches_original_stderr"] = act["stderr"]["sha256"] == ORIGINAL[arch]
            primary = a.get("primaryOutput", {})
            if act["success"] and primary.get("uri"):
                path = lean.home_file(primary["uri"])
                act["primary_output"] = {"sha256": lean.digest(path), "bytes": path.stat().st_size}
            if a.get("failureDetail"):
                act["failure"] = evidence(a["failureDetail"].get("message","").encode())
            actions.append(act)
    row["actions"] = actions
    row["targets"] = [[configs.get(k), ok] for k, ok in outputs]
    persist()
    query = subprocess.run([*bazel, "aquery", *flags, "--output=jsonproto", "--include_artifacts=false",
                            f'mnemonic("{MNEMONIC}", {OWNER})'], capture_output=True, timeout=330)
    row["aquery_exit"] = query.returncode
    if not query.returncode:
        graph = json.loads(query.stdout)
        targets = {str(t["id"]):t["label"].lstrip("@") for t in graph["targets"]}
        [a] = [a for a in graph["actions"] if targets[str(a["targetId"])] == OWNER and a["mnemonic"] == MNEMONIC]
        cfg = next(c for c in graph["configuration"] if str(c["id"]) == str(a["configurationId"]))
        row["aquery_configuration"] = {"checksum": cfg.get("checksum"), "platform_name": cfg.get("platformName"),
                                      "mnemonic": cfg.get("mnemonic")}
        row["same_bep_and_aquery_configuration"] = (
            len(actions) == 1 and cfg.get("checksum") == actions[0]["configuration"]
            and configs.get(actions[0]["configuration"]) == cpu)
        argv = a["arguments"]
        env = {e["key"]:e["value"] for e in a.get("environmentVariables",[])}
        row["analyzer"] = {"argv0": lean.public_path(argv[0]),
                           "arguments_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
                           "environment": {k: lean.public_path(v) for k,v in env.items() if k in
                                           ("GOARCH","GOOS","CGO_ENABLED","GOROOT","GOTOOLCHAIN","PATH","HOME")},
                           "GO_args": [v for v in argv if v.startswith(("-GOOS=", "-GOARCH=", "-tags="))],
                           "action_key": a.get("actionKey"), "environment_keys": sorted(env), "execution_info": a.get("executionInfo",[]),
                           "tool_paths": inspect_go(arch, argv, env)}
    row["qualified_observation"] = (len(actions) == 1 and row["targets"] == [[cpu, row["build_exit"] == 0]]
        and row["finished"] == row["build_exit"] and row["aquery_exit"] == 0
        and row.get("same_bep_and_aquery_configuration") and actions[0]["same_original_config_and_primary"]
        and actions[0]["success"] == (row["build_exit"] == 0)
        and (row["build_exit"] == 0 or bool(actions[0].get("stderr",{}).get("lines"))))
    persist()


def main():
    persist()
    try:
        lean.setup()
        for repository in ("docker.io/library/ubuntu", "public.ecr.aws/ubuntu/ubuntu"):
            image = repository + ORDINARY[ORDINARY.index("@"):]
            pulled = subprocess.run(["docker","pull","--platform=linux/amd64",image],capture_output=True)
            if pulled.returncode == 0:
                break
        assert pulled.returncode == 0
        image_id = subprocess.check_output(["docker","image","inspect","-f","{{.Id}}",image],text=True).strip()
        assert image_id == "sha256:d9b8c34d61a9b9a3c44cab883d7d2f2619d674966ab520186f799cf32e3e0377"
        report["ordinary_image_id"] = image_id
        report["empty_bin_dirs"] = lean.report["system_bin_entries"]
        report["affinity_cpus"] = len(os.sched_getaffinity(0))
        report["host_memtotal_kib"] = int(re.search(r"MemTotal:\s+(\d+)",Path("/proc/meminfo").read_text())[1])
        report["docker_resources"] = {
            "bytes_and_cpus": subprocess.check_output(
                ["docker", "info", "--format", "{{.MemTotal}} {{.NCPU}}"], text=True).strip(),
            "container_memory_override": None, "container_cpu_override": None}
        for mode in ("empty","ordinary"):
            if mode == "ordinary":
                (lean.STAGE / "home").rename(lean.STAGE / "completed_empty_home")
                (lean.STAGE / "home").mkdir()
            for arch in lean.ARCHES:
                try:
                    arm(mode, arch)
                except (OSError, AssertionError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
                    report["arms"][mode+"-"+arch]["observer_error"] = type(exc).__name__ + " " + clean_line(str(exc))
                    persist()
        report["all_observations_qualified"] = all(r.get("qualified_observation") for r in report["arms"].values())
    finally:
        persist()
    return 0 if report.get("all_observations_qualified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
