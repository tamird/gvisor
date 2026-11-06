"""Build only the coverage flavors, then run the existing KCOV syscall test on each."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlparse

from with_cfg_elf import inspect_elf


W = Path(os.environ["RUNNER_TEMP"]) / "with-cfg-native-kcov"
CPU = os.environ["CPU"]
ARCH, MACHINE = {"amd64": ("x86_64", 62), "arm64": ("aarch64", 183)}[CPU]
SOURCE = "c2a9426436643ee79e60d34a4e3b6bf4a88decce"
CASE = "KcovTest.Kcov"
KCOV = "//test/syscalls/linux:kcov_test"
PREWARM = "//runsc/prewarmer:prewarmer_binary"
UMOUNT = "//runsc/procumounter:procumounter_binary"
SUPPORT = {KCOV:"kcov_test", PREWARM:"gvisor-sentry-prewarmer", UMOUNT:"gvisor-proc-umounter"}
FLAVORS = {
    "coverage": {"//runsc:runsc_coverage":"runsc", "//runsc/cmd/sentry:gvisor_sentry_coverage":"gvisor_sentry"},
    "race-coverage": {"//runsc:runsc_race_coverage":"runsc", "//runsc/cmd/sentry:gvisor_sentry_race_coverage":"gvisor_sentry"},
}
BAZEL = [str(W / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3000m"]
OPTIONS = ["--config=" + ARCH, "-c", "opt", "--strip=never", "--jobs=3", "--local_resources=memory=9900",
    "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
    "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
    "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
    "--color=no", "--curses=no", "--noshow_progress"]
r = {"source":SOURCE, "with_cfg":"1.0.2", "cpu":CPU, "machine":platform.machine(),
     "kernel":platform.release(), "builds":{}, "outputs":{}, "runs":{}, "verified":False}


def save():
    (W / "result.json").write_text(json.dumps(r, indent=2) + "\n")


def signals(path):
    if not path.is_file():
        return []
    patterns = ("Resource Exhausted", "No space left on device", "OutOfMemoryError", "Signal: killed",
                "Killed", "not a valid compiler", "not found", "no such file", "permission denied",
                "operation not permitted", "not supported", "undefined reference", "text file busy",
                "ThreadSanitizer", "DATA RACE", "could not be resolved", "unexpected memory mapping")
    found = set()
    with path.open(errors="replace") as stream:
        for line in stream:
            found.update(p for p in patterns if p.lower() in line.lower())
    return sorted(found)


def run(args, log, seconds):
    with log.open("w") as out:
        return subprocess.run(["timeout", "--signal=INT", "--kill-after=45s", str(seconds), *args],
                              stdout=out, stderr=subprocess.STDOUT).returncode


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for buf in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(buf)
    return h.hexdigest()


def build(phase, requested, seconds):
    bep = W / (phase + ".bep")
    log = W / (phase + ".build.log")
    row = {"requested":list(requested), "begin_disk_free":shutil.disk_usage(W).free}
    r["builds"][phase] = row
    save()
    row["exit"] = run([*BAZEL, "build", *OPTIONS, "--keep_going",
            "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false",
            *requested], log, seconds)
    row["end_disk_free"] = shutil.disk_usage(W).free
    row["signals"] = signals(log)
    row["first_errors"] = []
    with log.open(errors="replace") as lines:
        for line in lines:
            if re.search(r"^(?:ERROR:|[^ ]+\.go:\d+:)|undefined reference|terminated by signal|command not found", line):
                row["first_errors"].append(line.strip()[:380])
                if len(row["first_errors"]) == 10:
                    break
    save()
    refs = {}
    file_sets = {}
    completed = {}
    if bep.is_file():
        with bep.open() as lines:
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                ids = event.get("id", {})
                if "namedSet" in ids:
                    file_sets[ids["namedSet"]["id"]] = event.get("namedSetOfFiles", {})
                entity = ids.get("targetCompleted", {})
                label = entity.get("label")
                if label in requested and not entity.get("aspect") and "completed" in event:
                    body = event["completed"]
                    completed[label] = body.get("success", False)
                    refs[label] = [n["id"] for g in body.get("outputGroup", []) if g.get("name") == "default"
                                   for n in g.get("fileSets", [])]
    row["completed"] = completed
    save()
    for label, name in requested.items():
        dest = W / ("support" if label in SUPPORT else phase) / name
        dest.parent.mkdir(exist_ok=True)
        target = r["outputs"][label] = {"completed":completed.get(label, False)}
        pending = list(refs.get(label, []))
        visited = set()
        defaults = {}
        while pending:
            n = pending.pop()
            if n in visited:
                continue
            visited.add(n)
            bunch = file_sets.get(n, {})
            pending.extend(s["id"] for s in bunch.get("fileSets", []))
            for output in bunch.get("files", []):
                uri = urlparse(output.get("uri", ""))
                if uri.scheme == "file" and uri.netloc in ("", "localhost"):
                    p = Path(unquote(uri.path))
                    if p.is_file():
                        defaults[p] = output.get("digest")
        target["default_files"] = sorted(p.name for p in defaults)
        if target["completed"] and len(defaults) == 1:
            src, = defaults
            shutil.copyfile(src, dest)
            dest.chmod(0o755)
            if label in SUPPORT:
                with dest.open("rb") as stream:
                    hdr = stream.read(20)
                target.update(elf64_le=hdr[:7] == b"\x7fELF\x02\x01\x01",
                              machine=int.from_bytes(hdr[18:20],"little"), sha256=sha(dest), size=dest.stat().st_size)
            else:
                target.update(inspect_elf(dest))
            target["bound_default_digest"] = defaults[src]
            target["matches_bep"] = not defaults[src] or defaults[src] == target.get("sha256")
        save()
    return row


def check_case(log):
    results = []
    if log.is_file():
        with log.open(errors="replace") as stream:
            for line in stream:
                match = re.match(r"^\[\s*(OK|FAILED|SKIPPED)\s*\]\s+" + re.escape(CASE) + r"\s+\(", line)
                if match:
                    results.append(match[1])
    return results


def native_case():
    binary = W / "support/kcov_test"
    if not binary.is_file():
        return
    log = W / "native.log"
    # Hosted kernels need both KCOV configuration and a mounted debugfs.
    # Mount it only inside this native control's private mount namespace.
    setup = 'if ! mountpoint -q /sys/kernel/debug; then mount -t debugfs debugfs /sys/kernel/debug || :; fi; exec "$@"'
    code = run(["sudo", "-n", "/usr/bin/unshare", "--mount", "--propagation", "private", "/bin/sh",
                "-c", setup, "kcov-native", str(binary), "--gtest_filter=" + CASE, "--gtest_color=no"], log, 120)
    r["runs"]["native_kernel"] = {"exit":code, "gtest":check_case(log), "signals":signals(log)}
    save()


def guest(phase):
    files = {key:r["outputs"].get(key, {}) for key in (*SUPPORT, *FLAVORS[phase])}
    if not all(f.get("machine") == MACHINE and f.get("matches_bep") for f in files.values()):
        return
    place = W / phase
    sidecars = place / "gvisor-bin"
    sidecars.mkdir(exist_ok=True)
    # This is the same layout as tools/release.bzl. Only the instrumented
    # Sentry is installed in this flavor's sidecar directory.
    for src, dest in ((place / "gvisor_sentry", "gvisor_sentry"),
            (W / "support" / SUPPORT[PREWARM], SUPPORT[PREWARM]),
            (W / "support" / SUPPORT[UMOUNT], SUPPORT[UMOUNT])):
        os.link(src, sidecars / dest)
    cli = place / "runsc"
    version = place / "version.log"
    result = r["runs"][phase] = {"cli_version_exit":run([str(cli), "--version"], version, 40)}
    result["printed_version"] = "runsc version " in version.read_text(errors="replace")
    result["version_signals"] = signals(version)
    save()
    boot_log = place / "runtime.log"
    boot_log.touch(mode=0o644)
    proc_log = place / "guest.log"
    state = place / "state"
    state.mkdir()
    sentry = sidecars / "gvisor_sentry"
    result["installed_sentry_sha256"] = sha(sentry)
    result["guest_exit"] = run(["sudo", "-n", "/usr/bin/env", "GVISOR_SIDECAR_BINARIES_DIR=" + str(sidecars),
           str(cli), "--root=" + str(state), "--platform=systrap", "--network=none", "--ignore-cgroups=true",
           "--debug", "--alsologtostderr=true", "--log=" + str(boot_log), "do", str(W / "support/kcov_test"),
           "--gtest_filter=" + CASE, "--gtest_color=no"], proc_log, 240)
    result["gtest"] = check_case(proc_log)
    msg = 'Sidecar "gvisor_sentry" found: booting sandbox with ' + str(sentry)
    result["selected_installed_sentry"] = subprocess.run(
        ["sudo","-n","/bin/grep","-Fq",msg,str(boot_log),str(proc_log)]).returncode == 0
    result["runtime_signals"] = sorted(set(signals(proc_log) + signals(boot_log)))
    with proc_log.open(errors="replace") as lines:
        result["diagnostics"] = [l.strip()[:390] for l in lines if re.search(
            r"FATAL|WARNING: DATA RACE|not usable:|creating (?:sandbox|container)|Failure$|unexpected memory mapping|Error accessing",l)][:6]
    save()


def main():
    save()
    assert r["machine"] == ARCH
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == SOURCE
    assert subprocess.check_output(["git","status","--porcelain","--untracked-files=no"],text=True) == ""
    modules = Path("MODULE.bazel").read_text()
    assert 'name = "with_cfg.bzl", version = "1.0.2"' in modules
    # Give native compilation room for the large instrumented ABI package:
    # finish one coverage configuration before starting the race variant.
    build("coverage", {**FLAVORS["coverage"], **SUPPORT}, 105 * 60)
    native_case()
    guest("coverage")
    build("race-coverage", FLAVORS["race-coverage"], 95 * 60)
    guest("race-coverage")
    results = []
    for phase, targets in FLAVORS.items():
        view = r["runs"].get(phase, {})
        results.append(r["builds"][phase]["exit"] == 0 and view.get("printed_version")
            and view.get("cli_version_exit") == 0 and view.get("guest_exit") == 0
            and view.get("gtest") == ["OK"] and view.get("selected_installed_sentry")
            and all(r["outputs"].get(t, {}).get("go_cover_symbols", 0) > 0
                    and r["outputs"][t].get("nm_exit") == 0 and r["outputs"][t].get("exec_load")
                    and (r["outputs"][t].get("race_runtime_symbols", 0) > 0) == (phase == "race-coverage")
                    for t in targets))
    r["verified"] = all(results)
    save()


if __name__ == "__main__":
    main()
