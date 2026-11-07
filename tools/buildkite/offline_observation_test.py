"""Run only synthetic inputs through the submitted Buildkite CLI functions."""

import json
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
    self.assertNotIn(log[1], observed)
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
      jobs = [{"id": a, "state": "finished", "passed": False, "name": "sandbox", "retried_in_job_uuid": b},
              {"id": b, "state": "running" if successor == "running" else "finished",
               "passed": successor == "passed", "name": "sandbox"}]
      details = {"number": 1, "path": "/gvisor/pipeline/builds/1", "state": successor}
      with (mock.patch.object(buildkite, "_fetch", side_effect=AssertionError("unexpected network")),
            mock.patch.object(buildkite, "_fetch_json", return_value=details),
            mock.patch.object(buildkite, "_fetch_records", side_effect=lambda url: jobs if url == build.data_url("jobs") else []),
            mock.patch.object(buildkite, "_add_log") as logs):
        report = buildkite.failed(build, logs=True)
      self.assertEqual(len(report["jobs"]), 1)
      observed = report["jobs"][0].get("retry")
      self.assertEqual(observed, "pending" if successor == "running" else "passed")
      self.assertEqual(logs.call_count, int(successor == "running"))
      observations[successor] = {"retry": observed, "logs_consulted": logs.call_count}
    print("OBSERVATION=" + json.dumps({"retry_disposition": observations}))

  def test_annotation_reports_final_status_with_logs(self):
    build = buildkite.Build("gvisor", "pipeline", 1)
    job = "00000000-0000-0000-0000-000000000001"
    run = "00000000-0000-0000-0000-000000000002"
    # The repository's post-command serializes each target and its log URL;
    # tools/bazel.mk testlogs includes both FLAKY and FAILED summaries.
    links = [("recovered", "attempt_1.log"), ("failed", "test.log")]
    html = "".join(f'<li><a href="https://storage.googleapis.com/gvisor-buildkite/{run}/{job}/{name}/{path}">//pkg:{name}</a></li>' for name,path in links)
    annotation = [{"context": "failures", "body": {"html": html}}]
    details = {"number": 1, "path": "/gvisor/pipeline/builds/1", "state": "failed"}
    jobs = [{"id": job, "name": "sandbox", "state": "finished", "passed": False}]
    log = b"//pkg:recovered  FLAKY, failed in 1 out of 2 in 1s\n//pkg:failed  FAILED in 2s\n"
    with (mock.patch.object(buildkite, "_fetch", return_value=log),
          mock.patch.object(buildkite, "_fetch_json", return_value=details),
          mock.patch.object(buildkite, "_fetch_records", side_effect=lambda url: jobs if url == build.data_url("jobs") else annotation)):
      simple = buildkite.failed(build)["jobs"][0]
      annotated = buildkite.failed(build, logs=True)["jobs"][0]
    self.assertNotIn("failed_targets", simple)
    self.assertEqual([e["target"] for e in simple["annotated_targets"]], ["//pkg:recovered", "//pkg:failed"])
    statuses = {e["target"]: e["status"] for e in annotated["annotated_targets"]}
    self.assertEqual(statuses, {"//pkg:recovered": "FLAKY", "//pkg:failed": "FAILED"})
    print("OBSERVATION=" + json.dumps({"statuses": statuses}))


if __name__ == "__main__":
  unittest.main()
