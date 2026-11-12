"""Exercise the previously linked race guest using the ordinary boot TestRun."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET


arch = sys.argv[1]
cpu, config, machine, stock_hash, image = {
    "amd64": ("k8", "x86_64", 62, "00c154c85d5b7d37e88c7396bd2a0bd96cd2f7b56529846df6e2ebc8d6f545a1",
              "d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"),
    "arm64": ("aarch64", "aarch64", 183, "91690f1ef38b5ae58824cf15e9a71ae888f04eee39b23b9d154f0a923c3bdb74",
              "b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"),
}[arch]
root = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
out = root / "public"
target = "//runsc/boot:boot_test"
platform = "//test/rbe/release_prewarmer:" + arch
invocation = str(uuid.uuid4())
bep = root / (arch + ".bep")
compact = root / (arch + ".compact")
log = root / (arch + ".log")
receipt = dict(arch=arch, source="e47624715b3e7be80ecc404bdc178c30ffb5a3f8", target=target,
               invocation=invocation, expected_image=image, reference_guest_sha256=stock_hash)


def save():
    (out / (arch + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")


save()
command = [
    "timeout", "--signal=INT", "--kill-after=30", "2700", str(root / "bin/bazel"),
    "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(root / "private.bazelrc"), "test",
    "--config=" + config, "--config=race", "--extra_execution_platforms=" + platform, "--host_platform=" + platform,
    "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
    "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
    "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
    "--noincompatible_exclude_starlark_flags_from_exec_config", "--lockfile_mode=off",
    "--jobs=32", "--color=no", "--curses=no", "--incompatible_use_native_posix_test_wrapper",
    "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1", "--test_timeout=180",
    "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_filter=^TestRun$",
    "--remote_download_outputs=toplevel", "--remote_accept_cached",
    "--remote_download_regex=^bazel-out/[^/]+/(testlogs/runsc/boot/boot_test/test[.](log|xml)|bin/test/cmd/test_app/.*test_app(_bin)?)$",
    "--nobuild_event_json_file_path_conversion", "--invocation_id=" + invocation,
    "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact), target,
]
with log.open("wb") as output:
    receipt["bazel_exit"] = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT).returncode
receipt["diagnostics"] = []
for line in log.read_text(errors="replace").splitlines():
    if any(marker in line.lower() for marker in ("authorization", "token=", "api-key", "remote_header", "bes_header")):
        continue
    if line.startswith(("ERROR:", "FAILED:", "FATAL:", "fatal error:")) or ": error:" in line:
        receipt["diagnostics"].append(line[:1200])
        if len(receipt["diagnostics"]) == 12:
            break
save()
if bep.exists():
    for raw in bep.open():
        event = json.loads(raw)
        identity = event.get("id", {})
        if "finished" in event:
            receipt["finished"] = event["finished"].get("exitCode")
        result = event.get("testResult")
        if not result or identity["testResult"].get("label") != target:
            continue
        receipt["status"] = result.get("status")
        receipt["execution"] = result.get("executionInfo", {}).get("strategy")
        for artifact in result.get("testActionOutput", []):
            name = artifact.get("name")
            if name not in ("test.log", "test.xml"):
                continue
            uri = urlsplit(artifact["uri"])
            path = Path(unquote(uri.path)) if uri.scheme == "file" else Path("bazel-testlogs/runsc/boot/boot_test") / name
            rel = path.resolve().relative_to(Path("bazel-out").resolve())
            assert rel.parts[0].startswith(cpu + "-") and rel.parts[1:] == ("testlogs", "runsc", "boot", "boot_test", name)
            assert path.is_file() and 0 < path.stat().st_size <= 4 * 1024 * 1024
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert not artifact.get("digest") or artifact["digest"] == digest
            if uri.scheme == "bytestream":
                assert uri.path.endswith("/blobs/" + digest + "/" + str(path.stat().st_size))
            shutil.copyfile(path, out / (arch + "-" + name))
            receipt[name] = digest
            if name == "test.xml":
                receipt["cases"] = [
                    {"name": test.get("name"), "outcome": [child.tag for child in test if child.tag in ("skipped", "failure", "error")],
                     "status": test.get("status")} for test in ET.parse(path).iter("testcase")
                ]
save()
if compact.exists() and compact.stat().st_size:
    query = """
    .spawn? | select(.mnemonic == "TestRunner" or (.mnemonic == "GoLink" and (.targetLabel | contains("//test/cmd/test_app:")))) |
    {owner:.targetLabel, mnemonic, runner, cache:(.cacheHit // false), exit:(.exitCode // 0),
     xml:(.args[1] == "--generate-xml"), race:(.args | any(. == "-race")),
     platform: [.platform.properties[]? | select(.name == "Arch" or .name == "container-image")]}
    """
    with subprocess.Popen([root / "bb", "print", "--raw", "--compact_execution_log=" + str(compact)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        answer = subprocess.run(["jq", "-c", query], stdin=decoder.stdout, capture_output=True, check=True)
        decoder.stdout.close()
        receipt["actions"] = [json.loads(line) for line in answer.stdout.splitlines()]
        receipt["decoder_exit"] = decoder.wait()
save()
guest = Path("bazel-bin/test/cmd/test_app/test_app")
if guest.is_file():
    binary = guest.read_bytes()
    assert binary.startswith(b"\x7fELF")
    receipt["guest_machine"] = int.from_bytes(binary[18:20], "little")
    receipt["guest_sha256"] = hashlib.sha256(binary).hexdigest()
save()
assert receipt["bazel_exit"] == 0 and receipt.get("status") == "PASSED", receipt["diagnostics"]
assert [(row["name"], row["outcome"]) for row in receipt["cases"]] == [("TestRun", [])]
assert receipt["cases"][0]["status"] not in ("notrun", "suppressed")
runners = [row for row in receipt["actions"] if row["mnemonic"] == "TestRunner" and not row["xml"]]
assert len(runners) == 1 and runners[0]["runner"] == "remote" and not runners[0]["cache"]
properties = {prop["name"]: prop["value"] for prop in runners[0]["platform"]}
assert properties == {"Arch": arch, "container-image": "docker://gcr.io/distroless/static-debian13@sha256:" + image}
assert receipt["guest_machine"] == machine
assert receipt["guest_sha256"] == stock_hash or any(row["race"] for row in receipt["actions"] if row["mnemonic"] == "GoLink")
