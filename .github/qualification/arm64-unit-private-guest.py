"""Run two original ARM64 unit payloads under the existing isolated guest rule."""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "guest-report"
PRODUCT = os.environ["PRODUCT"]
GUEST = os.environ["GUEST"]
OWNERS = {
    "runsc/procumounter": ("procumounter_test", "gvisor-proc-umounter",
                           {"TestUmounterFitsInOnePage", "TestUmounterIsStatic", "TestUmounter",
                            "TestUmounter/sync=true", "TestUmounter/sync=false"}),
    "runsc/specutils/safemount_test": ("safemount_test", "safemount_runner", {"TestSafeMount"}),
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    (OUT / "receipt.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def diagnostic(path):
    if not path.exists():
        return []
    text = path.read_text(errors="replace")
    patterns = {"permission_denied": r"permission denied|operation not permitted",
                "exec_format": r"exec format error", "remote_unavailable": r"UNAVAILABLE|deadline exceeded",
                "repository_fetch": r"error downloading|error fetching|checksum was",
                "analysis": r"analysis of target .* failed", "missing_input": r"no such (file|target|package)",
                "no_platform": r"no (matching|available) (execution )?platform|no matching toolchain",
                "guest_incomplete": r"guest did not report a completed test|passing guest omitted test XML"}
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)]


def bazel(phase, command, flags, targets):
    events = TMP / (phase + ".bep")
    cmd = ["bazel", "--bazelrc=" + str(TMP / (phase + ".bazelrc")), command, *flags,
           "--jobs=20", "--remote_download_outputs=toplevel", "--build_event_json_file=" + str(events), *targets]
    log = TMP / (phase + ".log")
    with log.open("w") as fp:
        status = subprocess.run(cmd, stdout=fp, stderr=subprocess.STDOUT).returncode
    rows = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    started = [row["started"]["uuid"] for row in rows if "started" in row]
    report[phase] = dict(command=[command, *flags, *targets], exit=status, finished=now(),
                         invocations=started, diagnostics=diagnostic(log))
    save()
    return status, rows


def add_rule(directory, load, rule):
    path = Path(directory) / "BUILD"
    path.write_text(load + "\n" + path.read_text() + "\n" + rule + "\n")


def product():
    require(git("rev-parse", "HEAD") == PRODUCT and not git("diff", "--name-only", "HEAD"),
            "unclean product or unexpected source")
    targets = []
    for directory, (name, _, _) in OWNERS.items():
        add_rule(directory, 'load("@rules_pkg//pkg:pkg.bzl", _test_tar = "pkg_tar")', f"""
_test_tar(
    name = "private_guest_capsule",
    testonly = True,
    srcs = [":{name}"],
    include_runfiles = True,
    strip_prefix = "/",
    allow_duplicates_with_different_content = False,
)
""")
        targets.append(f"//{directory}:private_guest_capsule")
    status, _ = bazel("product", "build", ["--strip=never"], targets)
    require(status == 0, "product build failed")
    require(set(git("diff", "--name-only", "HEAD").splitlines()) == {d + "/BUILD" for d in OWNERS},
            "product source changed beyond capsule declarations")
    report["payloads"] = {}
    for directory, (name, sidecar, _) in OWNERS.items():
        archive = Path("bazel-bin") / directory / "private_guest_capsule.tar"
        target = TMP / (name + ".tar")
        shutil.copyfile(archive, target)
        with tarfile.open(target) as tar:
            parts = {}
            for basename in (name, sidecar):
                found = {m.name: sha(content)
                         for m in tar if Path(m.name).name == basename and (m.isfile() or m.islnk())
                         if (content := tar.extractfile(m).read())
                         and content[:6] == b"\x7fELF\x02\x01" and int.from_bytes(content[18:20], "little") == 183}
                require(found and len(set(found.values())) == 1, "missing or conflicting ARM64 ELF: " + basename)
                parts[basename] = found
        report["payloads"][name] = dict(owner=f"//{directory}:{name}", sha256=sha(target.read_bytes()),
                                        elfs=parts)
    save()
    git("restore", "--", *(d + "/BUILD" for d in OWNERS))


def guest():
    git("checkout", "--quiet", "--detach", GUEST)
    require(git("rev-parse", "HEAD") == GUEST and not git("diff", "--name-only", "HEAD"),
            "unclean guest source")
    # Match the Go test XML settings in the native unit run. The donor exports
    # selected test-harness variables into the private guest at this point.
    launch = Path("test/rbe/tcg/run.sh")
    old = "  # Keep the canonical harness's outputs on ext4 until the payload has exited."
    source = launch.read_text()
    require(source.count(old) == 1, "guest environment anchor changed")
    launch.write_text(source.replace(old, "  printf 'export GO_TEST_RUN_FROM_BAZEL=1 GO_TEST_WRAP_TESTV=true\\n'\n" + old))
    targets = []
    for directory, (name, _, _) in OWNERS.items():
        capsule = Path(directory) / "private_guest_capsule.tar"
        shutil.copyfile(TMP / (name + ".tar"), capsule)
        require(sha(capsule.read_bytes()) == report["payloads"][name]["sha256"], "capsule substitution")
        add_rule(directory, 'load("//test/rbe/tcg:defs.bzl", "arm64_tcg_test")', f"""
arm64_tcg_test(
    name = "private_guest_test",
    payload = ":{name}",
    archive = ":private_guest_capsule.tar",
    image = "//test/rbe/tcg:rc_guest",
    host_architecture = "arm64",
    tags = [],
    size = "large",
)
""")
        targets.append(f"//{directory}:private_guest_test")
    status, events = bazel("guest", "test", ["--config=rbe-arm64", "--config=aarch64", "--keep_going",
                           "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
                           "--strategy=TestRunner=remote", "--noremote_local_fallback",
                           "--zip_undeclared_test_outputs", "--test_output=errors"], targets)
    for directory, (name, _, expected) in OWNERS.items():
        label = f"//{directory}:private_guest_test"
        records = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == label
                   and "testResult" in e]
        row = report["payloads"][name]
        row["results"] = [dict(status=r.get("status"), cachedLocally=r.get("cachedLocally", False),
                               cachedRemotely=r.get("executionInfo", {}).get("cachedRemotely", False),
                               strategy=r.get("executionInfo", {}).get("strategy")) for r in records]
        outputs = Path("bazel-testlogs") / directory / "private_guest_test/test.outputs/outputs.zip"
        if outputs.exists():
            with zipfile.ZipFile(outputs) as z:
                files = set(z.namelist())
                for path in ("host-architecture.txt", "qemu-version.txt", "guest/kernel.txt",
                             "guest/page-size.txt", "guest/exit_status"):
                    if path in files:
                        row[path] = z.read(path).decode().strip()[:500]
                if "guest/test.xml" in files:
                    xml = z.read("guest/test.xml")
                    require(len(xml) < 16000, "unexpected guest XML size")
                    (OUT / (name + ".xml")).write_bytes(xml)
                    row["xml_sha256"] = sha(xml)
                    row["cases"] = [[case.get("name"), bool(case.find("failure") is not None or
                                    case.find("error") is not None or case.find("skipped") is not None
                                    or case.get("status") in {"notrun", "skipped"})]
                                    for case in ET.fromstring(xml).iter("testcase")]
        results = row["results"]
        row["verified"] = (len(results) == 1 and results[0]["status"] == "PASSED" and
            not results[0]["cachedLocally"] and not results[0]["cachedRemotely"] and
            results[0]["strategy"] in (None, "remote") and row.get("host-architecture.txt") == "aarch64" and
            "QEMU emulator version" in row.get("qemu-version.txt", "") and
            "7.3.0-070300rc3-generic" in row.get("guest/kernel.txt", "") and "aarch64" in row.get("guest/kernel.txt", "") and
            row.get("guest/page-size.txt") == "Guest page size: 4096" and row.get("guest/exit_status") == "0" and
            sorted(row.get("cases", [])) == sorted([x, False] for x in expected))
        save()
    require(status == 0 and all(v["verified"] for v in report["payloads"].values()), "guest gates incomplete")


OUT.mkdir(exist_ok=True)
report = dict(status="INCOMPLETE", carrier=os.environ["GITHUB_SHA"], product_source=PRODUCT,
              guest_source=GUEST, prior_host_run="41589548481", started=now())
save()
try:
    product()
    guest()
    report["status"] = "PASS"
except Exception as exc:
    report["reason"] = str(exc)[:500]
    raise
finally:
    report["verified_at"] = now()
    save()
