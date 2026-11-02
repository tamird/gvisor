"""Keep only producer identity and declared-package violations from Bazel 8.8.1's compact execution log."""

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


def now():
    return datetime.now(timezone.utc).isoformat()


# Length-delimited proto records; the field numbers below belong to Bazel
# 8.8.1 src/main/protobuf/spawn.proto (ExecLogEntry). Skip sensitive fields
# including full argv, env, platform properties and command output.
def number(f):
    n = 0
    for shift in range(0, 70, 7):
        b = f.read(1)
        if not b:
            if shift:
                raise ValueError("truncated varint")
            return None
        n |= (b[0] & 127) << shift
        if b[0] < 128:
            return n
    raise ValueError("oversized varint")


def fields(buf):
    f = BytesIO(buf)
    out = defaultdict(list)
    while (tag := number(f)) is not None:
        field, typ = divmod(tag, 8)
        if typ in (0, 2):
            n = number(f)
            if n is None:
                raise ValueError("truncated field")
            v = n if typ == 0 else f.read(n)
            if typ == 2 and len(v) != n:
                raise ValueError("truncated bytes")
        elif typ in (1, 5):
            v = f.read(8 if typ == 1 else 4)
        else:
            raise ValueError("invalid field")
        out[field].append(v)
    return out


def first(m, k, default=b""):
    return m.get(k, [default])[0]


def digest(b):
    m = fields(b)
    return [first(m, 1).decode(), first(m, 2, 0)]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def cache_record(key):
    h, n = key
    if not re.fullmatch("[a-f0-9]{64}", h) or n <= 0:
        return {"retrieved": False}
    # Read the ActionResult, never the action's environment, request headers
    # or cached output streams. A worker string can be private.
    url = "https://app.buildbuddy.io/file/download?" + urlencode({"bytestream_url": f"actioncache://remote.buildbuddy.io/blobs/ac/{h}/{n}"})
    req = Request(url, headers={"x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    try:
        with build_opener(NoRedirect()).open(req, timeout=8) as resp:
            data = resp.read(2 << 20)
        if len(data) >= 2 << 20:
            return {"retrieved": False, "reason": "size"}
        result = fields(data)
        meta = fields(first(result, 9))
        out = {"retrieved": True, "resultSha256": sha256(data).hexdigest(), "hasExecutionMetadata": bool(meta), "hasWorker": bool(first(meta, 1))}
        for k, name in [(3, "workerStart"), (7, "executionStart"), (8, "executionEnd")]:
            if k in meta:
                tm = fields(first(meta, k))
                out[name] = datetime.fromtimestamp(first(tm, 1, 0) + first(tm, 2, 0) / 1e9, timezone.utc).isoformat()
        if first(meta, 1):
            out["workerSha256"] = sha256(first(meta, 1)).hexdigest()
        return out
    except HTTPError as e:
        return {"retrieved": False, "httpStatus": e.code}
    except (URLError, OSError, ValueError) as e:
        return {"retrieved": False, "reason": type(e).__name__}


excluded = re.compile(r"(?:^|/)(crypto/internal/sysrand/internal/seccomp|encoding/json/internal/jsontest|log/slog/internal/(?:benchmarks|slogtest)|internal/(?:obscuretestdata|testpty)|net/internal/(?:cgotest|socktest)|reflect/internal/example[^/]*|runtime/internal/startlinetest)\.a$")


def summarize(log, query_cache):
    directories = {}
    spawns = []
    inv = None
    with subprocess.Popen(["zstd", "-qdc", str(log)], stdout=subprocess.PIPE) as z:
        while (n := number(z.stdout)) is not None:
            entry = fields(z.stdout.read(n))
            if 2 in entry:
                inv = first(fields(first(entry, 2)), 4).decode()
            if 4 in entry:
                d = fields(first(entry, 4))
                root = first(d, 1).decode()
                if "/stdlib_" in root or "/stdlib/" in root:
                    paths = [first(fields(x), 1).decode() for x in d[2]]
                    archives = [x for x in paths if x.endswith(".a")]
                    if archives:
                        directories[first(entry, 1)] = {"path": root, "archiveCount": len(archives), "fmt": [x for x in archives if x.endswith("/fmt.a")], "excludedArchives": sorted(x for x in archives if excluded.search(x))}
            if 7 not in entry:
                continue
            s = fields(first(entry, 7))
            mn = first(s, 8).decode()
            if mn not in {"GoStdlib", "GoStandardLibraryAnalysis"}:
                continue
            outputs = [first(fields(o), 5, 0) for o in s[6]]
            row = {"mnemonic": mn, "label": first(s, 7).decode(), "runner": first(s, 11).decode(), "cacheHit": bool(first(s, 12, 0)), "exit": first(s, 9, 0), "status": first(s, 10).decode(), "digest": digest(first(s, 16)), "outputs": [directories[i] for i in outputs if i in directories]}
            if mn == "GoStandardLibraryAnalysis":
                row["architecture"] = [a.decode() for a in s[1] if a.startswith(b"-GOARCH=")]
                row["stdlibInputs"] = [a.decode() for a in s[1] if b"stdlib_" in a and len(a) < 300 and re.fullmatch(rb'[-=/+.@\w]+', a)]
            if query_cache and mn == "GoStdlib" and row["cacheHit"]:
                row["actionCache"] = cache_record(row["digest"])
            spawns.append(row)
        if z.wait():
            raise ValueError("invalid compact log")
    return {"invocation": inv, "actions": spawns}


def original():
    facts = Path(os.environ["RUNNER_TEMP"], "direct-smoke")
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert source == os.environ["ORIGINAL"]
    for name in (["release", "release-race"] if os.environ["MACHINE"] == "x86_64" else ["release"]):
        raw = facts / f"{name}.original.compact"
        log = facts / f"{name}.original.log"
        row = {"source": source, "target": "//:" + name, "startedAt": now()}
        with log.open("w") as out:
            row["exit"] = subprocess.run(["timeout", "--kill-after=15s", "14m", "bazel", f"--bazelrc={facts}/cache.rc", "build", "--config=" + os.environ["MACHINE"], f"--execution_log_compact_file={raw}", row["target"]], stdout=out, stderr=subprocess.STDOUT).returncode
        row["finishedAt"] = now()
        row["unmatchedPackages"] = sorted(set(re.findall(r'error filtering packages: package "([a-zA-Z0-9_/]+)" present in stdlib GOROOT but not in source', log.read_text(errors="replace"))))
        if raw.is_file():
            try:
                row.update(summarize(raw, True))
            except (ValueError, OSError) as e:
                row["collectorError"] = type(e).__name__
        (facts / "records" / f"{name}.original.json").write_text(json.dumps(row, indent=2) + "\n")
        print(name, "original", row["exit"], row["unmatchedPackages"], "collected", len(row.get("actions", [])), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "original":
        original()
    else:
        assert sys.argv[1] == "summarize"
        Path(sys.argv[3]).write_text(json.dumps(summarize(Path(sys.argv[2]), False), indent=2) + "\n")
