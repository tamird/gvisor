"""Recover the original successful C++ actions' ELF inputs; run no builds."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

from cas_inputs import blob, cas, input_file, rpc
from static_build import elf

SOURCE = "c4eed9ce9e501de2633c1295205bf20a404f6397"
ORIGINALS = {
    "amd64": ("3238a7af-1331-4c8c-9792-9eac00b6718c", 1793528801222, "k8", 62,
              "ld-linux-x86-64.so.2", "41daf7836f7fce21a40ca63a00c5caf21b05a35a76ccc075d86f1716212cdf01"),
    "arm64": ("d7e6fd50-9a2e-4c3c-a926-a5faff02f1df", 1793529024254, "aarch64", 183,
              "ld-linux-aarch64.so.1", "1f4fe51e11324b5bcc01ef1fd3859b5dd344ec16a8674d74ce618ef65016fbaf"),
}
TARGET = "//test/util:posix_error_test"
TMP = Path(os.environ["RUNNER_TEMP"])
KEY = os.environ["BUILDBUDDY_API_KEY"]


def main(result):
    original = subprocess.run(["gh", "api", "repos/tamird/gvisor/actions/artifacts/13079933163/zip"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    assert original.returncode == 0, "Original artifact unavailable"
    assert hashlib.sha256(original.stdout).hexdigest() == "79427044cbef99b9c1a65fbf87373085ce834e944dc4869bab2e1831b6afbce7", "Original artifact hash mismatch"
    with zipfile.ZipFile(io.BytesIO(original.stdout)) as z:
        summary = json.loads(z.read("summary.json"))
        previous = json.loads(z.read("result.json"))
        assert summary["source"] == SOURCE and previous["source_and_binary_identity"], "Original source identity mismatch"
        result["original_aggregate"] = previous
        for prior in summary["rows"]:
            arch = prior["arch"]
            invocation, finish, output_arch, machine, loader_name, loader_hash = ORIGINALS[arch]
            tests = json.loads(z.read(arch + "/tests.json"))
            assert tests["status"] == prior["actions"]["status"] == "PASS", "Original behavior was not qualified"
            assert tests["case_counts"] == {"passed": 3, "failed": 0, "skipped": 0} and not tests["missing"] and not tests["unpassed_required_cases"], "Original cases differ"
            assert prior["phases"]["test"]["invocation_ids"] == [invocation], "Original invocation mismatch"
            row = {"status": "INCOMPLETE", "invocation": invocation, "behavior_status": "PASS",
                   "original_execution_finished_at": tests["execution_finished_at"],
                   "original_behavior_verdict_at": tests["verdict_available_at"],
                   "original_producer_verdict_at": prior["verified_at"], "cases": tests["case_counts"]}
            result["architectures"][arch] = row
            values = rpc("GetInvocation", {"lookup": {"invocationId": invocation}}).get("invocation", [])
            values = [v for v in values if v.get("invocationId") == invocation]
            assert len(values) == 1, "Original invocation unavailable"
            terminal = [e["buildEvent"]["finished"] for e in values[0].get("event", []) if "finished" in e.get("buildEvent", {})]
            assert len(terminal) == 1 and terminal[0].get("overallSuccess"), "Original terminal success unavailable"
            actual_finish = terminal[0].get("finishTimeMillis")
            if actual_finish is None:
                actual_finish = round(datetime.fromisoformat(terminal[0]["finishTime"]).timestamp() * 1000)
            assert int(actual_finish) == finish, "Original finish identity mismatch"
            witness = prior["actions"]["tests"][0]
            assert len(prior["actions"]["tests"]) == 1 and witness["target"] == TARGET
            digest = witness["digest"]
            executions = rpc("GetExecution", {"executionLookup": {"invocationId": invocation,
                "actionDigestHash": digest["hash"]}, "inlineExecuteResponse": True}).get("execution", [])
            executions = [e for e in executions if e.get("actionDigest", {}).get("hash") == digest["hash"]]
            assert len(executions) == 1, "Original action is missing or ambiguous"
            execution = executions[0]
            response = execution.get("executeResponse", {})
            assert execution.get("stage") in (4, "COMPLETED") and not execution.get("status", {}).get("code", 0)
            assert "result" in response and not response.get("status", {}).get("code", 0) and not response.get("cachedResult", False)
            assert not response["result"].get("exitCode", 0), "Original action failed"
            action = cas(digest, "Action")
            command = cas(action["commandDigest"], "Command")
            assert command["arguments"] == [witness["executable"], *witness["arguments"]], "Original command differs"
            properties = {p["name"]: p["value"] for p in (action.get("platform") or command.get("platform", {})).get("properties", [])}
            assert all(properties.get(k) == v for k, v in witness["platform"].items()), "Original worker differs"
            prefix = "bazel-out/" + output_arch + "-fastbuild/bin/test/util/posix_error_test"
            runfiles = prefix + ".runfiles/_main/"
            test_path = runfiles + witness["test_binary"]
            test_file = input_file(action["inputRootDigest"], test_path)
            test_elf = elf(blob(test_file["digest"], invocation))
            assert test_elf["machine"] == machine and len(test_elf["interpreter"]) == 1
            assert Path(test_elf["interpreter"][0]).name == loader_name and "libc.so.6" in test_elf["needed"], "Original test did not use the expected GNU ABI"
            loader_file = input_file(action["inputRootDigest"], runfiles + "test/rbe/native_glibc/loader")
            loader = elf(blob(loader_file["digest"], invocation))
            assert loader["sha256"] == loader_hash and loader["machine"] == machine and not loader["interpreter"] and not loader["needed"], "Original loader differs"
            libc_file = input_file(action["inputRootDigest"], runfiles + "test/rbe/native_glibc/lib/libc.so.6")
            libc = elf(blob(libc_file["digest"], invocation))
            assert libc["machine"] == machine, "Original libc architecture differs"
            row.update(status="PASS", action=digest, input_root=action["inputRootDigest"], worker=witness["platform"],
                       test_path=test_path, test_elf=test_elf, loader=loader, libc=libc)
    assert set(result["architectures"]) == set(ORIGINALS)
    result["status"] = "PASS"


result = {"status": "INCOMPLETE", "source": SOURCE, "original_run": 41297166023,
          "original_artifact": 13079933163, "metadata_only": True, "new_test_executions": 0,
          "architectures": {}, "started_at": datetime.now(timezone.utc).isoformat()}
try:
    main(result)
except Exception as err:
    result["failure"] = type(err).__name__
    if isinstance(err, (AssertionError, KeyError)):
        result["reason"] = str(err).replace(KEY, "[REDACTED]")[:1500]
finally:
    result["verified_at"] = datetime.now(timezone.utc).isoformat()
    (TMP / "cpp-elf-recovery.json").write_text(json.dumps(result, indent=2) + "\n")
print(result["status"])
raise SystemExit(result["status"] != "PASS")
