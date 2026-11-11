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


def tracker_protections(pages: str, names: set[str], tips: set[str]) -> set[tuple[str, str]]:
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
    prs = {(row.get("repo", UPSTREAM), row["number"]): row for row in snapshot["prs"]}
    merged = {(row["ref"], row["head"]) for row in prs.values()
              if row.get("headRepo") == FORK and row["status"] == "merged"}
    for node in registry["nodes"]:
        source = prs.get((node.get("repo", UPSTREAM), node.get("number")))
        if node.get("ref") and not (source and (node["ref"], source["head"]) in merged):
            names.add(node["ref"])
        if node["id"].startswith("branch:"):
            names.add(node["id"].removeprefix("branch:"))
    for row in prs.values():
        if row["status"] == "open" and row.get("headRepo") == FORK:
            names.add(row["ref"])
            tips.add(row["head"])
    # A tree URL needs its branch name to survive. Kept commit/compare links
    # must remain reachable on the fork, including historical review evidence.
    handoffs = {key: value for key, value in snapshot.items()
                if key.endswith("Handoff") or key in {"incomingReviewRequests", "ciFlakes", "reviewOutreach"}}
    blob = unquote(json.dumps([registry, handoffs]))
    names.update(re.findall(r"(?:github\.com/tamird/gvisor/tree/|\b)((?:qualification|diagnostics|research|review|proposal|integration|tracker|workmap|rebase|delivery)/[\w./-]+)", blob))
    for target in re.findall(r"github\.com/tamird/gvisor/tree/([\w./-]+)", blob):
        # A tree route may append a source directory after the branch.
        parts = target.strip("/").split("/")
        names.update("/".join(parts[:end]) for end in range(1, len(parts) + 1))
    for match in re.finditer(r"github\.com/(?:tamird|google)/gvisor/(?:commit|compare|blob)/([a-f0-9]{7,40})(?:\.{2,3}([a-f0-9]{7,40}))?", blob):
        tips.update(part for part in match.groups() if part)
    # Prior review heads, inline replies and qualification receipts may live
    # only in the GitHub snapshot. Their commits must stay reachable even
    # when the last published tip is several commits ahead of the citation.
    all_text = unquote(json.dumps([registry, snapshot]))
    tips.update(re.findall(r"(?<![A-Za-z0-9])[a-f0-9]{7,40}(?![A-Za-z0-9])", all_text))
    return merged


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
        if (kind, value) not in {("remote", "origin"), ("merge", HEADS + name)}:
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


def completed_runs(cutoff: dt.date) -> set[tuple[str, str]]:
    # A missing or expired run is never evidence for retirement. Bound the
    # historical lookup; each day gets its own GitHub search-result limit.
    result = set()
    for days in range(28):
        day = cutoff - dt.timedelta(days=days)
        rows = api(f"repos/{FORK}/actions/runs?status=completed&created={day}&per_page=100",
                   f'[.workflow_runs[] | select((.event=="push" or .event=="workflow_dispatch") and .head_repository.full_name=="{FORK}") | [.head_branch,.head_sha]]',
                   pages=True)
        result.update(map(tuple, rows))
    return result


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


def evidence_keepers(fork: dict[str, str], dropping: set[str], upstream: str, evidence: set[str]) -> set[str]:
    """Return branches containing cited commits that the deletion would orphan."""
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
    graph = git("rev-list", "--topo-order", "--parents", "--stdin", data=lines(inputs)).decode()
    lengths = {len(oid) for oid in evidence}
    keep = set()
    for line in graph.splitlines():
        oid, *parents = line.split()
        name = origins[oid]
        if any(oid[:size] in evidence for size in lengths):
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


def plan(retention: int) -> list[Candidate]:
    check_origin()
    fork = remote_refs("origin")
    upstream = public_bases(fork)
    local = dict(row.split("\t") for row in git("for-each-ref", "--format=%(refname:lstrip=2)%09%(objectname)",
                                                 HEADS).decode().splitlines())
    names, tips = live_protections()
    evidence = set()
    merged_prs = tracker_protections(fork["gh-pages"], names, evidence)
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
            if stamp <= cutoff.timestamp() and author == owner:
                if scope == "local" and fork.get(name) == oid:
                    mirrors[name] = oid
                if name.startswith(MANAGED) or (name, oid) in merged_prs:
                    potential.append((scope, name, oid))
    main_tips = {fork["master"], fork["gh-pages"], upstream}
    ancestors = set(git("rev-list", "--stdin", data=lines(main_tips)).decode().splitlines())
    retained = {scope: {oid for name, oid in refs.items() if not name.startswith(MANAGED) or name in names}
                for scope, refs in branches.items()}
    pending = []
    candidates = []
    evidence_lengths = {len(oid) for oid in evidence}
    for scope, name, oid in potential:
        if oid not in ancestors and any(oid[:length] in evidence for length in evidence_lengths):
            continue
        if not name.startswith(MANAGED):
            if oid in ancestors and (name, oid) in merged_prs:
                candidates.append(Candidate(scope, name, oid, "merged PR at exact upstream-reachable tip"))
            continue
        if oid in ancestors or oid in retained[scope]:
            candidates.append(Candidate(scope, name, oid, "reachable from a published base" if oid in ancestors else "retained branch has identical tip"))
        elif name.startswith(DISPOSABLE):
            pending.append((scope, name, oid))
    paths = payload_paths({oid for _, _, oid in pending if data[oid][2] == 1})
    pairs = {(name, oid) for _, name, oid in pending if oid in paths}
    completed = pairs & completed_runs(cutoff.date()) if pairs else set()
    for scope, name, oid in pending:
        if (name, oid) in completed:
            candidates.append(Candidate(scope, name, oid, "retired CI payload with a completed run at the exact tip"))
    dropping = {item.name for item in candidates if item.scope == "fork"}
    keep = evidence_keepers(fork, dropping, upstream, evidence)
    if evidence_keepers(fork, dropping - keep, upstream, evidence):
        raise ValueError("fork plan still contains the last published branch for cited commits")
    candidates = [item for item in candidates if item.scope != "fork" or item.name not in keep]
    removing = {item.name for item in candidates}
    # A commit URL still resolves when its same-name fork branch survives.
    # Check reflogs too: a local rebase may have displaced unpublished work.
    mirrors = {name: oid for name, oid in mirrors.items() if name not in removing}
    dropping -= keep
    surviving = {oid for name, oid in fork.items() if name not in dropping}
    for name in published_reflogs(mirrors, surviving):
        candidates.append(Candidate("local", name, mirrors[name], FORK_MIRROR))
    return sorted(candidates)


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


def apply(items: list[Candidate], retention: int) -> None:
    if not items:
        return
    if git("rev-parse", "--show-ref-format").strip() != b"files":
        raise ValueError("local reflog preservation requires Git's files ref backend")
    # The proof search can take minutes. Re-read live exclusions after it.
    check_origin()
    fork = remote_refs("origin")
    upstream = public_bases(fork)
    names, tips = live_protections()
    evidence = set()
    tracker_protections(fork["gh-pages"], names, evidence)
    evidence_lengths = {len(oid) for oid in evidence}
    lengths = {len(oid) for oid in tips if oid}
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention)
    recent = local_activity({i.name: i.oid for i in items if i.scope == "local"}, cutoff)
    for item in items:
        if (item.name in names or item.scope == "local" and item.name in recent or any(item.oid[:length] in tips for length in lengths)
                or item.scope == "fork" and fork.get(item.name) != item.oid
                or item.reason not in {FORK_MIRROR, "reachable from a published base", "merged PR at exact upstream-reachable tip"}
                and any(item.oid[:length] in evidence for length in evidence_lengths)):
            raise ValueError(f"{item.name} changed or became active; rerun the plan")
    dropping = {item.name for item in items if item.scope == "fork"}
    if evidence_keepers(fork, dropping, upstream, evidence):
        raise ValueError("a candidate now contains the only published copy of cited work; rerun the plan")
    ref = archive(items)
    print(f"Local recovery manifest: git show {ref}:refs.tsv", file=sys.stderr, flush=True)
    # Remote deletions are atomic per batch. A failed lease leaves later
    # batches untouched. An interrupted run can simply recompute its plan.
    for scope in ("fork", "local"):
        if scope == "local":
            fork = remote_refs("origin")
            mirrors = {item.name: item.oid for item in items if item.reason == FORK_MIRROR}
            if (any(fork.get(name) != oid for name, oid in mirrors.items())
                    or published_reflogs(mirrors, set(fork.values())) != mirrors.keys()):
                raise ValueError("a local mirror or its published history changed; rerun the plan")
        for batch in chunks([item for item in items if item.scope == scope], 80):
            names, tips = checked_out()
            if any(item.name in names or item.oid in tips for item in batch):
                raise ValueError("a candidate was checked out during the cleanup; rerun the plan")
            if scope == "fork":
                git("-c", "remote.origin.mirror=false", "push", "--atomic", "--no-follow-tags", "--recurse-submodules=no",
                    *(f"--force-with-lease={HEADS}{item.name}:{item.oid}" for item in batch), "origin",
                    *(f":{HEADS}{item.name}" for item in batch))
            else:
                git("update-ref", "--stdin", data=lines(f"delete {HEADS}{item.name} {item.oid}" for item in batch))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="archive and delete the freshly planned branches")
    parser.add_argument("--retention-days", type=int, default=7, help="minimum commit age in days (default 7)")
    args = parser.parse_args()
    if args.retention_days < 7:
        parser.error("keep at least seven days of recent branches")
    items = plan(args.retention_days)
    for item in items:
        print(f"{item.scope}\t{item.name}\t{item.oid}\t{item.reason}")
    print(f"{len(items)} eligible refs", file=sys.stderr)
    if args.apply:
        apply(items, args.retention_days)


if __name__ == "__main__":
    main()
