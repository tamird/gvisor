import copy
import json
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, call, patch

import outreach_delivery as delivery


class ReviewDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.mode = self.enterContext(patch.object(delivery, "live_posting_mode", return_value="all"))
        self.number = 123
        self.key = f"123:{'a' * 40}:reviewer"
        self.versions = {"head": "a" * 40, "base": "b" * 40}
        self.action = {**self.versions, "mergeable": "MERGEABLE", "status": "due", "number": self.number, "reviewer": "reviewer", "head": "a" * 40,
                       "channel": "request", "key": self.key,
                       "body": f"@reviewer, could you review this change?\n\nPosted by Codex on behalf of @tamird.\n\n<!-- gvisor-review-outreach {self.key} -->"}
        self.requests = "repos/google/gvisor/pulls/123/requested_reviewers"
        self.comments = "repos/google/gvisor/issues/123/comments"
        self.receipt = {"id": 456, "user": {"login": "tamird"}, "body": self.action["body"],
                        "issue_url": "https://api.github.com/repos/google/gvisor/issues/123",
                        "html_url": "https://github.com/google/gvisor/pull/123#issuecomment-456"}
        self.api = self.enterContext(patch.object(delivery, "api"))
        self.registry_oid = "c" * 40
        self.check_publication = delivery.registry_is_published
        self.registry = self.enterContext(patch.object(delivery, "registry_is_published", return_value=True))
        self.live = MagicMock(return_value=copy.deepcopy(self.action))

    def send(self):
        return delivery.send_one(self.action, self.live, "tamird", self.registry_oid)

    def test_only_fresh_source_rotations_may_write_during_a_pause(self):
        self.mode.return_value = "rotations"
        for channel in ("request", "comment"):
            self.action.update(channel=channel, sourceRotation=True)
            for stale in ({"sourceRotation": False}, {"kind": "import-status"}, {}):
                fresh = {**self.action, **stale}
                if not stale:
                    fresh.pop("sourceRotation")
                self.live.return_value = fresh
                self.assertEqual(self.send()["status"], "held")
                self.api.assert_not_called()
    def test_rotation_uses_only_the_native_request(self):
        self.mode.return_value = "rotations"
        self.action.update(sourceRotation=True, lastContactAt="2026-11-09T20:00:00Z")
        self.live.return_value = self.action
        self.api.side_effect = [{"content": "e30="}, {}, {"users": [{"login": "reviewer"}]}]
        self.assertEqual(self.send()["status"], "requested")
        self.action["channel"] = "comment"
        self.api.reset_mock()
        self.assertEqual(self.send()["status"], "held")
        self.api.assert_not_called()

    def test_rotations_hold_after_a_definitive_native_denial(self):
        self.mode.return_value = "rotations"
        self.action.update(sourceRotation=True, lastContactAt="2026-11-09T20:00:00Z")
        self.live.return_value = self.action
        for status in (403, 404, 422):
            with self.subTest(status=status), patch.object(delivery, "save_native_denial") as save:
                self.api.reset_mock()
                self.api.side_effect = [{"content": "e30="}, delivery.GitHubError(status, "Rejected"), {"users": []}]
                self.assertEqual(self.send()["status"], "native-request-denied")
                save.assert_called_once_with(self.action, status)
                self.assertEqual([c.args for c in self.api.call_args_list if len(c.args) == 2],
                                 [(self.requests, {"reviewers": ["reviewer"]})])

    def test_pause_during_initial_request_does_not_record_a_rotation(self):
        self.mode.side_effect = ["all", "rotations"]
        self.api.side_effect = [delivery.GitHubError(403, "Forbidden"), {"users": []}]
        with patch.object(delivery, "save_native_denial") as save:
            self.assertEqual(self.send()["status"], "held")
            save.assert_not_called()
        self.assertEqual([c.args for c in self.api.call_args_list if len(c.args) == 2],
                         [(self.requests, {"reviewers": ["reviewer"]})])

    def test_stale_snapshot_observes_published_denial_before_a_write(self):
        self.mode.return_value = "rotations"
        self.action.update(sourceRotation=True, lastContactAt="2026-11-09T20:00:00Z")
        self.live.return_value = self.action
        hold = {k: self.action[k] for k in ("head", "reviewer", "lastContactAt")}
        self.api.return_value = {"content": delivery.base64.b64encode(json.dumps({"123": {**hold, "http": 422}}).encode()).decode()}
        self.assertEqual(self.send()["status"], "native-request-denied")
        self.api.assert_called_once()

    def test_denial_is_persisted_to_the_fork_with_its_observed_file_sha(self):
        action = {**self.action, "lastContactAt": "2026-11-09T20:00:00Z"}
        prior = {"124": {"http": 403, "reviewer": "other"}}
        file = {"sha": "d" * 40, "content": delivery.base64.b64encode(json.dumps(prior).encode()).decode()}
        with patch.dict(delivery.os.environ, {"WORK_MAP_RECEIPT_TOKEN": "fork-only"}), \
             patch.object(subprocess, "check_output", return_value=json.dumps(file)), patch.object(subprocess, "run") as run:
            run.return_value.returncode = 0
            delivery.save_native_denial(action, 422)
        request = run.call_args
        self.assertEqual(request.args[0][-3:], ["repos/tamird/gvisor/contents/outreach-holds.json", "--input", "-"])
        payload = json.loads(request.kwargs["input"])
        self.assertEqual((payload["sha"], payload["branch"]), (file["sha"], "gh-pages"))
        saved = json.loads(delivery.base64.b64decode(payload["content"]))
        self.assertEqual(saved["124"], prior["124"])
        self.assertEqual((saved["123"]["head"], saved["123"]["lastContactAt"], saved["123"]["reviewer"], saved["123"]["http"]),
                         (action["head"], action["lastContactAt"], action["reviewer"], 422))

    def test_late_old_job_cannot_replace_a_denial_on_the_new_head(self):
        old = {**self.action, "lastContactAt": "2026-11-09T20:00:00Z"}
        newer = {"head": "b" * 40, "lastContactAt": old["lastContactAt"],
                 "observedAt": "2026-11-10T20:00:00Z", "reviewer": "someone", "http": 422}
        file = {"sha": "a" * 40, "content": delivery.base64.b64encode(json.dumps({"123": newer}).encode()).decode()}
        with patch.dict(delivery.os.environ, {"WORK_MAP_RECEIPT_TOKEN": "test"}), \
             patch.object(subprocess, "check_output", return_value=json.dumps(file)), patch.object(subprocess, "run") as put:
            self.api.return_value = {"head": {"sha": newer["head"]}}
            delivery.save_native_denial(old, 403)
            put.assert_not_called()

    def test_confirm_native_request(self):
        self.api.side_effect = [{}, {"users": [{"login": "Reviewer"}]}]
        self.assertEqual(self.send(), {**self.versions, "status": "requested", "reviewer": "reviewer"})
        self.assertEqual(self.api.call_args_list[0].args, (self.requests, {"reviewers": ["reviewer"]}))
        self.assertEqual(self.api.call_count, 2)

    def test_unknown_source_is_rechecked_but_cannot_send_until_proven_mergeable(self):
        self.action["status"] = "verify-mergeability"
        self.live.return_value = {**self.action, "mergeable": "UNKNOWN"}
        self.assertEqual(self.send(), {**self.versions, "status": "mergeability-unavailable"})
        self.api.assert_not_called()
        self.live.return_value = {**self.action, "status": "due", "mergeable": "MERGEABLE", "base": "c" * 40}
        self.api.side_effect = [{}, {"users": [{"login": "reviewer"}]}]
        self.assertEqual(self.send(), {"head": "a" * 40, "base": "c" * 40,
                                       "status": "requested", "reviewer": "reviewer"})

    def test_definite_rejection_rechecks_then_falls_back_once(self):
        self.api.side_effect = [delivery.GitHubError(404, "Not Found"), {"users": []}, {"id": 456}, self.receipt]
        self.assertEqual(self.send(), {**self.versions, "status": "commented", "url": self.receipt["html_url"], "fallback": True})
        self.assertEqual(self.live.call_count, 2)
        self.assertEqual(self.registry.call_args_list, [call(self.registry_oid), call(self.registry_oid)])
        self.assertEqual(self.api.call_args_list[2].args, (self.comments, {"body": self.action["body"]}))
        self.assertEqual(self.api.call_args_list[3].args, ("repos/google/gvisor/issues/comments/456",))

    def test_rejection_followed_by_a_new_request_is_not_a_reason_to_comment(self):
        self.api.side_effect = [delivery.GitHubError(422, "Already requested"), {"users": [{"login": "reviewer"}]}]
        self.assertEqual(self.send()["status"], "request-observed")
        self.assertEqual(self.api.call_count, 2)

    def test_failure_after_possible_write_never_falls_back(self):
        for status in (None, 500, 502):
            with self.subTest(status=status):
                self.api.reset_mock()
                self.api.side_effect = [delivery.GitHubError(status, "lost response"), {"users": []}]
                with self.assertRaises(delivery.GitHubError):
                    self.send()
                self.assertEqual(self.api.call_count, 2)

    def test_rate_limit_stops_without_a_second_request(self):
        self.api.side_effect = delivery.GitHubError(403, "Secondary rate limit", limited=True)
        with self.assertRaises(delivery.GitHubError):
            self.send()
        self.api.assert_called_once()

    def test_changed_head_recipient_and_registry_suppress_outreach(self):
        for key, value in (("head", "b" * 40), ("reviewer", "second"), ("key", "later"), ("status", "waiting")):
            with self.subTest(key=key):
                self.live.return_value = {**self.action, key: value}
                self.assertEqual(self.send()["status"], "superseded")
                self.api.assert_not_called()
        self.live.return_value = self.action
        self.registry.return_value = False
        self.assertEqual(self.send()["status"], "registry-changed")
        self.api.assert_not_called()

    def test_worktree_registry_differs_from_published_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            git = ["git", "-C", tmp]
            subprocess.run([*git, "init", "--quiet"], check=True)
            path.write_text('{"blocked": true}\n')
            subprocess.run([*git, "add", "registry.json"], check=True)
            subprocess.run([*git, "-c", "user.name=Test", "-c", "user.email=test@example.org",
                            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Published registry"], check=True)
            published = subprocess.check_output([*git, "rev-parse", "HEAD:registry.json"], text=True).strip()
            original, original_oid = delivery.load_registry(path)
            self.assertEqual(original_oid, published)
            path.write_text('{"blocked": false}\n')
            local, self.registry_oid = delivery.load_registry(path)
            self.assertNotEqual(original, local)
            # The file may even return to HEAD after the planner has read it.
            subprocess.run([*git, "restore", "registry.json"], check=True)
            self.api.return_value = {"sha": published}
            self.registry.side_effect = self.check_publication
            for channel in ("request", "comment"):
                with self.subTest(channel=channel):
                    self.action["channel"] = channel
                    self.live.return_value = self.action
                    self.api.reset_mock()
                    self.assertEqual(self.send()["status"], "registry-changed")
                    self.api.assert_called_once_with("repos/tamird/gvisor/contents/registry.json?ref=gh-pages")

    def test_fallback_stops_if_registry_changes_after_native_rejection(self):
        self.api.side_effect = [delivery.GitHubError(403, "Forbidden"), {"users": []}]
        self.registry.side_effect = [True, False]
        self.assertEqual(self.send()["status"], "registry-changed")
        self.assertEqual(self.registry.call_args_list, [call(self.registry_oid), call(self.registry_oid)])
        self.assertEqual(self.api.call_args_list, [call(self.requests, {"reviewers": ["reviewer"]}),
                                                 call(self.requests + "?per_page=100")])

    def test_permission_error_does_not_bypass_new_activity(self):
        self.api.side_effect = [delivery.GitHubError(403, "Forbidden"), {"users": []}]
        self.live.side_effect = [self.action, None]
        self.assertEqual(self.send()["status"], "superseded")
        self.assertEqual(self.api.call_count, 2)

    def test_comment_readback_binds_body_author_and_issue(self):
        self.action["channel"] = "comment"
        self.live.return_value = self.action
        self.api.side_effect = [{"id": 456}, {**self.receipt, "issue_url": "https://api.github.com/repos/google/gvisor/issues/789"}]
        with self.assertRaisesRegex(delivery.GitHubError, "verify"):
            self.send()


class GitHubStatusTest(unittest.TestCase):
    def test_fork_switches_are_read_fresh_even_with_a_conflicting_environment(self):
        with patch.dict(delivery.os.environ, {"WORK_MAP_POSTING_PAUSED": "false"}), patch("subprocess.run") as run:
            for paused, rotations, expected in (("false", "true", "all"), ("true", "true", "rotations"),
                                               ("true", "false", "paused"), ("true", "", "paused"),
                                               ("", "true", "unknown"), ("bad", "true", "unknown")):
                run.return_value.stdout = f"WORK_MAP_POSTING_PAUSED\t{paused}\nWORK_MAP_REVIEW_ROTATIONS_ENABLED\t{rotations}\n"
                self.assertEqual(delivery.live_posting_mode(), expected)
                self.assertIn("repos/tamird/gvisor/actions/variables?per_page=100", run.call_args.args[0])

    @patch("subprocess.run")
    def test_secondary_limit_with_generic_forbidden_status(self, run):
        run.return_value = subprocess.CompletedProcess([], 1,
            "HTTP/2.0 403 Forbidden\nRetry-After: 60\n\n" + json.dumps({"message": "Forbidden"}), "")
        with self.assertRaises(delivery.GitHubError) as ctx:
            delivery.api("user")
        self.assertTrue(ctx.exception.limited)
        self.assertEqual(ctx.exception.status, 403)

    @patch("subprocess.run")
    def test_proxy_error_cannot_authorize_a_fallback(self, run):
        for status, limited in ((403, False), (429, True)):
            with self.subTest(status=status):
                run.return_value = subprocess.CompletedProcess([], 1,
                    f"HTTP/2.0 {status} Error\nContent-Type: text/html\n\n<h1>Error</h1>", "")
                with self.assertRaises(delivery.GitHubError) as ctx:
                    delivery.api("user")
                self.assertIsNone(ctx.exception.status)
                self.assertEqual(ctx.exception.limited, limited)

    @patch("subprocess.run")
    def test_transport_failure_has_no_http_status(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "connection reset")
        with self.assertRaises(delivery.GitHubError) as ctx:
            delivery.api("user")
        self.assertIsNone(ctx.exception.status)
