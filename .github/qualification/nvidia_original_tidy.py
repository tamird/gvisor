"""Read the specific earlier run_differ analysis error, without publishing build logs."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib import error, request


CARRIER = "c1ee307969da7c060fec2122074f09e9416b3814"
FIXTURE = "f2a65e06ac211813a61ec1efadb8eeb6e6270e13"
PRODUCT = "b4929480061e95c901f3f21ed3ced64a8ea3f447"
LABEL = "//tools/nvidia_driver_differ:run_differ"
UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
W = Path(os.environ["RUNNER_TEMP"]) / "nvidia-abi-cli"
START = datetime(2026, 11, 6, 22, 1, 36, tzinfo=timezone.utc).timestamp()
END = datetime(2026, 11, 6, 22, 2, 50, tzinfo=timezone.utc).timestamp()
KEY = os.environ.pop("BUILDBUDDY_API_KEY")


class Fixed(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def rpc(route, payload, limit):
    assert route in ("api/v1/GetInvocation", "rpc/BuildBuddyService/GetEventLogChunk")
    req = request.Request("https://app.buildbuddy.io/" + route, data=json.dumps(payload).encode(),
                          headers={"Content-Type":"application/json", "x-buildbuddy-api-key":KEY})
    with request.build_opener(Fixed).open(req, timeout=25) as res:
        raw = res.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("oversize")
    return json.loads(raw)


def signals(text):
    found = {k: k.lower() in text.lower() for k in
             ("org_golang_x_text", "No repository visible", "could not be resolved",
              "//tools/go_marshal/gomarshal", "BUILD file not found", "no such package",
              "No space left on device", "RESOURCE_EXHAUSTED", "checksum", "HTTP 403")}
    return {k: v for k, v in found.items() if v}


def main():
    out = {"github_run": 42300917747, "matches": 0, "signals": {}, "read": False}
    try:
        matches = set()
        for sha in (CARRIER, FIXTURE, PRODUCT):
            result = rpc("api/v1/GetInvocation", {"selector":{"commitSha":sha}}, 1024 * 1024)
            for inv in result.get("invocation", []):
                uid = inv.get("id", {}).get("invocationId", inv.get("id", {}).get("invocation_id", ""))
                commit = inv.get("commitSha", inv.get("commit_sha"))
                repo = inv.get("repoUrl", inv.get("repo_url", "")).removesuffix(".git")
                stamp = int(inv.get("createdAtUsec", inv.get("created_at_usec", 0))) / 1e6
                if (UUID.fullmatch(uid) and sha == commit and START <= stamp <= END
                        and inv.get("command") == "run" and LABEL in inv.get("pattern", "")
                        and repo in ("https://github.com/tamird/gvisor", "git@github.com:tamird/gvisor")):
                    matches.add(uid)
        out["matches"] = len(matches)
        if len(matches) == 1:
            (uid,) = matches
            out["invocation"] = uid
            cursor = ""
            for _ in range(3):
                payload = {"invocationId": uid, "minLines": 45}
                if cursor:
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cursor):
                        break
                    payload["chunkId"] = cursor
                chunk = rpc("rpc/BuildBuddyService/GetEventLogChunk", payload, 4 * 1024 * 1024)
                body = base64.b64decode(chunk.get("buffer", ""), validate=True)
                lines = body.decode(errors="replace").splitlines()
                out["signals"].update(signals("\n".join(lines)))
                out.setdefault("chunk_hashes", []).append(hashlib.sha256(body).hexdigest())
                out["read"] = True
                cursor = chunk.get("previousChunkId", "")
                if not cursor or out["signals"].get("org_golang_x_text"):
                    break
    except (OSError, ValueError) as exc:
        out["error"] = "HTTP " + str(exc.code) if isinstance(exc, error.HTTPError) else type(exc).__name__
    data = json.dumps(out, indent=2)
    assert KEY not in data and len(data) < 4000
    (W / "original.json").write_text(data + "\n")


if __name__ == "__main__":
    main()
