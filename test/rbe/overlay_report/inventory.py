import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


SOURCES = {
    "stock": "3b6a97d8f8c99704e4f9139abece33c0723f23c9",
    "map": "872ac9e3e30d0e27c5559ab866556fa377173a19",
    "fixed": "84a21e699e533ce807970ffb29842095ce878a7f",
}


def call(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        "https://app.buildbuddy.io/" + path,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"],
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def rows_for(invocation_id: str) -> list[str]:
    request = {"selector": {"invocationId": invocation_id}}
    parts = []
    length = 0
    for _ in range(100):
        response = call("api/v1/GetLog", request)
        chunk = response.get("log", {}).get("contents", "")
        length += len(chunk)
        if length > 20_000_000:
            return []
        parts.append(chunk)
        token = response.get("nextPageToken") or response.get("next_page_token")
        if not token:
            break
        request["pageToken"] = token
    pattern = r"BenchmarkOverlayPoll/(?:tmpfs|proc)/live[18] 100000 [0-9]+[.][0-9]+ ns/op [0-9]+[.][0-9]+ allocs/op"
    return re.findall(pattern, "".join(parts))


def main() -> None:
    root = Path(os.environ["RUNNER_TEMP"]) / "remote-receipts"
    for arm, sha in SOURCES.items():
        try:
            data = call("rpc/BuildBuddyService/SearchInvocation", {
                "query": {"commitSha": sha}, "count": 12,
            })
        except urllib.error.HTTPError as error:
            print(arm, "search HTTP", error.code)
            continue
        except (urllib.error.URLError, ValueError, TimeoutError) as error:
            print(arm, type(error).__name__)
            continue
        found = data.get("invocation", [])
        print(arm, sha, "records", len(found))
        for invocation in found:
            if invocation.get("commitSha", sha) != sha:
                continue
            invocation_id = invocation.get("invocationId")
            print(" ", invocation_id,
                  invocation.get("invocationStatus"), invocation.get("success"),
                  invocation.get("durationUsec"))
            if not invocation_id:
                continue
            try:
                rows = rows_for(invocation_id)
            except urllib.error.HTTPError as error:
                print("  log HTTP", error.code)
                continue
            except (urllib.error.URLError, ValueError, TimeoutError) as error:
                print("  log", type(error).__name__)
                continue
            if len(rows) == 40:
                dest = root / (arm + "-cost")
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "receipt").write_text("\n".join(rows) + "\n")
                print("  40 original benchmark samples")


if __name__ == "__main__":
    main()
