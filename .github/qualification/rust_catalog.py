"""Measure the unmodified gVisor license checker against the Rust example's BCR pin."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SOURCES = {"master": "ac878dc8a5a4862555b532a055800eea832e524e",
           "protobuf37": "7d90dd78694554f17d6a8c57337c32646d7c5c5e"}
ARM, PHASE = sys.argv[1:]
SOURCE = SOURCES[ARM]
ARCHIVE = "a345fe3fa11fbccec1de3e19c9597fef7e0cebb9af9ef3847d74abf26f222c8b"
URL = "https://github.com/hermeticbuild/rules_rs/releases/download/v0.1.3/rules_rs-v0.1.3.tar.gz"
COMMIT = "7065a72736844e3f7358431d8b2299ec35674d61"
TARGET = "//tools/licensecheck/main:licensecheck"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "rules-rs-stock-license"
WORK = ROOT / ARM
CATALOG = Path("tools/licensecheck/dependencies.yaml")
ROW = re.compile(r"(?ms)^- dependency: ([^\n]+)\n.*?(?=^- dependency:|\Z)")
TRIAL = WORK / "trial.yaml"
RECEIPT = WORK / "receipt.json"


def rows(text):
    return {match[1]: match[0] for match in ROW.finditer(text)}


def known():
    return set(rows(CATALOG.read_text()))


def run(name, argv, report):
    # Forward the ephemeral GitHub token only to the stock Fetch invocation.
    env = {k: os.environ[k] for k in ("HOME", "USER", "PATH", "LANG") if k in os.environ}
    env.update(PATH=str(ROOT) + os.pathsep + env["PATH"], LANG="C.UTF-8",
               MAKEFLAGS="DOCKER_BUILD=false", DOCKER_HOST="unix:///nonexistent-rust-license.sock")
    if name == "fetch" and "GITHUB_TOKEN" in os.environ:
        env["GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN"]
    result = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = result.stdout.decode(errors="replace")
    record = {"exit": result.returncode, "log_sha256": hashlib.sha256(result.stdout).hexdigest(),
              "bytes": len(result.stdout)}
    if count := re.search(r"all (\d{1,5}) dependencies have up-to-date license entries", text):
        record["verified_entries"] = int(count[1])
    if name == "negative":
        record["missing_rust"] = "missing entry for rules_rs" in text
    problem_names = set()
    for dep in known():
        if re.search(r"(?m)^(?:missing entry for |stale entry for )" + re.escape(dep) + r"$|^" + re.escape(dep) + r"(?:: .*does not match| was audited| has unknown license|\s+FAILED:)", text):
            problem_names.add(dep)
    record["diagnostic_owners"] = sorted(problem_names)[:35]
    signals = ["HTTP 403", "API rate limit", "no license file", "kept existing entry", "invalid module",
               "could not load", "repository rule", "no such package", "no such target", "No matching toolchains",
               "checksum", "timeout", "timed out", "Connection reset", "502 Bad Gateway", "404 Not Found",
               "failed to generate", "cannot fetch licenses", "Out of memory", "not visible",
               "No such file or directory", "Unable to find package", "shared library", "strict deps"]
    signals += ["lockfile", "lock file", "repin", "apply reprojection", "Cargo.Bazel.lock", "Cargo.lock", "protobuf+",
                "crates_repository", "cargo_bazel", "crate_universe", "generated lockfiles", "root module", "extension evaluation"]
    record["signals"] = [s for s in signals if s.lower() in text.lower()]
    # Associate the diagnostic with the source/extension without including command lines.
    record["protobuf_lock_lines"] = [
        [signal for signal in ("protobuf", "lockfile", "lock file", "cargo.bazel.lock", "cargo.lock", "repin", "crate_universe", "crates_repository")
         if signal in line.lower()]
        for line in text.splitlines() if "protobuf" in line.lower() and any(t in line.lower() for t in ("lock", "repin", "crate_universe", "crates_repository"))
    ][:12]
    report["commands"][name] = record
    RECEIPT.write_text(json.dumps(report, indent=2) + "\n")
    return text


def main():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == SOURCE
    if PHASE == "prepare":
        WORK.mkdir(exist_ok=True)
        initial = CATALOG.read_text()
        rules = rows(initial)["rules_rs"]
        assert URL in rules and COMMIT in rules and ARCHIVE in rules and "  license: Apache-2.0\n" in rules
        TRIAL.write_text(initial.replace(rules, ""))
        assert "rules_rs" not in rows(TRIAL.read_text())
        report = {"source": SOURCE, "expected_entries": len(rows(initial)), "commands": {},
                  "preexisting_rust_row_in_fetch_input": False, "catalog_sha256": hashlib.sha256(initial.encode()).hexdigest()}
        run("stock_verify", ["make", "-s", "DOCKER_BUILD=false", "license-check"], report)
        info = run("registry_repo", ["make", "-s", "DOCKER_BUILD=false", "mod", "OPTIONS=", "TARGETS=show_repo @rules_rs"], report)
        sri = "sha256-" + base64.b64encode(bytes.fromhex(ARCHIVE)).decode()
        report["registry_pin_and_url"] = (ARCHIVE in info or sri in info) and URL in info
        run("negative", ["bazel", "run", "-c", "opt", TARGET, "--", "--mode=verify", "--yaml=" + str(TRIAL)], report)
        lock = Path("MODULE.bazel.lock")
        if lock.is_file():
            contents = json.loads(lock.read_text())
            report["registry_lock"] = {name: value for name, value in contents.get("registryFileHashes", {}).items()
                if name in {"https://bcr.bazel.build/modules/rules_rs/0.1.3/MODULE.bazel",
                            "https://bcr.bazel.build/modules/rules_rs/0.1.3/source.json"}
                and re.fullmatch(r"[0-9a-f]{64}", value or "")}
    elif PHASE == "fetch":
        report = json.loads(RECEIPT.read_text())
        assert "rules_rs" not in rows(TRIAL.read_text())
        log = run("fetch", ["bazel", "run", "-c", "opt", TARGET, "--", "--mode=fetch", "--yaml=" + str(TRIAL)], report)
        report["rust_fetch_stdout"] = bool(re.search(r"(?m)^rules_rs\s+Apache-2\.0\s*$", log))
        raw = TRIAL.read_text()
        generated = rows(raw)
        actual = generated.get("rules_rs", "")
        report["recovered"] = {"url": URL in actual, "commit": COMMIT in actual, "archive": ARCHIVE in actual,
                               "license": "  license: Apache-2.0\n" in actual}
        expected = rows(CATALOG.read_text())
        report["unrelated_changed_count"] = sum(expected[k] != generated.get(k) for k in expected if k != "rules_rs")
        report["changed_owner_sample"] = sorted(k for k in expected if k != "rules_rs" and expected[k] != generated.get(k))[:30]
        report["tracked_catalog_preserved"] = hashlib.sha256(CATALOG.read_bytes()).hexdigest() == report["catalog_sha256"]
        run("fetched_verify", ["bazel", "run", "-c", "opt", TARGET, "--", "--mode=verify", "--yaml=" + str(TRIAL)], report)
    else:
        raise ValueError(PHASE)
    RECEIPT.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
