"""Exercise the current upstream SDK builder on the original minimal AMD64 worker."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

import sdk_native as native


WORK = native.WORK
ROOT = Path("bazel-bin")
SDK = native.owner("amd64").split("//")[0] + "//:"
FIXTURE = "//test/rbe/sdk_links:"
PRODUCTS = ("builder", "pack.exe", "covdata.exe")
WRAPPER = "100f88fb915d40a22588281d993a0dad877b46049a4490d11f64aefefa2e2215"
CASES = (
    ("stock-shebang", SDK, False, None),
    ("declared-shell", SDK, True, None),
    ("relative", FIXTURE, True, None),
    ("relative-readlink", FIXTURE, True, "readlink"),
    ("relative-both", FIXTURE, True, "both"),
)


def safe_diagnostics(output):
    patterns = [
        r".{0,55}(?:readlink|dirname): (?:command )?not found",
        r"binary_wrapper\.sh.{0,120}(?:no such file or directory|not found)",
        r"fork/exec .{0,160}binary_wrapper\.sh.{0,90}",
        r"ERROR: [^\n]{0,500}",
        r"(?:failed to|cannot) (?:start|exec|embed)[^\n]{0,200}",
    ]
    return list(dict.fromkeys(
        re.sub(r"(?i)(remote_header|api.key|authorization).*", "[redacted]", match).strip()
        for pattern in patterns for match in re.findall(pattern, output, flags=re.I)
    ))[:12]


def measure(case, prefix, declared, helpers):
    log = WORK / (case + ".log")
    spawnfile = WORK / (case + ".spawns")
    inv = str(uuid.uuid4())
    args = [
        *native.BAZEL, "--bazelrc=" + str(WORK / "remote.bazelrc"), "build",
        *native.flags("amd64", declared), "--spawn_strategy=remote", "--remote_download_outputs=toplevel",
        "--remote_timeout=300", "--action_env=SDK_LINK_CASE=" + case, "--invocation_id=" + inv,
        "--execution_log_json_file=" + str(spawnfile),
    ]
    if helpers:
        args.append("--define=SDK_LINK_PATH=" + helpers)
    if prefix == FIXTURE:
        args.append(prefix + "witness")
    args.extend(prefix + name for name in PRODUCTS)
    with log.open("wb") as stream:
        try:
            status = subprocess.run(args, stdout=stream, stderr=stream, timeout=740).returncode
        except subprocess.TimeoutExpired:
            status = 124
    console = log.read_text(errors="replace")
    row = {"case": case, "owner": prefix + "builder", "invocation": inv, "bazelExit": status,
           "diagnostics": safe_diagnostics(console), "spawns": [], "files": {}}
    result = WORK / "results" / (case + ".json")
    result.write_text(json.dumps(row, indent=2) + "\n")
    for spawn in native.spawns(spawnfile):
        if spawn.get("mnemonic") not in ("GoToolchainBinaryBuild", "WitnessGoSDKLink"):
            continue
        command = spawn.get("commandArgs", [])
        env = {item["name"]: item.get("value", "") for item in spawn.get("environmentVariables", [])}
        props = {item["name"]: item.get("value", "") for item in spawn.get("platform", {}).get("properties", [])}
        outputs = {Path(f["path"]).name: f.get("digest", {}).get("hash") for f in spawn.get("actualOutputs", [])}
        wrappers = [f.get("digest", {}).get("hash") for f in spawn.get("inputs", [])
                    if f["path"].endswith("/go/private/rules/binary_wrapper.sh")]
        row["spawns"].append({"mnemonic": spawn["mnemonic"], "owner": spawn.get("targetLabel"),
                              "runner": spawn.get("runner"), "cache": bool(spawn.get("cacheHit")),
                              "exit": int(spawn.get("exitCode", 0)), "status": native.signals(spawn.get("status", "")),
                              "diagnostics": safe_diagnostics(spawn.get("status", "")),
                              "argv0": command[0] if command else None, "wrapper": wrappers,
                              "GO_BINARY": env.get("GO_BINARY"), "PATH": env.get("PATH"),
                              "worker": {k: props.get(k) for k in native.expected("amd64")}, "outputs": outputs})
    root = ROOT / ("external/" + prefix.lstrip("@").split("//")[0] if prefix == SDK else "test/rbe/sdk_links")
    if prefix == FIXTURE:
        witness = root / "relative/link.txt"
        if witness.is_file():
            row["witness"] = witness.read_text().splitlines()
    if not status:
        for name in PRODUCTS:
            path = root / name
            with path.open("rb") as stream:
                header = stream.read(64)
                stream.seek(0)
                row["files"][name] = {"bytes": path.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                                      "elf64le": header[:6] == b"\x7fELF\x02\x01", "machine": int.from_bytes(header[18:20], "little")}
    result.write_text(json.dumps(row, indent=2) + "\n")
    print(case, status, row["diagnostics"][:3], flush=True)
    return row


def main():
    rows = {case[0]: measure(*case) for case in CASES}
    for name, row in rows.items():
        builder, = (s for s in row["spawns"] if s["mnemonic"] == "GoToolchainBinaryBuild")
        assert builder["runner"] == "remote" and not builder["cache"], (name, builder)
        assert builder["worker"] == native.expected("amd64") and builder["wrapper"] == [WRAPPER], name
        if name == "stock-shebang":
            assert row["bazelExit"] and builder["argv0"].endswith("/binary_wrapper.sh"), name
            assert any("no such file" in msg.lower() for msg in row["diagnostics"] + builder["diagnostics"]), name
            continue
        assert builder["argv0"].endswith("/test/rbe/go_bootstrap/ash"), name
        if name.startswith("relative"):
            witness = row.get("witness", [])
            assert witness and witness[0].startswith("relative-target=../") and witness[0].endswith("/bin/go"), (name, witness)
        if not row["bazelExit"]:
            for product in PRODUCTS:
                entry = row["files"][product]
                assert entry["elf64le"] and entry["machine"] == 62 and entry["bytes"] > 1024, name
                assert entry["sha256"] == builder["outputs"][product], (name, product)
        else:
            want = {"relative": "readlink", "relative-readlink": "dirname"}.get(name)
            assert want and any(want + ":" in msg for msg in row["diagnostics"]), name
    assert not rows["declared-shell"]["bazelExit"] and not rows["relative-both"]["bazelExit"]
    for product in PRODUCTS:
        assert rows["declared-shell"]["files"][product]["sha256"] == rows["relative-both"]["files"][product]["sha256"], product


if __name__ == "__main__":
    main()
