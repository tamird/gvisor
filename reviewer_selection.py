"""Choose gVisor reviewers from their recent activity and technical experience."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction

REPO = "google/gvisor"
ACTIVE_WINDOW = timedelta(days=30)
GOVERNANCE = "https://github.com/google/gvisor/blob/master/governance/maintainers.yaml"
MENTION = re.compile(r"(?<![\w@])@([\w-]+)")
ASK = re.compile(r"\b(review|re-review|approve|approval|ptal|take a look)\b", re.I)
GENERIC_REVIEW = re.compile(r"(?:lgtm|thanks|approved|looks good(?: to me)?)[.! ]*", re.I)


def reviewer_governance(areas_text: str, maintainers_text: str) -> tuple[set[str], dict[str, set[str]]]:
    """Read active status and specialization paths from gVisor governance."""
    import yaml

    active = set()
    owners = defaultdict(set)
    for maintainer in yaml.safe_load(maintainers_text)["maintainers"]:
        if maintainer["status"] == "ACTIVE":
            active.add(maintainer["github"].lower())
            for name in maintainer.get("areas", []):
                owners[name].add(maintainer["github"].lower())
    paths = defaultdict(set)
    for area in yaml.safe_load(areas_text)["areas"]:
        for path in area["paths"]:
            paths[path.strip("/")].update(owners[area["name"]])
    return active, dict(paths)


def _time(value: datetime | str) -> datetime:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _login(actor: dict | None) -> str:
    if not actor or actor.get("__typename", "User") != "User":
        return ""
    login = (actor.get("login") or "").lower()
    return "" if login.endswith("[bot]") or login in {"github-actions", "copybara-service", "copilot"} else login


@dataclass
class Experience:
    last_review: datetime | None = None
    # (path, PR number) -> URL of the review or authored change.
    sources: dict[tuple[str, int], str] = field(default_factory=dict)
    outstanding: int = 0
    asks: Counter[str] = field(default_factory=Counter)
    answers: Counter[str] = field(default_factory=Counter)


@dataclass
class History:
    people: dict[str, Experience]
    specialties: dict[str, set[str]]
    owner: str


def recent_reviewer_work(graphql, active_logins, previous: dict, now: datetime | str) -> dict:
    """Sample reviews of other gVisor authors once per day, using one API request."""
    now = _time(now)
    logins = sorted(active_logins)
    checked = previous.get("checkedAt")
    if checked and _time(checked).date() == now.date() and previous.get("logins") == logins:
        return previous
    cutoff = now - ACTIVE_WINDOW
    searches = []
    for i, login in enumerate(logins):
        query = f"repo:{REPO} is:pr reviewed-by:{login} updated:>={cutoff.date()} sort:updated-desc"
        searches.append(f'r{i}:search(type:ISSUE, first:6, query:{json.dumps(query)}) '
                        '{nodes{... on PullRequest {number url author{login} files(first:30){nodes{path}} '
                        f'reviews(last:4,author:{json.dumps(login)}) '
                        '{nodes{url state bodyText submittedAt comments(first:3){nodes{path bodyText url}}}} }}}')
    observations = []
    if searches:
        data = graphql("query {" + " ".join(searches) + "}")
        for i, login in enumerate(logins):
            for pr in data[f"r{i}"]["nodes"]:
                if (pr.get("author") or {}).get("login", "").lower() == login:
                    continue
                for review in pr["reviews"]["nodes"]:
                    at = review.get("submittedAt")
                    if not at or _time(at) < cutoff or review["state"] in {"DISMISSED", "PENDING"}:
                        continue
                    paths = {}
                    if review["bodyText"].strip() and not GENERIC_REVIEW.fullmatch(review["bodyText"].strip()):
                        paths.update({f["path"]: review["url"] for f in pr["files"]["nodes"]})
                    for comment in review["comments"]["nodes"]:
                        if comment["bodyText"].strip() and not GENERIC_REVIEW.fullmatch(comment["bodyText"].strip()):
                            paths[comment["path"]] = comment["url"]
                    observations.append({"login": login, "number": pr["number"], "at": at,
                                         "url": review["url"], "paths": paths})
    return {"checkedAt": now.isoformat(), "logins": logins, "observations": observations}


def reviewer_history(prs: list[dict], owner: str, *, maintainers: set[str] | None = None,
                     specialties: dict[str, set[str]] | None = None, observations=()) -> History:
    """Build a shared index once per plan. All input data is public."""
    owner = owner.lower()
    records = {p["number"]: p for p in prs if p.get("repo") == REPO}.values()
    if maintainers is None:
        maintainers = {name for p in records for name in p.get("maintainerReviewers", [])}
    people = {login.lower(): Experience() for login in maintainers if login.lower() != owner}
    for pr in records:
        number = pr["number"]
        author = (pr.get("authorLogin") or "").lower()
        paths = pr.get("files", [])
        if author in people and pr.get("mergedAt"):
            for path in paths:
                people[author].sources[path, number] = pr["url"]
        if pr.get("status", "").lower() == "open":
            for request in pr.get("reviewRequests", []):
                if (login := _login(request)) in people:
                    people[login].outstanding += 1

        # Attribute each response to the last observed request. An automated
        # native assignment followed by a direct @mention is one comparison,
        # rather than a success for both channels.
        events = []
        for event in pr.get("reviewTimeline", {}).get("items", []):
            if event["kind"] == "request" and (login := _login(event.get("reviewer"))) in people:
                events.append((_time(event["at"]), login, "request"))
        for item in pr.get("feedback", {}).get("items", []):
            at = item.get("submittedAt") or item.get("createdAt")
            if not at:
                continue
            when = _time(at)
            login = _login(item.get("author"))
            body = item.get("body") or ""
            if login == author and ASK.search(body):
                for recipient in set(MENTION.findall(body.lower())) & people.keys():
                    events.append((when, recipient, "comment"))
            if login not in people or login == author or item["kind"] not in {"inline", "review"}:
                continue
            if item.get("state") in {"DISMISSED", "PENDING"}:
                continue
            person = people[login]
            person.last_review = max(person.last_review or when, when)
            events.append((when, login, "answered"))
            if body.strip() and not GENERIC_REVIEW.fullmatch(body.strip()):
                reviewed = [item["path"]] if item.get("path") else paths
                for path in reviewed:
                    person.sources[path, number] = item["url"]

        pending = {}
        for _, login, kind in sorted(events, key=lambda e: (e[0], e[2] == "answered")):
            if kind == "answered":
                if channel := pending.pop(login, None):
                    people[login].answers[channel] += 1
            else:
                people[login].asks[kind] += 1
                pending[login] = kind
    for observed in observations:
        if observed["login"] in people:
            person = people[observed["login"]]
            when = _time(observed["at"])
            person.last_review = max(person.last_review or when, when)
            for path, url in observed["paths"].items():
                person.sources[path, observed["number"]] = url
    return History(people, specialties or {}, owner)


def _home(path: str) -> str:
    return path.rpartition("/")[0]


def _expertise(pr: dict, login: str, person: Experience, specialties: dict) -> tuple[int, int, str, str, str]:
    paths = set(pr.get("files", []))
    # Root module and checksum edits accompany otherwise unrelated changes.
    paths = {p for p in paths if "/" in p} or paths
    dirs = {_home(p) for p in paths if _home(p)}
    best = (0, 0, "", "", "")
    for (path, number), url in person.sources.items():
        if number == pr["number"] and path in paths:
            return (5, 0, url, "You reviewed this part of the PR earlier.", path)
        if path in paths:
            best = max(best, (4, 0, url, f"You worked on this file in #{number}.", path))
        elif _home(path) in dirs:
            directory = _home(path) + "/"
            best = max(best, (3, 0, url, f"You worked in this directory in #{number}.", directory))
    for prefix, owners in specialties.items():
        if login not in owners:
            continue
        for path in paths:
            if path == prefix or path.startswith(prefix + "/"):
                best = max(best, (2, prefix.count("/"), GOVERNANCE,
                                  f"The project lists you as a reviewer for {prefix}/.", path))
    return best


def get_reviewer_candidates(pr: dict, history: History, previous: set[str], requested: set[str],
                            now: datetime | str, *, planned: dict[str, int] | None = None) -> list[dict]:
    """Rank candidates and choose a single GitHub channel for each person."""
    now = _time(now)
    excluded = {name.lower() for name in previous}
    excluded.update({history.owner, (pr.get("authorLogin") or "").lower()})
    requested = {name.lower() for name in requested}
    candidates = []
    for login, person in history.people.items():
        if login in excluded or not person.last_review or now - person.last_review > ACTIVE_WINDOW:
            continue
        level, depth, evidence, reason, scope = _expertise(pr, login, person, history.specialties)
        if not level:
            continue
        # Prefer the channel with a demonstrated response. A pending native
        # request can't notify the same recipient a second time.
        def rate(channel: str) -> Fraction:
            return Fraction(person.answers[channel], person.asks[channel] or 1)
        channel = "comment" if login in requested or rate("comment") > rate("request") else "request"
        load = person.outstanding + (planned or {}).get(login, 0)
        choice = {"login": login, "channel": channel, "evidence": evidence, "reason": reason, "scope": scope,
                  "pending": load, "successfulResponses": dict(person.answers)}
        tie = hashlib.sha256(f'{pr["number"]}/{login}'.encode()).digest()
        candidates.append(((-level, -depth, load, -person.last_review.timestamp(), tie), choice))
    return [choice for _, choice in sorted(candidates)]
