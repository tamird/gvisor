"""Verify sudo's package closure and declared installed configuration."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

from image_archive import archive, elf_closure, identity, resolve

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
destination = Path(sys.argv[1])
output_base = Path(os.environ["APPLICABILITY_OUTPUT_BASE"])
startup = ["--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"]]
options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
           "--downloader_config=" + os.environ["APPLICABILITY_DOWNLOADER_CONFIG"]]
records = {"status": "INCOMPLETE", "commands": [], "architectures": [], "metadata_errors": []}
labels = ["//images/basic:" + name for name in ("sudo_archive", "sudo_context", "sudoers_file")]
inputs = {
    "etc/passwd": ("sudo.passwd", 0o644, 0),
    "etc/group": ("sudo.group", 0o644, 0),
    "etc/shadow": ("sudo.shadow", 0o640, 42),
    "etc/sudoers.d/test-admins": ("sudo.sudoers", 0o440, 0),
}
for name in ("common-auth", "common-account", "common-password", "common-session"):
    inputs["etc/pam.d/" + name] = ("sudo." + name, 0o644, 0)
inputs["etc/pam.d/common-session-noninteractive"] = ("sudo.common-session", 0o644, 0)


def run(arguments):
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Sudo archive inspection exhausted the work deadline")
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
    execution_root = Path(run(["bazel", *startup, "info", *options, "execution_root"]).strip())
    if not execution_root.is_absolute() or not execution_root.is_dir():
        raise ValueError("Invalid canonical execution root")
    for architecture, config, machine, triplet in (
            ("amd64", "x86_64", "Advanced Micro Devices X86-64", "x86_64-linux-gnu"),
            ("arm64", "aarch64", "AArch64", "aarch64-linux-gnu")):
        row = {"architecture": architecture, "status": "INCOMPLETE"}
        records["architectures"].append(row)
        query = run(["bazel", *startup, "cquery", *options, "--config=" + config,
                     "--output=starlark", "--starlark:expr=" +
                     "json.encode([target.label.name, [f.path for f in target.files.to_list()]])",
                     "set(" + " ".join(labels) + ")"])
        outputs = {}
        for line in query.splitlines():
            match json.loads(line):
                case [str(name), [str(value)]]:
                    path = Path(value)
                    if name in outputs or path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                        raise ValueError(f"Unexpected configured output: {name}: {value}")
                    outputs[name] = execution_root / path
                case _:
                    raise ValueError("Expected a named single output: " + line)
        if set(outputs) != {label.split(":")[1] for label in labels}:
            raise ValueError("Configured sudo output set differs")
        config_data, files, elf_files = archive(outputs["sudo_archive"], row)
        inventory = destination.parent / (architecture + "-sudo-inventory.json")
        inventory.write_text(json.dumps(row, indent=2) + "\n")
        row["context"] = identity(outputs["sudo_context"])
        with tarfile.open(outputs["sudo_context"]) as contents:
            members = [item for item in contents if item.name.removeprefix("./") == "images/basic/sudo/image.tar"]
            match members:
                case [member] if member.isfile():
                    with contents.extractfile(member) as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                case _:
                    raise ValueError("Make context must contain one regular sudo archive")
        if digest != row["archive"]["sha256"]:
            raise ValueError("Make context differs from the declared sudo archive")
        cfg = config_data["config"]
        actual = (config_data["os"], config_data["architecture"], cfg.get("User"))
        if actual != ("linux", architecture, "alice") or cfg.get("Entrypoint") or cfg.get("Cmd"):
            raise ValueError(f"Unexpected sudo execution contract: {actual}, {cfg}")
        if not {"PATH=/usr/bin:/bin", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"}.issubset(cfg["Env"]):
            raise ValueError("Missing inherited image environment")
        if files["tmp"]["mode"] != 0o1777:
            raise ValueError("Temporary directory lost sticky permissions")
        row["configuration_inputs"] = {}
        for installed, (name, mode, gid) in inputs.items():
            source = identity(Path("images/basic") / name)
            item = files[installed]
            if (item["sha256"], item["mode"], item["uid"], item["gid"]) != (source["sha256"], mode, 0, gid):
                raise ValueError("Configuration bytes or metadata differ: " + installed)
            row["configuration_inputs"][installed] = source
        sudoers = identity(outputs["sudoers_file"])
        item = files["etc/sudoers"]
        if (item["sha256"], item["mode"], item["uid"], item["gid"]) != (sudoers["sha256"], 0o440, 0, 0):
            raise ValueError("Packaged sudoers bytes or installed permissions differ")
        if b"@includedir /etc/sudoers.d" not in outputs["sudoers_file"].read_bytes():
            raise ValueError("Packaged sudoers does not include the declared group policy")
        row["packaged_sudoers"] = sudoers
        for name, mode, uid, gid in (("home", 0o755, 0, 0), ("home/alice", 0o750, 1000, 1000),
                                     ("home/bob", 0o750, 1001, 1001)):
            item = files[name]
            if (item["type"], item["mode"], item["uid"], item["gid"]) != ("5", mode, uid, gid):
                raise ValueError("Home directory metadata differs: " + name)
        required = ["usr/bin/sudo", "usr/bin/strace", "usr/bin/setpriv", "usr/bin/unshare",
                    "sbin/setcap", "bin/sh", "bin/sleep", "bin/cp", "bin/chmod", "bin/cat", "bin/whoami"]
        row["required_executables"] = {}
        for name in required:
            resolved = resolve(files, name)
            if resolved not in elf_files or files[resolved]["mode"] & 0o111 != 0o111:
                raise ValueError("Required executable is absent: " + name)
            row["required_executables"][name] = {"resolved": resolved, **files[resolved]}
        if resolve(files, "bin/sh") != "bin/dash" or files["usr/bin/sudo"]["mode"] != 0o4755:
            raise ValueError("Shell resolution or sudo setuid permissions differ")
        if resolve(files, "bin/whoami") != "usr/bin/whoami":
            raise ValueError("The consumer's absolute whoami path does not resolve to its package")
        row["pam_modules"] = []
        for name in ("pam_unix", "pam_deny", "pam_permit", "pam_umask", "pam_limits", "pam_env"):
            installed = "lib/" + triplet + "/security/" + name + ".so"
            if resolve(files, installed) not in elf_files:
                raise ValueError("Missing declared PAM module: " + installed)
            row["pam_modules"].append(installed)
        for name in ("sudoers.so", "libsudo_util.so.0"):
            if resolve(files, "usr/libexec/sudo/" + name) not in elf_files:
                raise ValueError("Missing sudo plugin: " + name)
        for name in ("etc/pam.d/sudo", "etc/pam.d/sudo-i", "etc/sudo.conf"):
            if not files[resolve(files, name)]["bytes"]:
                raise ValueError("Missing packaged service configuration: " + name)
        row["elf_closure"] = elf_closure(files, elf_files, machine,
                                         ["/lib/" + triplet, "/usr/lib/" + triplet, "/lib", "/usr/lib"], run,
                                         destination.parent / (architecture + "-sudo-readelf.txt"))
        row["nss_files"] = {name: item for name, item in files.items()
                            if name == "etc/nsswitch.conf" or "libnss_" in Path(name).name}
        row["runtime_inputs"] = {"status": "PASS", "source": records["source"],
                                  "archive": row["archive"], "config_sha256": row["config_sha256"]}
        notices = ["usr/share/doc/" + name + "/copyright" for name in (
            "sudo", "libpam-runtime", "libpam-modules", "libpam0g", "dash", "coreutils",
            "libcap2-bin", "libcap2", "strace", "util-linux", "libc6", "base-files")]
        notices += ["usr/share/common-licenses/" + name for name in ("GPL-2", "GPL-3", "LGPL-2.1")]
        row["notices"] = {}
        for name in notices:
            try:
                resolved = resolve(files, name)
                if not files[resolved]["bytes"] or files[resolved]["mode"] & 0o444 != 0o444:
                    raise ValueError("Missing readable package notice")
                row["notices"][name] = {"resolved": resolved, **files[resolved]}
            except (ValueError, RuntimeError) as error:
                records["metadata_errors"].append({"architecture": architecture, "path": name, "error": str(error)})
        row["status"] = "FAIL" if any(error["architecture"] == architecture for error in records["metadata_errors"]) else "PASS"
    records["status"] = "FAIL" if records["metadata_errors"] else "PASS"
    if records["metadata_errors"]:
        raise ValueError("Package notice validation failed; verified runtime inputs remain usable")
finally:
    destination.write_text(json.dumps(records, indent=2) + "\n")
