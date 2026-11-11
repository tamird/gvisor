#!/usr/bin/env python3
"""Plan or retire old gVisor automation branches from the local clone and its fork."""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

FORK = "tamird/gvisor"
UPSTREAM = "google/gvisor"
HEADS = "refs/heads/"
DISPOSABLE = ("qualification/", "diagnostics/")
MANAGED = DISPOSABLE + ("proposal/", "research/", "review/", "integration/",
                        "rebase/", "tracker/", "workmap/", "delivery/")
PAYLOAD = (".github/workflows/", ".github/qualification/", "test/rbe/")
ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")
FORK_MIRROR = "identical fork branch retained"
MERGED_SOURCE = "merged PR at exact upstream-reachable tip"
COMPLETED_CI = "retired CI payload with a completed run at the exact tip"
LOCAL_CI = "local copy of retiring completed CI branch"
PUBLISHED_BRANCH = "reachable from a retained public branch"
REDUNDANT_CI = "old CI tip reachable from a surviving fork branch"
CI_IDLE = dt.timedelta(days=1)
OID = re.compile(r"[0-9a-f]{40}")


def command(*args: str, data: bytes = b"") -> bytes:
    return subprocess.run(args, input=data, stdout=subprocess.PIPE, check=True).stdout


def git(*args: str, data: bytes = b"") -> bytes:
    return command("git", *args, data=data)


def api(path: str, query: str, *, pages: bool = False) -> list:
    args = ["--paginate"] if pages else []
    output = command("gh", "api", "--hostname", "github.com", *args, path, "--jq", f"({query}) | @json")
    return [row for page in output.splitlines() for row in json.loads(page)]


def lines(items) -> bytes:
    return "".join(item + "\n" for item in items).encode()


def chunks(items, size=100):
    for pos in range(0, len(items), size):
        yield items[pos:pos + size]


def remote_refs(repo: str, pattern: str = HEADS + "*") -> dict[str, str]:
    return {ref.removeprefix(HEADS): oid for oid, ref in
            (row.split("\t") for row in git("ls-remote", "--heads", repo, pattern).decode().splitlines())}


def check_origin() -> None:
    allowed = {f"git@github.com:{FORK}", f"https://github.com/{FORK}", f"ssh://git@github.com/{FORK}"}
    for flags in ([], ["--push"]):
        urls = git("remote", "get-url", *flags, "--all", "origin").decode().splitlines()
        if len(urls) != 1 or urls[0].removesuffix(".git") not in allowed:
            raise ValueError(f"origin must have a single fetch and push URL for {FORK}: {urls}")


def checked_out() -> tuple[set[str], set[str]]:
    fields = git("worktree", "list", "--porcelain", "-z").decode().split("\0")
    return ({field.removeprefix("branch " + HEADS) for field in fields if field.startswith("branch " + HEADS)},
            {field[5:] for field in fields if field.startswith("HEAD ")})


def live_protections() -> tuple[set[str], set[str]]:
    names, tips = checked_out()
    names.update(api(f"repos/{FORK}/branches?protected=true&per_page=100", "[.[] | .name]", pages=True))
    for repo in (FORK, UPSTREAM):
        for name, oid in api(f"repos/{repo}/pulls?state=open&per_page=100",
                             f'[.[] | (.head,.base) | select(.repo.full_name=="{FORK}") | [.ref,.sha]]', pages=True):
            names.add(name)
            tips.add(oid)
    for state in ACTIVE:
        for attempt in range(3):
            observed = api(f"repos/{FORK}/actions/runs?status={state}&per_page=100",
                           "[[.total_count, [.workflow_runs[] | [.id,.head_branch,.head_sha]]]]", pages=True)
            count = observed[0][0]
            rows = [row for _, page in observed for row in page]
            for _, name, oid in rows:
                names.add(name)
                tips.add(oid)
            if count == len({row[0] for row in rows}):
                break
            if attempt == 2:
                raise ValueError(f"incomplete {state} Actions inventory ({count} expected)")
            time.sleep(1)
    return names, tips


def live_merged_sources(rows: list[dict], fork: dict[str, str], local: dict[str, str], upstream: str) -> dict[str, int]:
    # A Copybara import may have landed an earlier revision. Confirm the
    # actual source PR and fork ref before consuming its historical metadata.
    ancestors = set(git("rev-list", upstream).decode().splitlines())
    candidates = {row["ref"]: row for row in rows if row.get("repo", UPSTREAM) == UPSTREAM
                  and row.get("headRepo") == FORK and row["status"] == "merged"
                  and fork.get(row["ref"]) == row["head"] and row["head"] in ancestors
                  and local.get(row["ref"], row["head"]) == row["head"]}
    confirmed = {}
    for batch in chunks(sorted(candidates), 40):
        fields = []
        for i, name in enumerate(batch):
            ref = json.dumps(HEADS + name)
            fields.append(f"b{i}:ref(qualifiedName:{ref})" + """{target{oid}
                associatedPullRequests(first:10,states:[OPEN,MERGED]){pageInfo{hasNextPage}
                    nodes{number state headRefName headRefOid headRepository{nameWithOwner} baseRepository{nameWithOwner}}}}""")
        query = 'query{repository(owner:"tamird",name:"gvisor"){' + " ".join(fields) + "}}"
        result = json.loads(command("gh", "api", "--hostname", "github.com", "graphql", "-f", "query=" + query))
        for i, name in enumerate(batch):
            row, ref = candidates[name], result["data"]["repository"][f"b{i}"]
            if not ref or ref["target"]["oid"] != row["head"]:
                continue
            prs = ref["associatedPullRequests"]
            if prs["pageInfo"]["hasNextPage"] or any(pr["state"] == "OPEN" for pr in prs["nodes"]):
                continue
            if any(pr["number"] == row["number"] and pr["state"] == "MERGED" and pr["headRefName"] == name
                   and pr["headRefOid"] == row["head"] and (pr["headRepository"] or {}).get("nameWithOwner") == FORK
                   and (pr["baseRepository"] or {}).get("nameWithOwner") == UPSTREAM for pr in prs["nodes"]):
                confirmed[name] = row["number"]
    return confirmed


def tracker_metadata(registry: dict, snapshot: dict, merged: dict[str, int], names: set[str], tips: set[str]) -> None:
    handoffs = {key: value for key, value in snapshot.items() if key.endswith("Handoff")
                or key in {"incomingReviewRequests", "ciFlakes", "reviewOutreach"}}
    # Branch ids in edges link to tracker cards. They do not open a Git ref.
    active = [{k: v for k, v in registry.items() if k not in {"nodes", "edges"}}, handoffs,
              [{k: v for k, v in edge.items() if k not in {"from", "to"}} for edge in registry.get("edges", [])]]
    for node in registry["nodes"]:
        name = node.get("ref", "")
        number = merged.get(name)
        pr_url = f"https://github.com/{UPSTREAM}/pull/{number}"
        links = [link.get("url") if isinstance(link, dict) else link for link in node.get("evidence", [])]
        linked = pr_url == node.get("url") or pr_url in links
        source = node["type"] == "pr" and node.get("repo", UPSTREAM) == UPSTREAM and node.get("number") == number
        published = node["type"] == "branch" and node.get("repo") == FORK and (
            node.get("inactive") or linked and node.get("status") in {"Published", "Delivered"})
        pending = any(d and d.get("status") == "pending"
                      for d in [node.get("contributorReview"), *node.get("additionalReviews", [])])
        terminal = number and (source or published) and not pending
        if terminal:
            active.extend([(node.get("followup") or {}).get("nextAction"), node.get("refinement")])
        else:
            active.append(node)
            if name:
                names.add(name)
        if node["id"].startswith("branch:"):
            branch_id = node["id"].removeprefix("branch:")
            if not terminal or branch_id != name:
                names.add(branch_id)
    for row in snapshot["prs"]:
        if row["status"] in {"open", "draft"} and row.get("headRepo") == FORK:
            names.add(row["ref"])
            tips.add(row["head"])
        if row["status"] == "merged" and row.get("headRepo") == FORK and row["ref"] not in merged:
            names.add(row["ref"])
    active.extend(row.get("feedback") for row in [*snapshot["prs"], *snapshot.get("reviewPRs", [])]
                  if row["status"] in {"open", "draft"})
    active.extend(row for row in snapshot.get("issues", {}).values() if row.get("githubState") == "open")

    blob = unquote(json.dumps([registry, handoffs]))
    managed_refs = re.findall(r"\b((?:qualification|diagnostics|research|review|proposal|integration|tracker|workmap|rebase|delivery)/[\w./-]+)", blob)
    names.update(set(managed_refs) - merged.keys())
    # An active command such as `git switch a-merged-branch` still needs
    # the name even when the commit itself is safely on upstream master.
    all_text = unquote(json.dumps([registry, snapshot]))
    commands = re.findall(r'\bgit[ \t]+[^"\n`]*', all_text.replace(r'\"', "'").replace(r"\n", "\n"))
    active_text = unquote(json.dumps(active)) + "\n" + "\n".join(commands)
    for name in merged:
        if re.search(r"(?:^|[^\w.-]|\.\.)" + re.escape(name) + r"(?=$|[^\w./-]|\.\.)", active_text):
            names.add(name)
    # All links and previous commit citations survive, including those on
    # delivered cards and in older GitHub conversations.
    names.update(linked_branches(all_text))
    tips.update(re.findall(r"(?<![A-Za-z0-9])[a-f0-9]{7,40}(?![A-Za-z0-9])", all_text))


def tracker_protections(fork: dict[str, str], local: dict[str, str], upstream: str,
                        names: set[str], tips: set[str]) -> set[tuple[str, str]]:
    pages = fork["gh-pages"]

    def document(filename: str) -> dict:
        # Prefer the immutable local objects; gh-pages may advance between
        # fetches. Both fallback reads use the same observed fork commit.
        obj = pages + ":" + filename
        if subprocess.run(["git", "cat-file", "-e", obj], stderr=subprocess.DEVNULL).returncode == 0:
            return json.loads(git("show", obj))
        return json.loads(command("gh", "api", "--hostname", "github.com", "-H", "Accept: application/vnd.github.raw+json",
                                  f"repos/{FORK}/contents/{filename}?ref={pages}"))
    registry, snapshot = document("registry.json"), document("github-status.json")
    if (not registry["nodes"] or registry["meta"]["repo"] != UPSTREAM
            or registry["meta"]["updatedAt"] != snapshot["registryDate"]):
        raise ValueError("unexpected or unpaired work map registry and snapshot")
    merged = live_merged_sources(snapshot["prs"], fork, local, upstream)
    tracker_metadata(registry, snapshot, merged, names, tips)
    return {(name, fork[name]) for name in merged}


def linked_branches(text: str) -> set[str]:
    names = set(re.findall(r'github\.com/tamird/gvisor/archive/refs/heads/([^\s"\\<>?#)]+?)\.(?:tar\.gz|zip)(?=[\s"\\<>?#)]|$)', text))
    text = re.sub(r"(?:raw\.githubusercontent\.com/tamird/gvisor/|github\.com/tamird/gvisor/raw/)(?:refs/heads/)?",
                  "github.com/tamird/gvisor/blob/", text)
    for owner, route, target in re.findall(
            r"github\.com/(tamird|google)/gvisor/(tree|blob|commits|compare)/([^\s\"\\<>?#)]+)", text):
        if route == "compare":
            for side in re.split(r"\.{2,3}", target):
                qualifier, sep, ref = side.rpartition(":")
                if (qualifier.split(":")[0] if sep else owner) == "tamird":
                    names.add(ref)
        elif owner == "tamird":
            # A tree or blob URL may append a path to a branch containing slashes.
            parts = target.rstrip("/").split("/")
            names.update("/".join(parts[:end]) for end in range(1, len(parts) + 1))
    return names


def local_activity(refs: dict[str, str], cutoff: dt.datetime) -> set[str]:
    common = Path(os.fsdecode(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()))
    protected = set()
    # update-ref preserves config. Leave any nonstandard configuration or
    # tracking relationship under its original branch name.
    configs = subprocess.run(["git", "config", "--local", "--get-regexp", r"^branch\."], stdout=subprocess.PIPE)
    if configs.returncode not in (0, 1):
        configs.check_returncode()
    for line in configs.stdout.decode().splitlines():
        key, _, value = line.partition(" ")
        name, _, kind = key.removeprefix("branch.").rpartition(".")
        if name in refs and (kind, value) not in {("remote", "origin"), ("merge", HEADS + name)}:
            protected.add(name)
    for name in refs.keys() - protected:
        log = common / "logs" / "refs" / "heads" / name
        # Repacking, restoring a backup, or moving the checkout can touch
        # every log file. Use Git's timestamp on the actual last ref update.
        if not log.is_file():
            protected.add(name)
            continue
        entries = log.read_bytes().splitlines()
        record = entries[-1].partition(b"\t")[0].split() if entries else []
        if (len(record) < 5 or record[1].decode() != refs[name]
                or not record[-2].isdigit() or int(record[-2]) > cutoff.timestamp()):
            protected.add(name)
    return protected


def completed_runs(now: dt.datetime) -> set[tuple[str, str]]:
    # GitHub accepts reruns up to day 30 and permits workflows to last 35
    # days. Include both limits and the idle day, even for an old rerun.
    # The REST filter is by creation, but the idle clock starts at completion.
    completed = set()
    recent = set()
    cutoff = now - CI_IDLE
    for days in range(67):
        day = now.date() - dt.timedelta(days=days)
        pages = api(f"repos/{FORK}/actions/runs?status=completed&created={day}&per_page=100",
                    "[[.total_count, [.workflow_runs[] | [.id,.head_branch,.head_sha,.updated_at,.event,.head_repository.full_name]]]]",
                    pages=True)
        runs = {row[0]: row for _, rows in pages for row in rows}
        if len(runs) != pages[0][0]:
            raise ValueError(f"incomplete completed Actions inventory for {day}")
        for _, name, oid, updated, event, repo in runs.values():
            if repo == FORK:
                if dt.datetime.fromisoformat(updated) > cutoff:
                    recent.add(name)
                if event in {"push", "workflow_dispatch"}:
                    completed.add((name, oid))
    return {(name, oid) for name, oid in completed if name not in recent}


def available(oids: set[str]) -> set[str]:
    if not oids:
        return set()
    return {line.split()[0] for line in git("cat-file", "--batch-check=%(objectname) %(objecttype)",
                                            data=lines(sorted(oids))).decode().splitlines() if line.endswith(" commit")}


def fetch_missing(repo: str, oids: set[str]) -> None:
    missing = oids - available(oids)
    if missing:
        # Publish-time comparisons need the objects but must not change
        # FETCH_HEAD, remote-tracking refs, tags, or the operator's checkout.
        git("fetch", "--no-write-fetch-head", "--refmap=", "--no-tags", "--no-prune",
            "--no-recurse-submodules", "--no-auto-maintenance", repo, *sorted(missing))
        if missing != available(missing):
            raise ValueError(f"the observed base commits are still missing after fetching {repo}")


def public_bases(fork: dict[str, str]) -> str:
    upstream_repo = f"https://github.com/{UPSTREAM}.git"
    upstream = remote_refs(upstream_repo, HEADS + "master")["master"]
    fetch_missing(f"https://github.com/{FORK}.git", {fork["master"], fork["gh-pages"]})
    fetch_missing(upstream_repo, {upstream})
    return upstream


def metadata(oids: set[str]) -> dict[str, tuple[int, str, int]]:
    if not oids:
        return {}
    rows = git("log", "--no-walk=unsorted", "--format=%H%x09%ct%x09%ae%x09%P", "--stdin",
               data=lines(sorted(oids))).decode().splitlines()
    return {oid: (int(stamp), author, len(parents.split())) for oid, stamp, author, parents in
            (row.split("\t") for row in rows)}


def payload_paths(oids: set[str]) -> dict[str, list[str]]:
    if not oids:
        return {}
    output = git("diff-tree", "--stdin", "--no-renames", "-r", "--format=%x1e%H", "--name-only",
                 data=lines(sorted(oids))).decode(errors="replace")
    result = {}
    for part in output.split("\x1e")[1:]:
        oid, *paths = filter(None, part.strip("\n").splitlines())
        if paths and all(path.startswith(PAYLOAD) for path in paths):
            result[oid] = paths
    return result


@dataclass(frozen=True, order=True)
class Candidate:
    scope: str
    name: str
    oid: str
    reason: str


def retained_public_history(fork: dict[str, str], protected: set[str] | None, dropping: set[str]) -> set[str]:
    tips = {oid for name, oid in fork.items()
            if name not in dropping and (protected is None or name in protected or not name.startswith(MANAGED))}
    return set(git("rev-list", "--stdin", data=lines(available(tips))).decode().splitlines())


def redundant_ci_refs(fork: dict[str, str], eligible: set[str], dropping: set[str]) -> set[str]:
    """Retain one fork ref for each maximal CI tip without an outside witness."""
    if not eligible:
        return set()
    by_tip = defaultdict(list)
    for name in eligible:
        by_tip[fork[name]].append(name)
    outside = available({oid for name, oid in fork.items() if name not in eligible and name not in dropping})
    # Subtract history already covered outside this pool. In the remaining
    # graph, a tip appearing as a parent has a descendant in the same pool.
    # Only tips with no descendant need their own surviving branch name.
    roots = list(by_tip) + ["^" + oid for oid in outside]
    seen, parents = set(), set()
    for row in git("rev-list", "--parents", "--stdin", data=lines(roots)).decode().splitlines():
        oid, *previous = row.split()
        seen.add(oid)
        parents.update(previous)
    keep = {min(by_tip[oid]) for oid in by_tip.keys() & (seen - parents)}
    return eligible - keep


def require_public_descendants(items: list[Candidate], fork: dict[str, str], protected: set[str], dropping: set[str]) -> None:
    for reason, names in ((PUBLISHED_BRANCH, protected), (REDUNDANT_CI, None)):
        tips = {item.oid for item in items if item.reason == reason}
        if tips and not tips <= retained_public_history(fork, names, dropping):
            raise ValueError("a candidate lost its retained public descendant; rerun the plan")


def unpublished_locals(items: list[Candidate], surviving: set[str]) -> set[str]:
    # Paired CI refs have their own pre-deletion publication proof. Their
    # archived CI tip can intentionally lose its last public branch.
    local = {item.name: item.oid for item in items if item.scope == "local" and item.reason != LOCAL_CI}
    return local.keys() - published_reflogs(local, surviving)


def evidence_keepers(fork: dict[str, str], dropping: set[str], upstream: str, evidence: set[str]) -> set[str]:
    """Retain a public branch for each citation and each non-CI source commit."""
    if not dropping:
        return set()
    retained = available({oid for name, oid in fork.items() if name not in dropping} | {upstream})
    origins = {}
    for name in sorted(dropping):
        origins.setdefault(fork[name], name)
    # In topological order every descendant is seen before its parents.
    # Carry one originating branch down the graph, with the retained roots
    # subtracted by Git. This finds a witness for all citations in one walk.
    inputs = list(origins) + ["^" + oid for oid in retained]
    graph = [line.split() for line in git("rev-list", "--topo-order", "--parents", "--stdin",
                                          data=lines(inputs)).decode().splitlines()]
    # A workflow-only tip may be the sole surviving branch for a source
    # patch beneath it. Check every commit the entire deletion would orphan.
    payload = payload_paths({oid for oid, *parents in graph if len(parents) == 1})
    lengths = {len(oid) for oid in evidence}
    keep = set()
    for oid, *parents in graph:
        name = origins[oid]
        if oid not in payload or any(oid[:size] in evidence for size in lengths):
            keep.add(name)
        for parent in parents:
            origins.setdefault(parent, name)
    return keep


def published_reflogs(mirrors: dict[str, str], surviving: set[str]) -> set[str]:
    """Keep a local branch if a reset or amend left unpublished history in its log."""
    if not mirrors:
        return set()
    common = Path(os.fsdecode(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()))
    reachable = set(git("rev-list", "--stdin", data=lines(available(surviving))).decode().splitlines())
    published = set()
    for name, head in mirrors.items():
        path = common / "logs" / "refs" / "heads" / name
        if not path.is_file():
            continue
        history = [row.split(b" ", 2)[:2] for row in path.read_bytes().splitlines()]
        if (history and history[-1][-1] == head.encode() and
                all(len(pair) == 2 and all(oid == b"0" * 40 or oid.decode() in reachable for oid in pair)
                    for pair in history)):
            published.add(name)
    return published


def plan(retention: int, *, merged_only: bool = False) -> list[Candidate]:
    check_origin()
    fork = remote_refs("origin")
    upstream = public_bases(fork)
    local = dict(row.split("\t") for row in git("for-each-ref", "--format=%(refname:lstrip=2)%09%(objectname)",
                                                 HEADS).decode().splitlines())
    names, tips = live_protections()
    evidence = set()
    merged_prs = tracker_protections(fork, local, upstream, names, evidence)
    names.update({"master", "main", "gh-pages", "go"})
    for repo in (FORK, UPSTREAM):
        names.update(api(f"repos/{repo}", "[.default_branch]"))
    reserved = {name for name in local.keys() & fork.keys() if local[name] != fork[name]}
    names.update(reserved)
    data = metadata(available(set(local.values()) | set(fork.values())))
    branches = {"local": local, "fork": fork}
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=retention)
    local_recent = local_activity(local, cutoff)
    owner = git("config", "--get", "user.email").decode().strip()
    prefix_lengths = {len(oid) for oid in tips if oid}
    potential = []
    mirrors = {}
    for scope, refs in branches.items():
        for name, oid in refs.items():
            if (name in names or scope == "local" and name in local_recent or oid not in data
                    or any(oid[:length] in tips for length in prefix_lengths)):
                continue
            stamp, author, _ = data[oid]
            if author != owner:
                continue
            if merged_only and ((name, oid) not in merged_prs or stamp > cutoff.timestamp()):
                continue
            if stamp <= cutoff.timestamp():
                if scope == "local" and fork.get(name) == oid:
                    mirrors[name] = oid
                if name.startswith(MANAGED) or (name, oid) in merged_prs:
                    potential.append((scope, name, oid))
            elif scope == "fork" and name.startswith(DISPOSABLE) and stamp <= (now - CI_IDLE).timestamp():
                potential.append((scope, name, oid))
    main_tips = {fork["master"], fork["gh-pages"], upstream}
    ancestors = set(git("rev-list", "--stdin", data=lines(main_tips)).decode().splitlines())
    # A fork branch that could itself be retired cannot establish publication
    # for another branch. Local-only branches cannot establish it either.
    public_history = set() if merged_only else retained_public_history(
        fork, names, {name for scope, name, _ in potential if scope == "fork"})
    pending = []
    candidates = []
    evidence_lengths = {len(oid) for oid in evidence}
    for scope, name, oid in potential:
        early = data[oid][0] > cutoff.timestamp()
        if (early or oid not in ancestors) and any(oid[:length] in evidence for length in evidence_lengths):
            continue
        if early:
            pending.append((scope, name, oid))
            continue
        if (name, oid) in merged_prs:
            candidates.append(Candidate(scope, name, oid, MERGED_SOURCE))
            continue
        if not name.startswith(MANAGED):
            continue
        if oid in ancestors:
            candidates.append(Candidate(scope, name, oid, "reachable from a published base"))
        elif oid in public_history:
            candidates.append(Candidate(scope, name, oid, PUBLISHED_BRANCH))
        elif name.startswith(DISPOSABLE):
            pending.append((scope, name, oid))
    paths = payload_paths({oid for _, _, oid in pending if data[oid][2] == 1})
    pairs = {(name, oid) for _, name, oid in pending if oid in paths}
    completed = pairs & completed_runs(now) if pairs else set()
    for scope, name, oid in pending:
        if (name, oid) in completed:
            candidates.append(Candidate(scope, name, oid, COMPLETED_CI))
    dropping = {item.name for item in candidates if item.scope == "fork"}
    keep = evidence_keepers(fork, dropping, upstream, evidence)
    # Names and active PR/worker tips have already been excluded. A citation
    # by commit alone continues to work if a surviving ref contains its tip.
    old_ci = [(scope, name, oid) for scope, name, oid in potential if not merged_only
              and name.startswith(DISPOSABLE) and data[oid][0] <= cutoff.timestamp()]
    extra = redundant_ci_refs(fork, {name for scope, name, _ in old_ci if scope == "fork"} - dropping, dropping - keep)
    candidates = [item for item in candidates if item.scope != "fork" or item.name not in keep]
    candidates.extend(Candidate("fork", name, fork[name], REDUNDANT_CI) for name in extra)
    dropping = dropping - keep | extra
    if evidence_keepers(fork, dropping, upstream, evidence):
        raise ValueError("fork plan still contains the last published branch for source or cited commits")
    # A completed CI run can retire a day-old local copy along with its fork
    # branch. A reset/amend may still leave unpublished work in its reflog.
    pairs = {i.name: i.oid for i in candidates if i.scope == "fork" and i.reason == COMPLETED_CI
             and local.get(i.name) == i.oid}
    candidates = [i for i in candidates if i.scope != "local" or i.name not in pairs]
    recent_ci = local_activity(pairs, now - CI_IDLE) if pairs else set()
    pairs = {name: oid for name, oid in pairs.items() if name not in recent_ci}
    for name in published_reflogs(pairs, set(fork.values()) | {upstream}):
        candidates.append(Candidate("local", name, pairs[name], LOCAL_CI))
    removing = {item.name for item in candidates}
    # A commit URL still resolves when its same-name fork branch survives.
    # Check reflogs too: a local rebase may have displaced unpublished work.
    mirrors = {name: oid for name, oid in mirrors.items() if name not in removing}
    surviving = {oid for name, oid in fork.items() if name not in dropping}
    for name in published_reflogs(mirrors, surviving):
        candidates.append(Candidate("local", name, mirrors[name], FORK_MIRROR))
    local_candidates = {item.name for item in candidates if item.scope == "local"}
    old_local = {name: oid for scope, name, oid in old_ci if scope == "local" and name not in local_candidates}
    for name in published_reflogs(old_local, surviving):
        candidates.append(Candidate("local", name, old_local[name], REDUNDANT_CI))
    unpublished = unpublished_locals(candidates, surviving | {upstream})
    return sorted(item for item in candidates if item.scope != "local" or item.name not in unpublished)


def archive(items: list[Candidate]) -> str:
    common = Path(os.fsdecode(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()))
    logs = {}
    parents = {item.oid for item in items}
    for item in items:
        if item.scope != "local":
            continue
        path = common / "logs" / "refs" / "heads" / item.name
        if path.exists():
            content = path.read_bytes()
            logs[item.name] = os.fsdecode(content)
            for row in content.splitlines():
                parents.update(v.decode() for v in row.split(b" ", 2)[:2] if v != b"0" * 40)
    table = lines(f"{row.scope}\t{row.name}\t{row.oid}" for row in items)
    reflogs = json.dumps(logs, sort_keys=True).encode()
    ref = "refs/branch-cleanup/archive"
    blobs = {}
    for filename, contents in (("refs.tsv", table), ("reflogs.json", reflogs)):
        blobs[filename] = git("hash-object", "-w", "--stdin", data=contents).decode().strip()
    tree = git("mktree", data=lines(f"100644 blob {oid}\t{filename}" for filename, oid in blobs.items())).decode().strip()
    prior = git("for-each-ref", "--format=%(objectname)", ref).decode().strip()
    if prior and git("rev-parse", prior + "^{tree}").decode().strip() == tree:
        return prior
    existing = available(parents)
    if not {item.oid for item in items} <= existing:
        raise ValueError("a planned branch tip is missing locally; fetch before cleanup")
    # Use one local ref across all sweeps. An octopus parent per extant
    # historical tip keeps its objects reachable even after reflog expiry/GC.
    last = [prior] if prior else []
    for group in chunks(sorted(existing)):
        args = [arg for parent in last + group for arg in ("-p", parent)]
        last = [git("commit-tree", tree, *args, "-m", "Retain branch tips and local reflogs before cleanup").decode().strip()]
    git("update-ref", ref, last[0], prior or "0" * 40)
    return last[0]


def delete_local(batch: list[Candidate], logs: dict[str, str], cutoff: dt.datetime, ci_cutoff: dt.datetime) -> None:
    common = Path(os.fsdecode(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()))
    # An expected tip catches A -> B, but not A -> B -> A. Under the locks,
    # compare each reflog against the exact bytes in the recovery archive.
    with subprocess.Popen(["git", "update-ref", "--stdin"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(b"start\n" + lines(f"delete {HEADS}{i.name} {i.oid}" for i in batch) + b"prepare\n")
            proc.stdin.flush()
            if proc.stdout.readline() != b"start: ok\n" or proc.stdout.readline() != b"prepare: ok\n":
                raise ValueError("a local branch moved; rerun the plan")
            names, tips = checked_out()
            for item in batch:
                path = common / "logs" / "refs" / "heads" / item.name
                if (item.name in names or item.oid in tips or item.name not in logs or not path.is_file()
                        or path.read_bytes() != os.fsencode(logs[item.name])):
                    raise ValueError(f"{item.name} changed since archival or was checked out; rerun the plan")
            for short, limit in ((False, cutoff), (True, ci_cutoff)):
                refs = {i.name: i.oid for i in batch if (i.reason == LOCAL_CI) == short}
                if refs and local_activity(refs, limit):
                    raise ValueError("a local branch became active; rerun the plan")
            proc.stdin.write(b"commit\n")
            proc.stdin.flush()
            if proc.stdout.readline() != b"commit: ok\n":
                raise ValueError("local branch deletion did not commit")
        finally:
            # EOF without commit aborts a prepared transaction and releases its locks.
            proc.stdin.close()


def apply(items: list[Candidate], retention: int) -> None:
    if not items:
        return
    if git("rev-parse", "--show-ref-format").strip() != b"files":
        raise ValueError("local reflog preservation requires Git's files ref backend")
    # The proof search can take minutes. Re-read live exclusions after it.
    check_origin()
    ci = {(i.name, i.oid) for i in items if i.reason == COMPLETED_CI}
    if ci and not ci <= completed_runs(dt.datetime.now(dt.timezone.utc)):
        raise ValueError("a retired CI branch ran again; rerun the plan")
    fork = remote_refs("origin")
    upstream = public_bases(fork)
    names, tips = live_protections()
    evidence = set()
    # A checkout may have retained a different version of a planned fork
    # branch since the plan was constructed.
    local = dict(row.split("\t") for row in git("for-each-ref", "--format=%(refname:lstrip=2)%09%(objectname)",
                                                 HEADS).decode().splitlines())
    names.update(name for name in local.keys() & fork.keys() if local[name] != fork[name])
    merged_prs = tracker_protections(fork, local, upstream, names, evidence)
    evidence_lengths = {len(oid) for oid in evidence}
    lengths = {len(oid) for oid in tips if oid}
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=retention)
    local_ci = {i.name: i.oid for i in items if i.reason == LOCAL_CI}
    recent = local_activity({i.name: i.oid for i in items if i.scope == "local" and i.name not in local_ci}, cutoff)
    if local_ci:
        recent.update(local_activity(local_ci, now - CI_IDLE))
    fork_ci = {i.name: i.oid for i in items if i.scope == "fork" and i.reason == COMPLETED_CI}
    if (any(fork_ci.get(name) != oid for name, oid in local_ci.items())
            or published_reflogs(local_ci, set(fork.values()) | {upstream}) != local_ci.keys()):
        raise ValueError("a paired CI branch or its published history changed; rerun the plan")
    for item in items:
        if (item.name in names or item.scope == "local" and item.name in recent or any(item.oid[:length] in tips for length in lengths)
                or item.scope == "fork" and fork.get(item.name) != item.oid
                or item.reason == MERGED_SOURCE and (item.name, item.oid) not in merged_prs
                or item.reason not in {FORK_MIRROR, "reachable from a published base", MERGED_SOURCE, REDUNDANT_CI}
                and any(item.oid[:length] in evidence for length in evidence_lengths)):
            raise ValueError(f"{item.name} changed or became active; rerun the plan")
    dropping = {item.name for item in items if item.scope == "fork"}
    require_public_descendants(items, fork, names, dropping)
    if unpublished_locals(items, {oid for name, oid in fork.items() if name not in dropping} | {upstream}):
        raise ValueError("a branch has unpublished local history; rerun the plan")
    if evidence_keepers(fork, dropping, upstream, evidence):
        raise ValueError("a candidate now contains the only published copy of source or cited work; rerun the plan")
    ref = archive(items)
    logs = json.loads(git("show", ref + ":reflogs.json"))
    print(f"Local recovery manifest: git show {ref}:refs.tsv", file=sys.stderr, flush=True)
    # Remote deletions are atomic per batch. A failed lease leaves later
    # batches untouched. An interrupted run can simply recompute its plan.
    for scope in ("fork", "local"):
        if scope == "local":
            fork = remote_refs("origin")
            if (local_ci.keys() & fork.keys()
                    or published_reflogs(local_ci, set(fork.values()) | {ref, upstream}) != local_ci.keys()):
                raise ValueError("a paired CI branch or its archived history changed; rerun the plan")
            mirrors = {item.name: item.oid for item in items if item.reason == FORK_MIRROR}
            if (any(fork.get(name) != oid for name, oid in mirrors.items())
                    or published_reflogs(mirrors, set(fork.values())) != mirrors.keys()):
                raise ValueError("a local mirror or its published history changed; rerun the plan")
        for batch in chunks([item for item in items if item.scope == scope], 80):
            if scope == "local" or any(i.reason in (PUBLISHED_BRANCH, REDUNDANT_CI) for i in batch):
                fork = remote_refs("origin")
                require_public_descendants(batch, fork, names, dropping)
                if scope == "local" and unpublished_locals(batch, set(fork.values()) | {upstream}):
                    raise ValueError("a branch has unpublished local history; rerun the plan")
            # An unchanged tip can acquire a PR or a workflow while earlier
            # batches run. A Git push lease alone cannot observe that.
            occupied, heads = live_protections() if scope == "fork" else checked_out()
            if any(item.name in occupied or item.oid in heads for item in batch):
                raise ValueError("a candidate became active during the cleanup; rerun the plan")
            if scope == "fork":
                git("-c", "remote.origin.mirror=false", "push", "--atomic", "--no-follow-tags", "--recurse-submodules=no",
                    *(f"--force-with-lease={HEADS}{item.name}:{item.oid}" for item in batch), "origin",
                    *(f":{HEADS}{item.name}" for item in batch))
            else:
                delete_local(batch, logs, cutoff, now - CI_IDLE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="archive and delete the freshly planned branches")
    parser.add_argument("--merged-only", action="store_true", help="check only merged source branches already on upstream master")
    parser.add_argument("--retention-days", type=int, default=7,
                        help="minimum age in days, except completed disposable CI (default 7)")
    args = parser.parse_args()
    if args.retention_days < 7:
        parser.error("keep at least seven days of recent branches")
    items = plan(args.retention_days, merged_only=args.merged_only)
    for item in items:
        print(f"{item.scope}\t{item.name}\t{item.oid}\t{item.reason}")
    print(f"{len(items)} eligible refs", file=sys.stderr)
    if args.apply:
        apply(items, args.retention_days)


if __name__ == "__main__":
    main()
