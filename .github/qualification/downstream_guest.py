"""Build the published ARM64 release archive and exercise its packaged Sentry."""
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

WORK = Path(os.environ["RUNNER_TEMP"]) / "paired-stub-guest"
SOURCE = "d2c6bbebffcbfe30f30ed7b105efa703d80d65ee"
HISTORICAL_PROBE_CARRIER = "8d8327953fd0b9b267c14bdb87a1352219749d38"
PROBE = "c49fb618f55e96782877cd132c2bb28a36623f67c2cea77bfc4f192f5320aa22"

# -I excludes the script directory. Load only the fixture pinned by the workflow.
spec = importlib.util.spec_from_file_location("paired_stub_guest", WORK / "paired_stub_guest.py")
assert spec is not None and spec.loader is not None
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)
PAGE = (WORK / "page").read_text().strip()
assert PAGE in ("4k", "64k")
guest.GUESTS = {"page" + PAGE: guest.GUESTS["page" + PAGE]}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def elf_machine(data: bytes) -> int:
    assert data[:6] == b"\x7fELF\x02\x01", data[:6]
    return int.from_bytes(data[18:20], "little")


def build_receipt(name: str, flags: list[str], targets: list[str]) -> dict:
    bep = WORK / (name + ".bep")
    result = subprocess.run([*guest.BAZEL, "build", *flags, "--build_event_json_file=" + str(bep), *targets])
    receipt = {"exit": result.returncode, "targets": targets}
    with bep.open() as events:
        for line in events:
            event = json.loads(line)
            if "started" in event:
                receipt["invocation"] = event["started"]["uuid"]
            if "buildMetrics" in event:
                receipt["runners"] = event["buildMetrics"].get("actionSummary", {}).get("runnerCount", [])
    guest.save(name, receipt)
    result.check_returncode()
    return receipt


def compiler_evidence(flags):
    sources = {"sighandler_arm64.c", "sysmsg_lib.c", "sigrestorer_arm64.S", "syshandler_arm64.S"}
    expression = 'inputs("pkg/sentry/platform/systrap/sysmsg/[^/]+[.][cS]$", mnemonic("CppCompile", deps(' + guest.STUB + ')))'
    with (WORK / "stub-aquery.json").open("w") as output:
        subprocess.run([*guest.BAZEL, "aquery", *flags, "--output=jsonproto", "--include_param_files",
                        expression], stdout=output, check=True)
    graph = json.loads((WORK / "stub-aquery.json").read_text())
    owners = {str(t["id"]): t["label"] for t in graph["targets"]}
    rows = {}
    for action in graph["actions"]:
        argv = list(action["arguments"])
        argv.extend(a for param in action.get("paramFiles", []) for a in param.get("arguments", []))
        compiled = [Path(arg).name for arg in argv if Path(arg).name in sources]
        if not compiled:
            continue
        source, = set(compiled)
        assert source not in rows, source
        targets = [arg.split("=", 1)[1] for arg in argv if arg.startswith(("--target=", "-target="))]
        targets.extend(value for flag, value in zip(argv, argv[1:]) if flag in ("-target", "--target"))
        defines = [arg for i, arg in enumerate(argv) if re.match(r"^-DPAGE_SIZE(?:=|$)", arg)
                   or (i > 0 and argv[i - 1] == "-D" and re.match(r"^PAGE_SIZE(?:=|$)", arg))]
        rows[source] = {"driver": argv[0], "target": targets, "PAGE_SIZE_defines": defines,
                        "owner": owners[str(action["targetId"])], "actionKey": action["actionKey"], "arguments": argv}
    guest.save("compiler", rows)
    assert set(rows) == sources, rows
    for row in rows.values():
        assert "clang" in Path(row["driver"]).name and row["target"], row
        assert all(t.startswith("aarch64") for t in row["target"]) and row["PAGE_SIZE_defines"] == [f"-DPAGE_SIZE={4096 if PAGE == '4k' else 65536}"], row
    return rows



def prepare_release() -> None:
    git("checkout", "--quiet", "--detach", SOURCE)
    assert git("rev-parse", "HEAD") == SOURCE
    assert 'bazel_dep(name = "llvm", version = "0.8.27")' in Path("MODULE.bazel").read_text()
    assert os.uname().machine == "x86_64"
    flags = [*guest.BUILD_FLAGS, f"--//tools/bazeldefs:page_size={PAGE}"]
    receipt = build_receipt("cross_build", flags, ["//debian:gvisor-release-tar-bz2"])
    compiler_evidence(flags)
    # Verify the resolved checksum in Bazel’s own module information.
    repo = "@@llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-amd64"
    invocation = [*guest.BAZEL, "mod", "show_repo", "--lockfile_mode=off", "--distdir=" + str(WORK / "dist"), repo]
    info = subprocess.check_output(invocation, text=True)
    (WORK / "compiler-repo.txt").write_text(info)
    assert "08472d6a8e871bb38a425a233dbe974e125e138903a80a3255d12cd6ad137120" in info
    assert "llvm-23.1.2-3/llvm-toolchain-minimal-23.1.2-linux-amd64-musl.tar.zst" in info
    archive = WORK / "arm64-gvisor.tar.bz2"
    shutil.copyfile("bazel-bin/debian/gvisor.tar.bz2", archive)
    inputs = WORK / "inputs" / "release"
    members = {}
    with tarfile.open(archive, "r:bz2") as tar:
        for entry in tar:
            if entry.isdir():
                assert entry.name in ("gvisor-bin", "./gvisor-bin"), entry.name
                continue
            name = entry.name.removeprefix("./")
            assert entry.isfile() and name in guest.MEMBERS and name not in members, entry.name
            assert entry.mode & 0o111, name
            with tar.extractfile(entry) as source:
                content = source.read()
            assert elf_machine(content) == 183, name
            output = inputs / name
            output.parent.mkdir(exist_ok=True, parents=True)
            output.write_bytes(content)
            output.chmod(0o755)
            members[name] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    assert set(members) == guest.MEMBERS, members.keys()
    (WORK / "release-manifest.sha256").write_text("".join(f"{members[k]['sha256']}  {k}\n" for k in sorted(members)))
    record = {"source": SOURCE, "members": members, "release_verified": True,
              "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "arm64": receipt,
              "page_size_flag": f"--//tools/bazeldefs:page_size={PAGE}"}
    guest.save("release", record)
    git("checkout", "--quiet", "--detach", HISTORICAL_PROBE_CARRIER)


def prepare_probe():
    record = json.loads((WORK / "release.json").read_bytes())
    assert record["release_verified"] and git("rev-parse", "HEAD") == HISTORICAL_PROBE_CARRIER
    target = "//test/rbe/paired_stub_guest:pagesize_probe"
    options = [*guest.BUILD_FLAGS, "--platforms=@llvm//platforms:linux_aarch64_musl"]
    subprocess.run([*guest.BAZEL, "build", *options, target], check=True)
    probe = Path("bazel-bin/test/rbe/paired_stub_guest/pagesize_probe")
    assert guest.sha(probe.read_bytes()) == PROBE
    inputs = WORK / "inputs"
    shutil.copyfile(probe, inputs / "probe")
    (inputs / "probe").chmod(0o755)
    manifest = inputs / "manifest.sha256"
    manifest.write_text("".join(f"{guest.sha(p.read_bytes())}  {p.relative_to(inputs)}\n"
                                for p in sorted(inputs.rglob("*")) if p.is_file()))
    record.update(probe_sha256=PROBE, probe_original_artifact=13956109226,
                  probe_platform="@llvm//platforms:linux_aarch64_musl",
                  manifest_sha256=guest.sha(manifest.read_bytes()))
    guest.save("release", record)
    record["verified"] = True
    guest.save("release", record)


def inputs_check():
    targets = {"//test/rbe/paired_stub_guest:" + name for name in guest.GUESTS}
    report = {}
    for line in (WORK / "guest-inputs.bep").open():
        event = json.loads(line)
        if "started" in event:
            report["invocation"] = event["started"]["uuid"]
        assert "testResult" not in event
        ident = event["id"].get("targetCompleted", {})
        if (target := ident.get("label")) in targets and not ident.get("aspect"):
            completed = event.get("completed", {})
            groups = [o for o in completed.get("outputGroup", []) if o["name"] == "guest_inputs"]
            report[target] = bool(completed.get("success") and len(groups) == 1 and groups[0].get("fileSets"))
    guest.save("guest-inputs", report)
    assert all(report.get(target) for target in targets), report


if __name__ == "__main__":
    command, = sys.argv[1:]
    if command == "release":
        prepare_release()
    elif command == "probe":
        prepare_probe()
    elif command == "check":
        inputs_check()
    elif command == "collect":
        guest.SOURCE = SOURCE
        raise SystemExit(guest.collect())
    else:
        raise ValueError(command)
