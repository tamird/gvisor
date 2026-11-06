import unittest

from incoming_reviews import addressed, discover

EARLY = "2026-11-06T14:00:00Z"
ASK = "2026-11-06T15:50:13Z"
LATER = "2026-11-06T17:28:00Z"
REPO = "google/gvisor"
URL = "https://github.com/google/gvisor/pull/17082"
OWNER = "tamird"


def message(kind, name, body, time=ASK, author="ayushr2", **fields):
    return dict(
        kind=kind,
        id=name,
        url=URL + "#issuecomment-6689000753",
        body=body,
        createdAt=time,
        updatedAt=time,
        author={"__typename": "User", "login": author},
        **fields,
    )


def observe(messages, previous=None, complete=True):
    pr = dict(
        repo=REPO,
        number=17082,
        title="Delay ACKs",
        url=URL,
        head="a" * 40,
        authorLogin="ayushr2",
        status="open",
        feedback={"items": messages, "complete": complete},
    )
    requests, scan = discover(previous or {}, [pr], OWNER, None, LATER, 0)
    return {"incomingReviewRequests": requests, "incomingReviewScan": scan}


class IncomingReviewsTest(unittest.TestCase):
    def test_request_language(self):
        for text in (
            "@tamird PTAL",
            "PTAL @tamird",
            "cc @tamird can we get a review from you on this?",
            "@tamird, could you take another look?",
            "Please review this PR, @TamirD.",
            "@tamird @reviewer please review",
            "CC @tamird for review",
        ):
            with self.subTest(text=text):
                self.assertTrue(addressed(text, OWNER))
        for text in (
            "done @tamird",
            "@tamird thanks for your review",
            "@tamird, can you reproduce the 30s read?",
            "Previously raised by @tamird.",
            "PTAL @other. cc @tamird",
            "Please review @tamird's code",
            "This was the review @tamird submitted",
            "@tamird-alt PTAL",
            "> @tamird PTAL\nThanks for your review.",
            "`@tamird PTAL`",
            "```text\n@tamird PTAL\n```",
        ):
            with self.subTest(text=text):
                self.assertFalse(addressed(text, OWNER))

    def test_real_ptal_after_review_and_answered_controls(self):
        reviewed = message("review", "review", "", EARLY, OWNER, state="COMMENTED", submittedAt=EARLY)
        ask = message("comment", "ptal", "@tamird PTAL")
        question = message("comment", "question", "@tamird, what did your test report?")
        first = observe([reviewed, ask, question])
        self.assertEqual([row["id"] for row in first["incomingReviewRequests"]], ["ptal"])
        self.assertEqual(
            observe([reviewed, ask, question], first)["incomingReviewRequests"], first["incomingReviewRequests"]
        )
        approval = message("review", "approval", "", LATER, OWNER, state="APPROVED", submittedAt=LATER)
        self.assertEqual(observe([ask, approval], first)["incomingReviewRequests"], [])
        self.assertEqual(
            observe([message("comment", "done", "done @tamird"), approval])["incomingReviewRequests"], []
        )

    def test_inline_reply_and_resolution(self):
        ask = message("inline", "discussion-1", "@tamird PTAL", threadId="thread", isResolved=False)
        response = message("inline", "answer", "Handled", LATER, OWNER, threadId="different", isResolved=False)
        pending = observe([ask, response])
        self.assertEqual(len(pending["incomingReviewRequests"]), 1)
        response["threadId"] = "thread"
        self.assertFalse(observe([response], pending, complete=False)["incomingReviewRequests"])
        self.assertFalse(observe([dict(ask, isResolved=True)])["incomingReviewRequests"])

    def test_other_repositories_never_enter_pending_or_cursors(self):
        row = dict(repo="llvm/llvm-project", number=233715, kind="formal", id="supplier")
        stale = {
            "incomingReviewRequests": [row],
            "incomingReviewScan": {"repo": row["repo"], "queue": {"x": "supplier"}},
        }
        asks, scan = discover(stale, [dict(row, title="supplier")], OWNER, None, LATER, 0)
        self.assertEqual(asks, [])
        self.assertEqual(scan["queue"], {})

    def test_new_external_pr_and_formal_ids(self):
        identity = dict(
            id="PR_42",
            number=42,
            title="New change",
            url="https://github.com/google/gvisor/pull/42",
            state="OPEN",
            headRefOid="b" * 40,
            createdAt=EARLY,
            updatedAt=ASK,
            author={"__typename": "User", "login": "external"},
            repository={"nameWithOwner": REPO, "isPrivate": False},
        )
        empty = {"nodes": [], "pageInfo": {"hasPreviousPage": False, "startCursor": None}}
        source = dict(
            identity,
            body="@tamird PTAL",
            lastEditedAt=None,
            comments=empty,
            reviews=empty,
            reviewThreads=empty,
            reviewRequests={
                "nodes": [{"id": "request1", "requestedReviewer": {"__typename": "User", "login": OWNER}}],
                "pageInfo": {"hasNextPage": False},
            },
        )

        def api(query):
            if query.startswith("{nodes"):
                return {"nodes": [source]}
            if "p0:node" in query:
                return {
                    "p0": identity,
                    "c0": {"comments": dict(empty, nodes=[message("comment", "old", "@tamird please review")])},
                }
            page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [identity]}
            return {"formal": page, "mentioned": page, "repository": {"latest": page, "backfill": page}}

        def fetch(previous, time):
            records, scan = discover(previous, [], OWNER, api, time, 5)
            return {"incomingReviewRequests": records, "incomingReviewScan": scan}

        first = fetch({}, ASK)
        self.assertEqual({r["kind"] for r in first["incomingReviewRequests"]}, {"description", "formal"})
        again = fetch(first, LATER)
        self.assertEqual(again["incomingReviewRequests"], first["incomingReviewRequests"])
        source["reviewRequests"]["nodes"] = []
        source["body"] = ""
        self.assertFalse(fetch(again, LATER)["incomingReviewRequests"])
        source["comments"] = dict(empty, pageInfo={"hasPreviousPage": True, "startCursor": "older"})
        self.assertEqual([r["id"] for r in fetch({}, LATER)["incomingReviewRequests"]], ["old"])
