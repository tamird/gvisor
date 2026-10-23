"""Verify compiled selftests, their installed sources and timeout dependencies."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

from image_archive import archive, configured_inputs, identity, resolve

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
destination = Path(sys.argv[1])
output_base = Path(os.environ["APPLICABILITY_OUTPUT_BASE"])
options = ["--lockfile_mode=error", "--noannounce_rc", "--compilation_mode=opt",
           "--downloader_config=" + os.environ["APPLICABILITY_DOWNLOADER_CONFIG"]]
startup = ["--output_base=" + str(output_base),
           "--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"]]
programs = ["pidfd_test", "pidfd_fdinfo_test", "pidfd_open_test", "pidfd_poll_test",
            "pidfd_wait", "pidfd_getfd_test", "pidfd_setns_test"]
installed_sources = {
    "src/pidfd-tests/pidfd/" + name: "tools/testing/selftests/pidfd/" + name
    for name in [name + ".c" for name in programs] + ["Makefile", "config", "pidfd.h"]
}
installed_sources.update({
    "opt/kselftest/run_kselftest.sh": "tools/testing/selftests/run_kselftest.sh",
    "opt/kselftest/kselftest/runner.sh": "tools/testing/selftests/kselftest/runner.sh",
    "opt/kselftest/kselftest/prefix.pl": "tools/testing/selftests/kselftest/prefix.pl",
    "usr/share/licenses/pidfd-tests/COPYING": "COPYING",
    "usr/share/licenses/pidfd-tests/GPL-2.0": "LICENSES/preferred/GPL-2.0",
    "usr/share/licenses/pidfd-tests/Linux-syscall-note": "LICENSES/exceptions/Linux-syscall-note",
    "usr/share/licenses/pidfd-tests/license-rules.rst": "Documentation/process/license-rules.rst",
})
labels = ["//images/basic:" + name for name in [*programs, "pidfd_tests_archive", "pidfd_tests_context", "pidfd_test_list"]]
labels += ["//images:timeout_binary", "//images:busybox_binary"]
records = {"status": "INCOMPLETE", "commands": [], "architectures": []}


def run(arguments):
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Image inspection exhausted the work deadline")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining)
    records["commands"].append({"arguments": arguments, "native_exit": result.returncode})
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode and result.stdout:
        print(result.stdout, end="")
    result.check_returncode()
    return result.stdout


try:
    records["source"] = run(["git", "rev-parse", "HEAD"]).strip()
    records["execution_root_output"] = run(["bazel", *startup, "info", *options, "execution_root"])
    execution_root = Path(records["execution_root_output"].strip())
    if not execution_root.is_absolute() or not execution_root.is_dir():
        raise ValueError("Invalid canonical execution root")
    for architecture, config, machine in (("amd64", "x86_64", "Advanced Micro Devices X86-64"),
                                          ("arm64", "aarch64", "AArch64")):
        row = {"architecture": architecture, "status": "INCOMPLETE"}
        records["architectures"].append(row)
        # Preserve transitioned local owners; source files use explicit repo labels.
        owners = "|".join(re.escape(label) for label in labels)
        sources = " ".join("@linux_pidfd//:" + name for name in installed_sources.values())
        query = run(["bazel", *startup, "cquery", *options, "--config=" + config,
                     "--output=starlark", "--starlark:expr=" +
                     "json.encode([str(target.label), target.label.name, [f.path for f in target.files.to_list()]])",
                     'filter("^(@@)?(' + owners + ')$", deps(//images/basic:pidfd_tests_archive))'
                     ' union set(' + sources + ') union //images/basic:pidfd_tests_context'])
        row["configured_inputs"] = [json.loads(line) for line in query.splitlines()]
        paths = configured_inputs(row["configured_inputs"], labels, installed_sources.values())
        outputs = {
            name: (output_base if path.startswith("external/") else execution_root) / path
            for name, path in paths.items()
        }
        archive_path = outputs["pidfd_tests_archive"]
        config_data, files, elf_files = archive(archive_path, row)
        inventory = destination.parent / (architecture + "-pidfd-inventory.json")
        inventory.write_text(json.dumps(row, indent=2) + "\n")
        context = outputs["pidfd_tests_context"]
        row["context"] = identity(context)
        with tarfile.open(context) as contents:
            matches = [member for member in contents if member.name.removeprefix("./") == "images/basic/pidfd-tests/image.tar"]
            if len(matches) != 1 or not matches[0].isfile():
                raise ValueError("Make context must contain one regular pidfd image archive")
            with contents.extractfile(matches[0]) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != row["archive"]["sha256"]:
                raise ValueError("Make context differs from the declared image archive")
        if (config_data["os"], config_data["architecture"]) != ("linux", architecture):
            raise ValueError("Image target architecture differs")
        cfg = config_data["config"]
        if cfg.get("WorkingDir") != "/opt/kselftest" or cfg.get("Cmd", [])[:2] != ["/bin/sh", "-ec"]:
            raise ValueError("Image does not launch the selftest shell in its installed directory")
        if files["opt/kselftest/kselftest-list.txt"]["sha256"] != identity(outputs["pidfd_test_list"])["sha256"]:
            raise ValueError("Installed selftest list differs from the generated input")
        for image_path, source_name in installed_sources.items():
            if files[image_path]["sha256"] != identity(outputs[source_name])["sha256"]:
                raise ValueError("Installed source or notice differs: " + image_path)
        notices = {
            "usr/share/licenses/busybox/NOTICE": identity(Path("images/busybox/NOTICE"))["sha256"],
            "usr/share/licenses/coreutils/NOTICE": identity(Path("images/coreutils/NOTICE"))["sha256"],
            "usr/share/licenses/musl/NOTICE": identity(Path("images/musl/NOTICE"))["sha256"],
            "usr/share/licenses/coreutils/COPYING": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
            "usr/share/licenses/busybox/LICENSE": "bbfc9843646d483c334664f651c208b9839626891d8f17604db2146962f43548",
        }
        row["utility_notices"] = {}
        for image_path, digest in notices.items():
            row["utility_notices"][image_path] = files[image_path]
            if files[image_path]["sha256"] != digest or files[image_path]["mode"] != 0o644:
                raise ValueError("Installed utility notice differs: " + image_path)
        expected_elf = {"opt/kselftest/pidfd/" + name for name in programs} | {"bin/busybox", "usr/bin/timeout"}
        if set(elf_files) != expected_elf:
            raise ValueError("Scratch image ELF inventory differs from the declared static programs")
        for image_path in ("opt/kselftest/run_kselftest.sh", "opt/kselftest/kselftest/runner.sh",
                           "opt/kselftest/kselftest/prefix.pl", "usr/bin/timeout"):
            if files[resolve(files, image_path)]["mode"] & 0o111 != 0o111:
                raise ValueError("Required runtime command is not executable: " + image_path)
        row["static_programs"] = []
        row["timeout_dependencies"] = []
        with tempfile.TemporaryDirectory(prefix="pidfd-elf-") as temporary:
            temporary = Path(temporary)
            for name in programs:
                image_path = "opt/kselftest/pidfd/" + name
                payload = elf_files[image_path]
                if (hashlib.sha256(payload).hexdigest() != identity(outputs[name])["sha256"]
                        or files[image_path]["mode"] != 0o755):
                    raise ValueError("Installed selftest differs from its compiled output: " + name)
                path = temporary / name
                path.write_bytes(payload)
                header = run(["readelf", "--file-header", str(path)])
                segments = run(["readelf", "--program-headers", str(path)])
                dynamic = run(["readelf", "--dynamic", str(path)])
                row["static_programs"].append({
                    "name": name, "installed_path": image_path, "output": identity(outputs[name]),
                    "file_header": header, "program_headers": segments, "dynamic": dynamic,
                })
                if machine not in header or "INTERP" in segments or "(NEEDED)" in dynamic:
                    raise ValueError("Selftest is not static for its target architecture: " + name)
            row["static_utilities"] = []
            for name, image_path, owner in (("timeout", "usr/bin/timeout", "timeout_binary"),
                                             ("busybox", "bin/busybox", "busybox_binary")):
                payload = elf_files[image_path]
                if (hashlib.sha256(payload).hexdigest() != identity(outputs[owner])["sha256"]
                        or files[image_path]["mode"] != 0o755):
                    raise ValueError("Installed utility differs from its compiled output: " + name)
                path = temporary / name
                path.write_bytes(payload)
                header = run(["readelf", "--file-header", str(path)])
                segments = run(["readelf", "--program-headers", str(path)])
                dynamic = run(["readelf", "--dynamic", str(path)])
                row["static_utilities"].append({
                    "name": name, "installed_path": image_path, "output": identity(outputs[owner]),
                    "file_header": header, "program_headers": segments, "dynamic": dynamic,
                })
                if machine not in header or "INTERP" in segments or "(NEEDED)" in dynamic:
                    raise ValueError("Utility is not static for its target architecture: " + name)
        row["runtime_inputs"] = {
            "status": "PASS", "source": records["source"], "archive": row["archive"],
            "config_sha256": row["config_sha256"], "inventory": identity(inventory),
        }
        row["status"] = "PASS"
    records["status"] = "PASS"
finally:
    destination.write_text(json.dumps(records, indent=2) + "\n")
