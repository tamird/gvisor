"""Discover public requests to review other authors' pull requests."""

from collections.abc import Callable, Iterable
import json
import re

IDENTITY = "id number title url state headRefOid updatedAt createdAt author{login __typename} repository{nameWithOwner isPrivate}"
MESSAGE = "id url body createdAt updatedAt author{login __typename}"
PAGING = "pageInfo{hasPreviousPage startCursor}"
THREAD = "id isResolved comments(last:50){" + PAGING + " nodes{" + MESSAGE + "}}"
FIELDS = {
    "comment": ("comments", 100, MESSAGE),
    "review": ("reviews", 100, MESSAGE + " state submittedAt"),
    "inline": ("reviewThreads", 35, THREAD),
}
# Re-scan review conversations even when a repository's issue-search index
# omits review bodies/inline comments. Creation order keeps a backfill stable
# when an old PR gets a new message.
REPOSITORY = "google/gvisor"
BACKFILL_BATCH = 10
DETAIL_BATCH = 8


def prose(text: str | None) -> str:
    text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*", "", text or "")
    text = re.sub(r"(?m)^\s*>[^\n]*", "", text)
    return re.sub(r"`[^`\n]*`", "", text)


def addressed(text: str | None, owner: str) -> bool:
    """Require both a direct addressee and a request to examine the change."""
    text = re.sub(r"https?://[^\s)]+", "", prose(text))
    tag = r"(?<![\w-])@" + re.escape(owner) + r"(?![\w-]|['’]s\b)"
    ask = (
        r"(?:ptal\b|(?:could|would|can|will)\s+you\s+(?:(?:please|kindly|also)\s+)*"
        r"(?:re-?review\b|review\b|take\s+(?:another\s+|a\s+)?look\b|have\s+a\s+look\b|"
        r"look\s+(?:at|over)\b)|(?:can|could)\s+(?:we|I)\s+get\s+(?:another\s+|a\s+)?review\s+from\s+you\b|(?:please\s+|kindly\s+)(?:re-?review\b|review\b|"
        r"take\s+(?:another\s+|a\s+)?look\b)|(?:another\s+)?review\s+requested\b|"
        r"for\s+(?:a\s+|another\s+)?review\b|would\s+(?:appreciate|love)\s+your\s+(?:review|feedback)\b)"
    )
    # Address lists may contain other reviewers. Never cross a different
    # sentence, a quote or an intervening addressee such as “cc @tamird”.
    direct = tag + r"(?:[,!:;\s-]+@[\w-]+)*[,!:;\s-]*" + ask
    end = (
        r"\b(?:ptal|please\s+(?:re-?review|review)(?:\s+this\s+(?:pr|change))?|(?:asking|requesting)\s+(?:a\s+)?review\s+from)[,:!\s-]+"
        + tag
    )
    return bool(re.search(direct, text, re.I) or re.search(end, text, re.I))


def linked_target(body: str | None, owner: str) -> int | None:
    """An addressed request for one explicit PR in the same paragraph."""
    link = (r"(?<![\w./#-])(?:https://github\.com/([\w.-]+/[\w.-]+)/pull/|(?:([\w.-]+/[\w.-]+))?#)"
            r"([1-9][0-9]{0,9})\b")
    text = prose(body)
    references = re.findall(link, text)
    if len(references) != 1:
        return None
    full_repo, short_repo, digits = references[0]
    number = int(digits)
    if (full_repo or short_repo or REPOSITORY).lower() != REPOSITORY or number > 2**31 - 1:
        return None
    for paragraph in re.split(r"\n\s*\n", re.sub(link, "LINKED_PR", text)):
        if {login.lower() for login in re.findall(r"(?<![\w-])@([\w-]+)", paragraph)} != {owner.lower()}:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip())
        for i, sentence in enumerate(sentences):
            if "LINKED_PR" not in sentence:
                continue
            # A direct address can introduce the link, with "Please take a
            # look" immediately following it, as on gVisor #17209.
            if addressed(sentence, owner) or (
                re.match(r"@" + re.escape(owner) + r"\b", sentence, re.I) and i + 1 < len(sentences)
                and addressed(f"@{owner} {sentences[i + 1]}", owner)
            ):
                return number
    return None


def connection(kind: str, before: str | None = None) -> str:
    name, count, fields = FIELDS[kind]
    cursor = ",before:" + json.dumps(before) if before else ""
    return f"{name}(last:{count}{cursor}){{{PAGING} nodes{{{fields}}}}}"


def discover(
    previous: dict,
    rows: Iterable[dict],
    owner: str,
    query: Callable[[str], dict],
    now: str,
    requests_left: int,
) -> tuple[list[dict], dict]:
    """Read outstanding gVisor requests and resume bounded conversation scans."""
    old = previous.get("incomingReviewScan", {})
    if old.get("repo") != REPOSITORY:
        old = {}
    state = {
        k: dict(old.get(k, {}))
        for k in (
            "cursors",
            "seen",
            "activeIds",
            "reviewedThrough",
            "threadReplies",
            "requestThreads",
        )
    }
    state["repo"] = REPOSITORY
    queue = dict(old.get("queue", {}))
    pages = list(old.get("pages", []))
    pending = {
        (r["repo"], r["number"], r["kind"], r["id"]): dict(r)
        for r in previous.get("incomingReviewRequests", [])
        if r["repo"] == REPOSITORY and "sourceURL" not in r
    }
    linked = {key: dict(value) for key, value in old.get("linkedComments", {}).items()}
    owner_lower = owner.lower()

    def persist():
        state.update(queue=queue, pages=pages, linkedComments=linked)
        records = [*pending.values(), *(r for r in linked.values() if r.get("head")
                                        and r["updatedAt"] > state["reviewedThrough"].get(str(r["number"]), ""))]
        return sorted(records, key=lambda r: (r["updatedAt"], r["url"], r["id"])), state

    def intake(meta, items, timestamp, complete=(), formal=None):
        repo, num = meta.get("repo", REPOSITORY), meta["number"]
        if (
            repo != REPOSITORY
            or meta.get("url") != f"https://github.com/{REPOSITORY}/pull/{num}"
            or not meta.get("authorLogin")
        ):
            return
        prkey = str(num)
        outstanding = {k for k in pending if k[:2] == (repo, num)}
        if meta["authorLogin"].lower() == owner_lower:
            comments = {m["id"]: m for m in items if m["kind"] == "comment"}
            active_links = set()
            for key, item in comments.items():
                author = item.get("author") or {}
                if (author.get("__typename") != "User" or author["login"].lower() == owner_lower
                        or not re.fullmatch(re.escape(meta["url"]) + r"#issuecomment-[0-9]+", item["url"])):
                    continue
                target = linked_target(item["body"], owner)
                stamp = item.get("updatedAt") or item["createdAt"]
                if target and target != num and stamp > state["reviewedThrough"].get(str(target), ""):
                    active_links.add(key)
                    request = dict(number=target, updatedAt=stamp, author=author["login"], sourceURL=item["url"])
                    if any(linked.get(key, {}).get(k) != v for k, v in request.items()):
                        linked[key] = request
            for key, request in list(linked.items()):
                if (request["sourceURL"].split("#")[0] == meta["url"] and key not in active_links
                        and (key in comments or "comment" in complete)):
                    del linked[key]
        if meta.get("status") not in ("open", "draft") or meta["authorLogin"].lower() == owner_lower:
            for k in outstanding:
                pending.pop(k)
            for key in [key for key, request in linked.items() if request["number"] == num]:
                del linked[key]
            return
        active = set()
        seen_ids = {(m["kind"], m["id"]) for m in items}
        last = state["reviewedThrough"].get(prkey, "")
        for item in items:
            if (
                (item.get("author") or {}).get("login", "").lower() == owner_lower
                and item["kind"] == "review"
                and item.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
                and item.get("submittedAt")
            ):
                last = max(last, item["submittedAt"])
        if last:
            state["reviewedThrough"][prkey] = last
        if formal is not None:
            for req in formal:
                reviewer = req.get("requestedReviewer") or {}
                if reviewer.get("__typename") != "User" or reviewer.get("login", "").lower() != owner_lower:
                    continue
                k = (repo, num, "formal", req["id"])
                stamp = pending.get(k, {}).get("updatedAt", timestamp)
                active.add(k)
                pending[k] = {
                    "repo": repo,
                    "number": num,
                    "title": meta["title"],
                    "prURL": meta["url"],
                    "head": meta["head"],
                    "author": None,
                    "kind": "formal",
                    "id": req["id"],
                    "url": meta["url"],
                    "updatedAt": stamp,
                }
        for item in items:
            if item["kind"] == "inline" and (item.get("author") or {}).get("login", "").lower() == owner_lower:
                t = item["threadId"]
                state["threadReplies"][t] = max(state["threadReplies"].get(t, ""), item["createdAt"])
        for item in items:
            kind = item["kind"]
            author = item.get("author") or {}
            stamp = item.get("updatedAt") or item.get("createdAt")
            if (
                author.get("__typename") != "User"
                or author.get("login", "").lower() == owner_lower
                or not stamp
                or stamp <= last
                or item.get("isResolved")
                or item.get("state") in ("DISMISSED", "PENDING")
                or state["threadReplies"].get(item.get("threadId"), "") >= stamp
                or not addressed(item.get("body"), owner)
            ):
                continue
            k = (repo, num, kind, item["id"])
            active.add(k)
            if kind == "inline":
                state["requestThreads"][item["id"]] = item["threadId"]
            pending[k] = {
                "repo": repo,
                "number": num,
                "title": meta["title"],
                "prURL": meta["url"],
                "head": meta["head"],
                "author": author["login"],
                "kind": kind,
                "id": item["id"],
                "url": item["url"],
                "updatedAt": stamp,
            }
        for k in outstanding - active:
            if (
                k[2] in complete
                or k[2:] in seen_ids
                or (k[2] != "formal" and pending[k]["updatedAt"] <= last)
                or state["threadReplies"].get(state["requestThreads"].get(k[3]), "") >= pending[k]["updatedAt"]
            ):
                pending.pop(k, None)
            elif k in pending:
                pending[k].update(title=meta["title"], head=meta["head"])

    # These public records are already fetched by the normal collector. Use
    # them now; the separate PR scan supplies descriptions and formal IDs.
    for pr in rows:
        if not pr.get("title"):
            continue
        intake(
            pr,
            pr.get("feedback", {}).get("items", []),
            now,
            ("comment", "review", "inline") if pr.get("feedback", {}).get("complete") else (),
        )
    if requests_left < 2:
        return persist()

    def after(stream: str) -> str:
        cursor = state["cursors"].get(stream)
        return f",after:{json.dumps(cursor)}" if cursor else ""

    def search(stream: str, term: str) -> str:
        exclude = f" -author:{owner}" if stream == "formal" else ""
        spec = f"repo:{REPOSITORY} is:pr is:open{exclude} {term} sort:updated-desc"
        return f"{stream}:search(type:ISSUE,first:24,query:{json.dumps(spec)}{after(stream)}){{{index_fields}}}"

    index_fields = "pageInfo{hasNextPage endCursor} nodes{... on PullRequest{" + IDENTITY + "}}"
    order = "orderBy:{field:%s,direction:DESC}"
    latest = f"latest:pullRequests(states:OPEN,first:8,{order % 'UPDATED_AT'}){{{index_fields}}}"
    backfill = f"backfill:pullRequests(states:OPEN,first:{BACKFILL_BATCH},{order % 'CREATED_AT'}{after('backfill')}){{{index_fields}}}"
    numbers = sorted({r["number"] for r in linked.values()})
    last = old.get("linkedAfter", 0)
    group = ([n for n in numbers if n > last] + [n for n in numbers if n <= last])[:DETAIL_BATCH]
    reviews = ('reviews(author:' + json.dumps(owner)
               + ',states:[APPROVED,CHANGES_REQUESTED,COMMENTED,DISMISSED],last:1){nodes{submittedAt}}')
    # resource() returns null for issues and nonexistent PR URLs. Unlike
    # pullRequest(number:), those inputs do not fail the shared index query.
    linked_fields = " ".join(
        f'p{n}:resource(url:"https://github.com/{REPOSITORY}/pull/{n}"){{... on PullRequest{{{IDENTITY} {reviews}}}}}'
        for n in group)
    index = query(
        "{"
        + search("formal", "review-requested:" + owner)
        + " "
        + search("mentioned", "mentions:" + owner)
        + ' repository(owner:"google",name:"gvisor"){'
        + latest
        + " "
        + backfill
        + "} "
        + linked_fields
        + "}"
    )
    if group:
        state["linkedAfter"] = group[-1]
    for key, request in list(linked.items()):
        num = request["number"]
        if num not in group:
            continue
        raw = index[f"p{num}"]
        if (not raw or raw["state"] != "OPEN" or raw["repository"]["isPrivate"]
                or raw["url"] != f"https://github.com/{REPOSITORY}/pull/{num}"
                or raw["repository"]["nameWithOwner"] != REPOSITORY or raw["number"] != num
                or (raw.get("author") or {}).get("__typename") != "User" or raw["author"]["login"].lower() == owner_lower):
            del linked[key]
            continue
        last_review = max((r["submittedAt"] for r in raw["reviews"]["nodes"] if r["submittedAt"]), default="")
        if last_review:
            state["reviewedThrough"][str(num)] = max(last_review, state["reviewedThrough"].get(str(num), ""))
        if request["updatedAt"] <= state["reviewedThrough"].get(str(num), ""):
            del linked[key]
            continue
        request.update(repo=REPOSITORY, title=raw["title"], prURL=raw["url"], url=raw["url"],
                       head=raw["headRefOid"], kind="comment", id=key)
    candidates = {}
    urgent = set()
    for stream in ("formal", "mentioned", "latest", "backfill"):
        page = index[stream] if stream in ("formal", "mentioned") else index["repository"][stream]
        if stream != "latest":
            info = page["pageInfo"]
            state["cursors"][stream] = info["endCursor"] if info["hasNextPage"] else None
        for raw in page["nodes"]:
            if (
                not raw
                or raw["repository"]["isPrivate"]
                or raw["repository"]["nameWithOwner"] != REPOSITORY
                or (stream != "mentioned" and (raw.get("author") or {}).get("login", "").lower() == owner_lower)
            ):
                continue
            pk = str(raw["number"])
            if stream == "formal":
                urgent.add(pk)
            if state["seen"].get(pk) != raw["updatedAt"] or stream in ("formal", "backfill"):
                candidates[pk] = [raw["id"], raw["updatedAt"]]
    # Existing requests are checked even when a PR vanishes from open search.
    for row in list(pending.values()):
        pk = str(row["number"])
        if pk in old.get("activeIds", {}):
            candidates.setdefault(pk, [old["activeIds"][pk], None])
    for pk, val in candidates.items():
        queue[pk] = val

    def put_page(prid, pk, who, kind, before):
        ident = [prid, pk, who, kind, before]
        if ident not in pages:
            pages.append(ident)

    def take(pk, kind, conn, who, prid, resolved=False):
        items = []
        if conn["pageInfo"]["hasPreviousPage"]:
            put_page(prid, pk, who, kind, conn["pageInfo"]["startCursor"])
        if kind == "inline":
            for t in conn["nodes"]:
                items += take(pk, "thread", t["comments"], t["id"], prid, t["isResolved"])
        else:
            for m in conn["nodes"]:
                items.append(
                    dict(m, kind="inline", threadId=who, isResolved=resolved)
                    if kind == "thread"
                    else dict(m, kind=kind)
                )
        return items

    def metadata(raw):
        return {
            "repo": raw["repository"]["nameWithOwner"],
            "number": raw["number"],
            "url": raw["url"],
            "title": raw["title"],
            "head": raw["headRefOid"],
            "authorLogin": (raw.get("author") or {}).get("login"),
            "status": raw["state"].lower(),
        }

    queries = min(5, requests_left - 1)
    for _ in range(max(1, queries - 1)):
        # A fresh formal request should not wait behind the historical scan.
        front = [pk for pk in queue if pk in urgent][: DETAIL_BATCH // 2]
        group = (front + [pk for pk in queue if pk not in front])[:DETAIL_BATCH]
        if not group:
            break
        ids = [queue[pk][0] for pk in group]
        fields = " ".join(connection(k) for k in FIELDS)
        formal = "reviewRequests(first:100){pageInfo{hasNextPage} nodes{id requestedReviewer{__typename ... on User{login}}}}"
        raws = query(
            "{nodes(ids:"
            + json.dumps(ids)
            + "){... on PullRequest{"
            + IDENTITY
            + " body lastEditedAt "
            + formal
            + " "
            + fields
            + "}}}"
        )["nodes"]
        for pk, raw in zip(group, raws, strict=True):
            queue.pop(pk)
            if not raw:
                for ident in list(pending):
                    if str(ident[1]) == pk:
                        del pending[ident]
                continue
            m = metadata(raw)
            if (m["repo"] != REPOSITORY or str(m["number"]) != pk) or raw["repository"]["isPrivate"]:
                continue
            state["seen"][pk] = raw["updatedAt"]
            state["activeIds"][pk] = raw["id"]
            items = [
                {
                    "kind": "description",
                    "id": raw["id"],
                    "url": raw["url"],
                    "body": raw["body"],
                    "author": raw["author"],
                    "createdAt": raw["createdAt"],
                    "updatedAt": raw["lastEditedAt"] or raw["createdAt"],
                }
            ]
            complete = {"description"}
            if not raw["reviewRequests"]["pageInfo"]["hasNextPage"]:
                complete.add("formal")
            for k, (f, _, _) in FIELDS.items():
                c = raw[f]
                items += take(pk, k, c, raw["id"], raw["id"])
                if not c["pageInfo"]["hasPreviousPage"] and (
                    k != "inline" or not any(t["comments"]["pageInfo"]["hasPreviousPage"] for t in c["nodes"])
                ):
                    complete.add(k)
            intake(m, items, now, complete, raw["reviewRequests"]["nodes"])
    # Continue old comment, review and thread pages instead of declaring an
    # incomplete discussion handled. Always bind them to the live PR.
    if pages and queries >= 2:
        jobs = pages[:6]
        del pages[: len(jobs)]
        parts = []
        for i, (prid, pk, who, kind, before) in enumerate(jobs):
            parts.append(f"p{i}:node(id:{json.dumps(prid)}){{... on PullRequest{{{IDENTITY}}}}}")
            if kind == "thread":
                q = f"isResolved comments(last:100,before:{json.dumps(before)}){{{PAGING} nodes{{{MESSAGE}}}}}"
                typ = "PullRequestReviewThread"
            else:
                q = connection(kind, before)
                typ = "PullRequest"
            parts.append(f"c{i}:node(id:{json.dumps(who)}){{... on {typ}{{{q}}}}}")
        result = query("{" + " ".join(parts) + "}")
        for i, (prid, pk, who, kind, before) in enumerate(jobs):
            raw = result[f"p{i}"]
            data = result[f"c{i}"]
            if not raw or not data or raw["repository"]["isPrivate"]:
                continue
            m = metadata(raw)
            if m["repo"] != REPOSITORY or str(m["number"]) != pk:
                continue
            c = data["comments"] if kind == "thread" else data[FIELDS[kind][0]]
            items = take(pk, kind, c, who, prid, kind == "thread" and data["isResolved"])
            intake(m, items, now)
    active = {str(num) for _, num, _, _ in pending}
    active_comments = {i for _, _, _, i in pending}
    state["requestThreads"] = {i: t for i, t in state["requestThreads"].items() if i in active_comments}
    state["activeIds"] = {pk: v for pk, v in state["activeIds"].items() if pk in active}
    # Keep discovery cursors and unprocessed work across refreshes. The
    # seen cache only avoids rereading search hits; evicting it is harmless.
    if len(state["seen"]) > 4000:
        state["seen"] = dict(sorted(state["seen"].items(), key=lambda x: x[1])[-3000:])
    return persist()
