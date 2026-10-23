"""Compare the real PHP fixtures and run the existing filtered runtime owner."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import qualification_report as report

from image_archive import identity
from image_runtime import prepared_runtime

SAMPLES = [
    "tests/basic/001.phpt",
    "sapi/cli/tests/001.phpt",
    "sapi/cgi/tests/001.phpt",
    "sapi/phpdbg/tests/basic_run.phpt",
    "ext/sodium/tests/crypto_hex.phpt",
    "ext/bcmath/tests/GHSA-x692-q9x7-8c3f.phpt",
    "ext/pdo_pgsql/tests/transations_deprecations.phpt",
]


def run(invoke, evidence: Path, private: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    contents = json.loads((evidence / "php-contents.json").read_text())
    proctor = contents["proctor"]["producer"]["output"]
    if contents["status"] != "PASS" or contents["source"] != config["source"] or identity(Path(proctor["path"])) != proctor:
        raise ValueError("PHP runtime requires exact inspected fixture and proctor bytes")
    result = {"source": config["source"], "status": "INCOMPLETE", "samples": SAMPLES,
              "proctor": contents["proctor"], "commands": [], "observations": []}
    deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])
    owned = []
    cleanup_deadline = deadline + 90

    def command(name, arguments, *, allow_failure=False, cleanup=False):
        remaining = (cleanup_deadline if cleanup else deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("PHP qualification deadline exhausted")
        stdout, stderr = evidence / (name + ".stdout.log"), evidence / (name + ".stderr.log")
        with stdout.open("wb") as out, stderr.open("wb") as err:
            proc = subprocess.run(arguments, stdout=out, stderr=err, timeout=remaining)
        row = {"name": name, "arguments": arguments, "native_exit": proc.returncode,
               "stdout": identity(stdout), "stderr": identity(stderr)}
        result["commands"].append(row)
        if proc.returncode and not allow_failure:
            raise RuntimeError(f"{name} failed with exit {proc.returncode}; see {stderr.name}")
        return proc.returncode, stdout.read_text()

    def inventory(name, *, cleanup=False):
        _, text = command(name, ["docker", "ps", "-aq", "--no-trunc"], cleanup=cleanup)
        return sorted(text.split())

    def remove(name, *, cleanup=False):
        code, _ = command("remove-" + name, ["docker", "rm", "--force", name], allow_failure=True, cleanup=cleanup)
        if code:
            _, existing = command("remaining-" + name, ["docker", "ps", "-aq", "--no-trunc", "--filter", "name=^/" + name + "$"], cleanup=cleanup)
            if existing.strip():
                raise RuntimeError("Owned PHP container remains after cleanup: " + name)

    before = inventory("containers-before")
    runtime = "qualification-php-" + uuid.uuid4().hex
    preparation = evidence / "php-runtime-preparation.json"
    setup = {"runtime_name": runtime, "image_name": "runtimes/php8.5.11",
             "runtime_flags": ["--platform=systrap", "--network=sandbox"],
             "contents": str(evidence / "php-contents.json")}
    try:
        command("baseline-pull", ["docker", "pull", config["baseline_image"]])
        _, raw = command("baseline-inspect", ["docker", "image", "inspect", config["baseline_image"]])
        match json.loads(raw):
            case [baseline] if baseline["Id"] == config["baseline_config_id"] and baseline["Architecture"] == "amd64" and baseline["Os"] == "linux":
                result["baseline_image"] = baseline
            case other:
                raise ValueError(f"Immutable baseline identity mismatch: {other}")
        with prepared_runtime(preparation, private / "php-runtime", setup) as prepared:
            new_id = prepared["image"]["id"]
            cases = [("before-native", "runc", config["baseline_config_id"], "sapi/cli/php"),
                     ("after-native", "runc", new_id, "/usr/local/bin/php"),
                     ("after-netstack", runtime, new_id, "/usr/local/bin/php")]
            for side, runtime_name, image_id, php in cases:
                name = "qualification-php-" + uuid.uuid4().hex
                owned.append(name)
                # Allocate the exact owned name before creation so failed startup
                # cannot bypass cleanup. Preserve each image's real entrypoint.
                command(side + "-create", ["docker", "create", "--name", name, "--runtime=" + runtime_name,
                    "--mount", "type=bind,src=" + proctor["path"] + ",dst=/proctor/proctor_bin,readonly",
                    "--env", "NO_INTERACTION=1", "--env", "REPORT_EXIT_STATUS=1", "--env", "TERM=dumb",
                    image_id, "/proctor/proctor_bin", "--pause"])
                command(side + "-start", ["docker", "start", name])
                _, raw = command(side + "-container-inspect", ["docker", "inspect", name])
                match json.loads(raw):
                    case [container] if container["Image"] == image_id and container["HostConfig"]["Runtime"] == runtime_name and container["State"]["Running"]:
                        pass
                    case other:
                        raise ValueError(f"Actual container identity/runtime mismatch: {other}")
                _, listing = command(side + "-list", ["docker", "exec", name, "/proctor/proctor_bin", "--runtime=php", "--list"])
                names = sorted(listing.splitlines())
                if len(names) != len(set(names)) or len(names) != 21872 or hashlib.sha256(("\n".join(names) + "\n").encode()).hexdigest() != config["phpt_names_sha256"]:
                    raise ValueError("Actual proctor selected-name list differs: " + side)
                row = {"side": side, "image_id": image_id, "runtime": runtime_name, "container": name,
                       "selected_names": {"count": len(names), "sha256": config["phpt_names_sha256"]}, "php": php}
                result["observations"].append(row)
                inventories = {}
                expression = '$e=get_loaded_extensions();sort($e);$d=ini_get("extension_dir");$s=glob($d."/*.so");if($s===false){throw new Exception("Cannot enumerate extension directory");}sort($s);echo json_encode(["version"=>PHP_VERSION,"sapi"=>PHP_SAPI,"modules"=>$e,"ini"=>php_ini_loaded_file(),"scanned"=>php_ini_scanned_files(),"extension_dir"=>$d,"shared"=>$s]),"\\n";'
                for mode, flags in (("normal", []), ("no_ini", ["-n"])):
                    _, raw = command(side + "-" + mode, ["docker", "exec", name, php, *flags, "-r", expression])
                    observed = json.loads(raw)
                    if observed["version"] != "8.5.11" or observed["sapi"] != "cli":
                        raise ValueError("Actual interpreter version/SAPI differs")
                    inventories[mode] = observed
                row["inventories"] = inventories
                for sapi, binary in (("cgi", "sapi/cgi/php-cgi" if side.startswith("before") else "/usr/local/bin/php-cgi"),
                                     ("phpdbg", "sapi/phpdbg/phpdbg" if side.startswith("before") else "/usr/local/bin/phpdbg")):
                    code, raw = command(side + "-" + sapi + "-version", ["docker", "exec", name, binary, "-v"], allow_failure=True)
                    row[sapi] = {"path": binary, "native_exit": code, "version_output": raw}
                if side.startswith("before"):
                    arguments = ["make", "test", "TESTS=" + " ".join(SAMPLES)]
                else:
                    arguments = ["/proctor/proctor_bin", "--runtime=php", "--tests=" + ",".join(SAMPLES),
                                 "--timeout=5m", "--per_test_timeout=4m", "--runs_per_test=1"]
                code, raw = command(side + "-phpt", ["docker", "exec", name, *arguments], allow_failure=True)
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw).replace("\r", "\n")
                statuses = {}
                for status, test, reason in re.findall(r"^(PASS|SKIP|FAIL|BORK|WARN|XFAIL|XLEAK|LEAK) [^\n]*?\[([^\]\n]+\.phpt)\]([^\n]*)$", clean, re.M):
                    if test in statuses:
                        raise ValueError("Duplicate PHPT result: " + test)
                    statuses[test] = {"status": status, "reason": reason.strip()}
                row["native_exit"], row["statuses"] = code, statuses
                if set(statuses) != set(SAMPLES) or code or any(v["status"] not in ("PASS", "SKIP") for v in statuses.values()):
                    raise ValueError("PHP run did not report the exact successful/explicit-skip sample set: " + side)
                for sample in SAMPLES[:4]:
                    if statuses[sample]["status"] != "PASS":
                        raise ValueError("Required CLI/CGI/phpdbg coverage is absent: " + sample)
                for sample in SAMPLES[-2:]:
                    if statuses[sample]["status"] != "SKIP":
                        raise ValueError("Expected missing optional prerequisite was not retained: " + sample)
                remove(name)
                owned.remove(name)
            before_row, after_row, netstack_row = result["observations"]
            if not set(before_row["inventories"]["no_ini"]["modules"]) <= set(after_row["inventories"]["no_ini"]["modules"]):
                raise ValueError("Official build lost a compiled-in baseline module")
            if after_row["statuses"] != netstack_row["statuses"]:
                raise ValueError("Native/gVisor selected PHPT results differ")
            for row in (after_row, netstack_row):
                if row["statuses"][SAMPLES[4]]["status"] != "PASS":
                    raise ValueError("Installed sodium shared-extension autoload was not exercised")
            result["module_differences"] = {
                mode: {"added": sorted(set(after_row["inventories"][mode]["modules"]) - set(before_row["inventories"][mode]["modules"])),
                       "removed": sorted(set(before_row["inventories"][mode]["modules"]) - set(after_row["inventories"][mode]["modules"]))}
                for mode in ("normal", "no_ini")}
            result["status_differences"] = {name: {"before": before_row["statuses"][name], "after": after_row["statuses"][name]}
                                            for name in SAMPLES if before_row["statuses"][name] != after_row["statuses"][name]}
            target = "//test/runtimes:php8.5.11"
            options = ["--compilation_mode=opt", "--config=x86_64", "--lockfile_mode=error",
                       "--noannounce_rc", "--remote_download_outputs=all",
                       "--build_event_json_file_path_conversion=false", "--nozip_undeclared_test_outputs",
                       "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                       "--test_sharding_strategy=disabled", "--strategy=TestRunner=local", "--local_test_jobs=1",
                       "--test_timeout=600", "--test_arg=--runtime=" + runtime,
                       "--test_arg=--batch=1", "--test_arg=--tests=" + ",".join(SAMPLES),
                       "--test_arg=-test.v=true"]
            analyzed = invoke("php-consumer-analysis", ["test", *options, "--nobuild", target], "analysis", targets=[target])
            if analyzed["status"] != "PASS":
                raise RuntimeError("Existing PHP runtime owner analysis failed")
            tested = invoke("php-consumer", ["test", *options, target], "test",
                            (evidence / "invocation-php-consumer-analysis/events.jsonl",), targets=[target])
            _, retained, errors = report.retained_reports(report.manifest_path(evidence / "invocation-php-consumer/events.jsonl"))
            if errors:
                raise ValueError("Cannot authenticate PHP consumer reports: " + str(errors))
            xmls = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.xml"]
            logs = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.log"]
            match xmls, logs:
                case [(attempt, xml)], [(log_attempt, log)] if attempt == log_attempt and attempt.owner.label == target:
                    pass
                case _:
                    raise ValueError("Expected one matching actual PHP runtime XML/log attempt")
            raw = log.read_text()
            selected = re.findall(r"Running test case batch: ([^\n]+)", raw)
            passed = re.findall(r"^--- PASS: (batch_[0-9]+) ", raw, re.M)
            if sorted(selected) != sorted(SAMPLES) or sorted(passed) != ["batch_" + str(i) for i in range(7)]:
                raise ValueError("Existing PHP runtime owner did not execute exactly the selected seven batches")
            if tested["status"] != "PASS" or len(tested["executed"]) != 1 or any(
                    item["cached_locally"] or item["execution_info"].get("cachedRemotely", False) for item in tested["executed"]):
                raise ValueError("Existing PHP runtime consumer did not pass in one uncached attempt")
            counts = report.xml_cases(xml)
            if not counts["passed"] or counts["failed"] or counts["skipped"]:
                raise ValueError("Existing PHP runtime XML is empty, failed or skipped")
            result["consumer"] = {"owner": target, "attempt": asdict(attempt), "xml": report.file_record(xml),
                                  "log": report.file_record(log), "counts": counts, "selected": selected,
                                  "passed_batches": passed, "native_attempts": tested["executed"],
                                  "xml_scope": "Bazel runtime rule wrapper; exact seven batches independently checked in authenticated native log"}
            result["status"] = "PASS"
    finally:
        completed = result["status"]
        result["status"] = "FAIL"
        try:
            cleanup_errors = []
            for name in owned:
                try:
                    remove(name, cleanup=True)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    cleanup_errors.append(str(error))
            result["cleanup_errors"] = cleanup_errors
            after = inventory("containers-after", cleanup=True)
            result["container_inventory"] = {"before": before, "after": after}
            if cleanup_errors or after != before:
                raise RuntimeError("PHP qualification cleanup failed or changed the unrelated container inventory")
            result["cleanup"] = "PASS"
            result["status"] = completed
        finally:
            (evidence / "php-runtime-results.json").write_text(json.dumps(result, indent=2) + "\n")
