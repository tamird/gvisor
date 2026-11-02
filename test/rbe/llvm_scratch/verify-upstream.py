"""Exercise LLVM's own repository rule with local and http_archive inputs."""

import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import time


SOURCE = Path(os.environ["GITHUB_WORKSPACE"]) / "llvm-project"
ROOT = SOURCE / "utils/bazel"
TEMP = Path(os.environ["RUNNER_TEMP"])
PROBE = ROOT / "upstream_instrumentor_probe"
LABEL = "@llvm-project//llvm:instrumentor_variables_gen"
EMBEDDED = "//upstream_instrumentor_probe:embedded"
IPO = Path("llvm/include/llvm/Transforms/IPO")
HELPER = SOURCE / IPO / "InstrumentorRuntimeHelper.h"
TEMPLATE = SOURCE / IPO / "InstrumentorVariables.inc.in"
TOKEN = b"@LLVM_INSTRUMENTOR_RUNTIME_HELPER@"
MARKER = b'\n// Local source edit: literal \\n, "quote", and @unused@.\n'
REPORT = {"product": os.environ["PRODUCT"], "base": os.environ["BASE"], "status": "INCOMPLETE", "phases": {}}


def save():
    path = TEMP / "llvm-evidence/verdict.json"
    path.write_text(json.dumps(REPORT, indent=2) + "\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def output(*args, cwd=ROOT):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        # No credential or remote cache is supplied to this job.
        lines = proc.stderr.decode(errors="replace").splitlines()
        issues = [i for i, line in enumerate(lines) if "ERROR" in line or "error:" in line]
        # Preserve the Starlark traceback after the first repository error,
        # along with the underlying filesystem diagnostic, if available.
        evidence = lines[issues[0]:issues[0] + 14] + lines[-5:] if issues else lines[-7:]
        raise RuntimeError(f"{Path(args[0]).name} {args[1]} exited {proc.returncode}: " + "\n".join(evidence)[:3500])
    return proc.stdout


def bazel(command, *args):
    startup = ["--windows_enable_symlinks"] if os.name == "nt" else []
    return output("bazel", *startup, command, "--lockfile_mode=off", *args)


def selected(label, suffix):
    files = bazel("cquery", "--output=files", label).decode().splitlines()
    file, = [name for name in files if name.replace("\\", "/").endswith(suffix)]
    return Path(bazel("info", "execution_root").decode().strip()) / file


def check_phase(name, helper):
    row = REPORT["phases"][name] = {"status": "INCOMPLETE", "start": time.time()}
    save()
    try:
        bazel("build", LABEL, EMBEDDED)
        actions = json.loads(bazel("aquery", "--output=jsonproto", LABEL))
        action, = actions["actions"]
        target, = [t for t in actions["targets"] if t["id"] == action["targetId"]]
        assert target["label"].endswith("//llvm:instrumentor_variables_gen"), target["label"]
        row["action"] = [action["mnemonic"], target["label"], action.get("actionKey")]
        assert action["mnemonic"] == "TemplateExpand" and not action.get("arguments"), "expected a native template action"
        output_label = ":include/llvm/Transforms/IPO/InstrumentorVariables.inc"
        consumers = bazel("query", "labels(textual_hdrs, @llvm-project//llvm:IPO)").decode().splitlines()
        assert any(label.endswith("//llvm" + output_label) for label in consumers), consumers[:3]
        expected = TEMPLATE.read_bytes().replace(TOKEN, helper)
        generated = selected(LABEL, "/InstrumentorVariables.inc").read_bytes()
        archive = selected(EMBEDDED, "/libembedded.a")
        row.update(helper_bytes=len(helper), helper=sha(helper), template=sha(TEMPLATE.read_bytes()), generated=sha(generated), generated_size=len(generated))
        assert generated == expected, "generated header differs from the shared template plus the exact source bytes"
        content = archive.read_bytes()
        assert helper + b"\0" in content, "compiled archive does not contain the exact null-terminated runtime source"
        symbols = output("nm", "-g", archive)
        assert b" llvm_instrumentor_source" in symbols
        row.update(archive=sha(content), embedded_bytes=len(helper) + 1, status="PASS")
    except Exception as exc:
        row["error"] = str(exc)[:2500]
    finally:
        row["finish"] = time.time()
        save()


def windows():
    REPORT.update(stage="Windows source identity", host=platform.platform(), machine=platform.machine(), arch=os.environ["RUNNER_ARCH"], target=LABEL)
    save()
    assert os.environ["RUNNER_OS"] == "Windows" and platform.system() == "Windows"
    assert output("git", "rev-parse", "HEAD", cwd=SOURCE).decode().strip() == REPORT["product"]
    assert output("git", "rev-parse", "HEAD^", cwd=SOURCE).decode().strip() == REPORT["base"]
    version = (ROOT / ".bazelversion").read_text().strip()
    assert version == "9.2.0" and output("bazel", "--version").decode().strip() == "bazel " + version
    REPORT["bazel"] = version
    helper, template = HELPER.read_bytes(), TEMPLATE.read_bytes()
    for path, contents in ((HELPER, helper), (TEMPLATE, template)):
        assert contents == output("git", "show", "HEAD:" + path.relative_to(SOURCE).as_posix(), cwd=SOURCE)
    assert template.count(TOKEN) == 1
    expected = template.replace(TOKEN, helper)
    REPORT.update(stage="canonical Windows build", template=sha(template), helper=sha(helper), helper_bytes=len(helper), expected=sha(expected), started_at=time.time())
    save()
    try:
        bazel("build", LABEL)
    except Exception as original:
        REPORT.update(build_error=str(original), stage="unmodified parent control")
        save()
        output("git", "switch", "--detach", REPORT["base"], cwd=SOURCE)
        try:
            bazel("build", LABEL)
            REPORT["parent_build"] = "PASS"
        except Exception as control:
            REPORT["parent_build_error"] = str(control)
        finally:
            output("git", "switch", "--detach", REPORT["product"], cwd=SOURCE)
            save()
        return False
    REPORT.update(stage="compare generated output", build_finished_at=time.time())
    save()
    generated = selected(LABEL, "/InstrumentorVariables.inc").read_bytes()
    actions = json.loads(bazel("aquery", "--output=jsonproto", LABEL))
    action, = actions["actions"]
    target, = [t["label"] for t in actions["targets"] if t["id"] == action["targetId"]]
    REPORT.update(generated=sha(generated), generated_bytes=len(generated), action=action["mnemonic"], owner=target)
    assert action["mnemonic"] == "TemplateExpand" and target.endswith("//llvm:instrumentor_variables_gen") and not action.get("arguments")
    assert generated == expected, "generated bytes differ from the shared template with the original helper"
    REPORT.update(status="VERIFIED", stage="complete")
    return True


def run():
    assert output("git", "rev-parse", "HEAD", cwd=SOURCE).decode().strip() == REPORT["product"]
    assert output("git", "rev-parse", "HEAD^", cwd=SOURCE).decode().strip() == REPORT["base"]
    assert not output("git", "status", "--porcelain", cwd=SOURCE)
    assert TEMPLATE.read_bytes().count(TOKEN) == 1
    version = (ROOT / ".bazelversion").read_text().strip()
    assert version == "9.2.0" and output("bazel", "--version").decode().strip() == "bazel " + version
    REPORT["bazel"] = version
    source_archive = TEMP / "llvm-project.tar.gz"
    output("git", "archive", "--format=tar.gz", "-1", "--prefix=llvm-project/", "-o", source_archive, REPORT["product"], cwd=SOURCE)
    with source_archive.open("rb") as archive:
        digest = hashlib.file_digest(archive, "sha256").hexdigest()
    REPORT["source_archive"] = digest
    original = HELPER.read_bytes()
    module = ROOT / "MODULE.bazel"
    manifest = module.read_bytes()
    assert MARKER not in original
    PROBE.mkdir()
    try:
        # Reuse the already qualified C++ consumer with no changes.
        for name in ("BUILD", "probe.bzl", "embedded.cc", "instrumentor-template.patch"):
            shutil.copyfile(Path(__file__).parent / name, PROBE / name)
        check_phase("local", original)
        HELPER.write_bytes(original + MARKER)
        check_phase("local_edit", original + MARKER)
        # Keep the modified local header in place: resolving the mutable layout
        # again instead of the immutable archive must fail the original-byte gate.
        with module.open("a") as f:
            f.write(f"""
raw_archive = use_repo_rule("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
raw_archive(
    name = "instrumentor_source_archive",
    urls = ["{source_archive.as_uri()}"],
    sha256 = "{digest}",
    strip_prefix = "llvm-project",
    build_file_content = "# empty",
)
override_repo(llvm_repos_extension, **{{"llvm-raw": "instrumentor_source_archive"}})
""")
        check_phase("raw_archive", original)
    finally:
        HELPER.write_bytes(original)
        module.write_bytes(manifest)
        shutil.rmtree(PROBE)
    assert not output("git", "status", "--porcelain", "--untracked-files=no", cwd=SOURCE)
    rows = REPORT["phases"]
    assert set(rows) == {"local", "local_edit", "raw_archive"}
    if all(v["status"] == "PASS" for v in rows.values()):
        assert rows["local"]["generated"] == rows["raw_archive"]["generated"] != rows["local_edit"]["generated"]
        REPORT["status"] = "VERIFIED"
    return REPORT["status"] == "VERIFIED"


if __name__ == "__main__":
    save()
    try:
        ok = windows()
    except Exception as exc:
        REPORT["error"] = str(exc)[:2500]
        ok = False
    REPORT["verdict_at"] = time.time()
    save()
    sys.exit(not ok)
