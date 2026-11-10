import copy
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import outreach_delivery as delivery


class ReviewDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.number = 123
        self.key = f"123:{'a' * 40}:reviewer"
        self.action = {"status": "due", "number": self.number, "reviewer": "reviewer", "head": "a" * 40,
                       "channel": "request", "key": self.key,
                       "body": f"@reviewer, could you review this change?\n\nPosted by Codex on behalf of @tamird.\n\n<!-- gvisor-review-outreach {self.key} -->"}
        self.requests = "repos/google/gvisor/pulls/123/requested_reviewers"
        self.comments = "repos/google/gvisor/issues/123/comments"
        self.receipt = {"id": 456, "user": {"login": "tamird"}, "body": self.action["body"],
                        "issue_url": "https://api.github.com/repos/google/gvisor/issues/123",
                        "html_url": "https://github.com/google/gvisor/pull/123#issuecomment-456"}
        self.api = self.enterContext(patch.object(delivery, "api"))
        self.registry = self.enterContext(patch.object(delivery, "registry_is_published", return_value=True))
        self.live = MagicMock(return_value=copy.deepcopy(self.action))

    def send(self):
        return delivery.send_one(self.action, self.live, "tamird")

    def test_confirm_native_request(self):
        self.api.side_effect = [{}, {"users": [{"login": "Reviewer"}]}]
        self.assertEqual(self.send(), {"status": "requested", "reviewer": "reviewer"})
        self.assertEqual(self.api.call_args_list[0].args, (self.requests, {"reviewers": ["reviewer"]}))
        self.assertEqual(self.api.call_count, 2)

    def test_definite_rejection_rechecks_then_falls_back_once(self):
        self.api.side_effect = [delivery.GitHubError(404, "Not Found"), {"users": []}, {"id": 456}, self.receipt]
        self.assertEqual(self.send(), {"status": "commented", "url": self.receipt["html_url"], "fallback": True})
        self.assertEqual(self.live.call_count, 2)
        self.assertEqual(self.registry.call_count, 2)
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
