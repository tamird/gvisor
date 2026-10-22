"""Check selected BusyBox configuration, ELF closure and native runner commands."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from image_archive import archive, identity, resolve

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
builds = json.loads(Path(sys.argv[1]).read_text())
contents = json.loads(Path(sys.argv[2]).read_text())
destination = Path(sys.argv[3])
records = {"source": builds["source"], "status": "INCOMPLETE", "architectures": [], "commands": []}
applets = ["basename", "cat", "cut", "dirname", "grep", "realpath", "sed", "sh",
           "sleep", "sort", "tee", "tr", "uniq", "wc"]
required = {line for line in Path("images/busybox/pidfd.config").read_text().splitlines()
            if line.startswith("CONFIG_")}
container_name = "qualification-pidfd-busybox-probe"


def run(arguments: list[str], expected: int = 0) -> str:
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("BusyBox probe exhausted existing work deadline")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=min(60, remaining))
    records["commands"].append({"arguments": arguments, "native_exit": result.returncode,
                                "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode != expected:
        raise RuntimeError(f"Command returned {result.returncode}, expected {expected}: {arguments}")
    return result.stdout


try:
    for build, machine, image_arch in zip(builds["architectures"],
            ("Advanced Micro Devices X86-64", "AArch64"), ("amd64", "arm64"), strict=True):
        outputs = build["outputs"]
        match outputs["binary"], outputs["config"]:
            case [binary_record], [config_record]:
                binary = Path(binary_record["output"])
                config = Path(config_record["retained"])
            case _:
                raise ValueError("Expected one BusyBox binary and configuration")
        configured = set(config.read_text().splitlines())
        row = {"architecture": build["architecture"], "binary": identity(binary),
               "config": identity(config), "missing_config": sorted(required - configured)}
        records["architectures"].append(row)
        if row["missing_config"]:
            raise ValueError(f"Resolved configuration dropped required options: {row['missing_config']}")
        row["file_header"] = run(["readelf", "--file-header", str(binary)])
        row["program_headers"] = run(["readelf", "--program-headers", str(binary)])
        row["dynamic"] = run(["readelf", "--dynamic", str(binary)])
        if machine not in row["file_header"] or "INTERP" in row["program_headers"] or "(NEEDED)" in row["dynamic"]:
            raise ValueError("BusyBox is not static for its selected target")
        tools = outputs["tools"]
        if len(tools) != 2 or any(item.get("missing") for item in tools):
            raise ValueError("Expected the actual execution HOSTCC and target NM wrappers")
        for item in tools:
            text = Path(item["retained"]).read_text()
            if Path(item["output"]).name.endswith("busybox_hostcc"):
                link = shlex.split(text.splitlines()[-1])
                row["hostcc_link_arguments"] = link
                if "-c" in link or "-E" in link or "-S" in link or "-o" in link:
                    raise ValueError("Combined HOSTCC invocation contains compile-only/output flags")
        if "hostcc_link_arguments" not in row:
            raise ValueError("Actual HOSTCC wrapper was not identified")
        scripts = [Path(item["retained"]).read_text() for item in outputs["logs"]
                   if not item.get("missing") and Path(item["output"]).name == "build_script.sh"]
        if len(scripts) != 1 or "ARCH=" + build["architecture"] not in scripts[0]:
            raise ValueError("Generated Make script omitted selected architecture")
        inspected = next(item for item in contents["architectures"] if item["architecture"] == image_arch)
        image = {}
        _, files, _ = archive(Path(inspected["archive"]["path"]), image)
        if image["archive"] != inspected["archive"] or image["config_sha256"] != inspected["config_sha256"]:
            raise ValueError("Image changed after canonical inspection")
        if files["bin/busybox"]["sha256"] != row["binary"]["sha256"] or files["bin/busybox"]["mode"] != 0o755:
            raise ValueError("Installed BusyBox differs from its configured binary")
        for applet in applets:
            if resolve(files, "bin/" + applet) != "bin/busybox":
                raise ValueError("Applet does not resolve to selected BusyBox: " + applet)
        if files["usr/share/licenses/busybox/LICENSE"]["sha256"] != "bbfc9843646d483c334664f651c208b9839626891d8f17604db2146962f43548":
            raise ValueError("BusyBox license differs from pinned source")
        row["image"] = inspected["archive"]
        row["status"] = "PASS"
    native = contents["architectures"][0]
    run(["docker", "load", "--input", native["archive"]["path"]])
    image_id = "sha256:" + native["config_sha256"]
    actual_id = run(["docker", "image", "inspect", "--format={{.Id}}", image_id]).strip()
    if actual_id != image_id:
        raise ValueError("Loaded native image identity differs")
    base = ["docker", "run", "--rm", "--runtime=runc", "--name", container_name,
            "--entrypoint=/bin/sh", image_id, "-ec"]
    available = run([*base, "/bin/busybox --list"]).splitlines()
    records["applets"] = available
    if not set(applets) <= set(available):
        raise ValueError("BusyBox applet inventory omits reached commands")
    run([*base, r'''set -o pipefail
if (exit 23) | cat; then exit 91; else test "$?" -eq 23; fi
test "$(printf ' a\tB\n' | tr -d '[:blank:][:cntrl:]')" = aB
test "$(printf 'a-b' | tr -c '[:alnum:]_' '_')" = a_b
test "$(printf abc | tr '[:lower:]' '[:upper:]')" = ABC
printf 'b\na\na\n' | sort | uniq | tee /tmp/lines | grep -q '^a$'
test "$(wc -l < /tmp/lines)" -eq 2
test "$(printf 'x:y' | cut -d: -f2)" = y
test "$(printf 'abc' | sed 's/b/X/')" = aXc
test "$(basename /tmp/lines)" = lines
test "$(dirname /tmp/lines)" = /tmp
test "$(realpath /tmp/../tmp)" = /tmp
sleep 0
/usr/bin/timeout --version
'''])
    run([*base, "/usr/bin/timeout 5 /bin/sh -c 'exit 7'"], expected=7)
    run([*base, "/usr/bin/timeout --foreground 1 /bin/sleep 5"], expected=124)
    run([*base, r'''set +e
/usr/bin/timeout --kill-after=2 1 /bin/sh -c '
  /bin/sh -c '\''trap "echo child-TERM > /tmp/group-signal; exit 0" TERM; echo ready > /tmp/group-ready; sleep 30; wait'\'' &
  wait
'
status=$?
test "$status" -eq 124 || exit 92
test "$(cat /tmp/group-ready)" = ready || exit 93
# The descendant receives the same process-group signal as the timed shell.
for attempt in 1 2 3; do test -s /tmp/group-signal && break; sleep 1; done
test "$(cat /tmp/group-signal)" = child-TERM
'''])
    records["status"] = "PASS"
finally:
    try:
        cleanup = subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True, timeout=10)
        records["container_cleanup"] = {"native_exit": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr}
    except (OSError, subprocess.SubprocessError) as error:
        records["status"] = "FAIL"
        records["container_cleanup"] = {"error": str(error)}
        raise
    finally:
        destination.write_text(json.dumps(records, indent=2) + "\n")
