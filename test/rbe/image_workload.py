"""Inspect the assembled official Ruby images; retain the prior HTTP results."""

import json
import xml.etree.ElementTree as ET

import qualification_report as report


def run(invoke, record_execution, evidence, private, output_root):
    names = {
        "//test/rbe/ruby_official:image_identity_test": "test/rbe/ruby_official/image_identity_test",
    }
    result = invoke("ruby-official-http", [
        "test", "--config=rbe-matrix", "--config=x86_64", "--compilation_mode=opt",
        "--lockfile_mode=error", "--noannounce_rc", "--keep_going",
        "--build_tag_filters=", "--test_tag_filters=",
        "--strategy=TestRunner=remote", "--nocache_test_results",
        "--runs_per_test=1", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
        "--test_timeout=900", "--test_output=all", "--remote_download_outputs=all",
        "--execution_log_compact_file=" + str(evidence / "native-execution.binpb"),
        *names,
    ], "test", targets=list(names))
    if result["status"] != "PASS":
        raise RuntimeError("Official Ruby HTTP/identity qualification failed")
    attempts = result["executed"]
    if len(attempts) != len(names) or {row["owner"]["label"] for row in attempts} != set(names):
        raise ValueError("Actual Ruby owner set differs")
    for row in attempts:
        execution = row["execution_info"]
        if (row["status"] != "PASSED" or row["cached_locally"] or execution.get("cachedRemotely", False)
                or execution.get("strategy") != "remote" or (row["run"], row["shard"], row["attempt"]) != (1, 1, 1)
                or (row["xml"]["passed"], row["xml"]["failed"], row["xml"]["skipped"]) != (1, 0, 0)):
            raise ValueError("Expected one uncached remote positive case per owner")
    directory = evidence / "invocation-ruby-official-http"
    _, reports, errors = report.retained_reports(directory / "events.jsonl.manifest.json")
    if errors:
        raise ValueError(errors)
    observed = {}
    for (attempt, kind), path in reports.items():
        if kind == "test.xml":
            actual = ["/".join(filter(None, (case.get("classname"), case.get("name")))) for case in ET.parse(path).getroot().iter("testcase")]
            if actual != [names[attempt.owner.label]] or attempt.owner.label in observed:
                raise ValueError(f"Unexpected actual case: {attempt}: {actual}")
            observed[attempt.owner.label] = {"case": actual[0], **report.file_record(path)}
    if set(observed) != set(names):
        raise ValueError("Missing original XML")
    (evidence / "ruby-http-required-cases.json").write_text(json.dumps(observed, indent=2) + "\n")
