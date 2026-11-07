"""Run only synthetic inputs through the submitted Buildkite CLI functions."""

import json
import re
import unittest
from unittest import mock

import buildkite


class OfflineObservation(unittest.TestCase):

  def test_passing_case_and_timeout(self):
    log = [
        "=== RUN   TestRejectBadPeer",
        "    proxy_test.go:81: connection reset by peer (expected)",
        "--- PASS: TestRejectBadPeer (0.00s)",
        "=== RUN   TestStalled",
        "panic: test timed out after 1m0s",
        "//pkg:proxy_test     TIMEOUT in 60.0s",
    ]
    observed = buildkite.analyze_log(log, 0).get("failure_lines", [])
    self.assertIn(log[4], observed)
    self.assertIn(log[5], observed)
    print("OBSERVATION=" + json.dumps({
        "passing_test_message_in_failure_lines": log[1] in observed,
        "passing_test_verdict_in_failure_lines": log[2] in observed,
        "timed_out_test_seen": log[4] in observed,
    }))

  def test_retry_disposition(self):
    build = buildkite.Build("gvisor", "pipeline", 1)
    a = "00000000-0000-0000-0000-000000000001"
    b = "00000000-0000-0000-0000-000000000002"
    observations = {}
    for successor in ("running", "passed"):
      jobs = [{"id": a, "state": "failed", "name": "sandbox", "retried_in_job_uuid": b},
              {"id": b, "state": successor, "name": "sandbox"}]
      state = "running" if successor == "running" else "passed"
      details = {"number": 1, "path": "/gvisor/pipeline/builds/1", "state": state}
      with (mock.patch.object(buildkite, "_fetch", side_effect=AssertionError("unexpected network")),
            mock.patch.object(buildkite, "_fetch_json", return_value=details),
            mock.patch.object(buildkite, "_fetch_records", return_value=[]),
            mock.patch.object(buildkite, "_jobs", side_effect=lambda build, state: [j for j in jobs if j["state"] == state]),
            mock.patch.object(buildkite, "_add_log") as logs):
        report = buildkite.failed(build, logs=True)
      self.assertEqual(len(report["jobs"]), 1)
      observations[successor] = {
          "reported_attempt": report["jobs"][0]["url"].rsplit("#", 1)[1],
          "retried": report["jobs"][0].get("retried", False),
          "logs_consulted": logs.call_count,
      }
    print("OBSERVATION=" + json.dumps({"retry_disposition": observations}))

  def test_annotation_cannot_distinguish_recovered_test(self):
    build = buildkite.Build("gvisor", "pipeline", 1)
    job = "00000000-0000-0000-0000-000000000001"
    run = "00000000-0000-0000-0000-000000000002"
    # The repository's post-command serializes each target and its log URL;
    # tools/bazel.mk testlogs includes both FLAKY and FAILED summaries.
    links = [("recovered", "attempt_1.log"), ("failed", "test.log")]
    html = "".join(f'<li><a href="https://storage.googleapis.com/gvisor-buildkite/{run}/{job}/{name}/{path}">//pkg:{name}</a></li>' for name,path in links)
    annotation = [{"context": "failures", "body": {"html": html}}]
    details = {"number": 1, "path": "/gvisor/pipeline/builds/1", "state": "failed"}
    with (mock.patch.object(buildkite, "_fetch", side_effect=AssertionError("unexpected network")),
          mock.patch.object(buildkite, "_fetch_json", return_value=details),
          mock.patch.object(buildkite, "_fetch_records", return_value=annotation),
          mock.patch.object(buildkite, "_jobs", side_effect=lambda build,state: [{"id":job,"name":"sandbox","state":"failed"}] if state=="failed" else [])):
      report = buildkite.failed(build, regex=re.compile("recovered"))
    observed = [t["target"] for j in report["jobs"] for t in j.get("failed_targets", [])]
    print("OBSERVATION=" + json.dumps({"annotated_failed_targets": observed,
          "recovered_target_regex_selected_job": bool(report["jobs"])}))


if __name__ == "__main__":
  unittest.main()
