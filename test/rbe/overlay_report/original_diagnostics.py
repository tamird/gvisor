"""Read only the two completed original commands. Never retain raw command logs."""

import json
import os
import re
import urllib.request
from hashlib import sha256
from urllib.parse import urlsplit


INVOCATIONS = {
    "compiler": "0eec5128-275a-4657-a456-ab9b3d479d86",
    "checkpoint": "d67e3288-8eb2-480d-b8ad-7d6e47c3a123",
}


def read_api(method: str, payload: dict, max_bytes: int) -> bytes:
    req = urllib.request.Request(
        "https://app.buildbuddy.io/api/v1/" + method,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("source exceeds bound")
    return data


def guest_log(invocation: str) -> str:
    selector = {"invocation_id": invocation, "target_label": "//test/syscalls:epoll_test_runsc_systrap_directfs_save"}
    actions = json.loads(read_api("GetAction", {"selector": selector}, 2 << 20))
    for action in actions.get("action", []):
        for file in action.get("file", []):
            if file.get("name", "").rsplit("/", 1)[-1] != "test.log":
                continue
            uri = file.get("uri", "")
            parsed = urlsplit(uri)
            digest = re.search(r"/blobs/([a-f0-9]{64})/(\d+)$", parsed.path)
            if parsed.scheme != "bytestream" or parsed.hostname != "remote.buildbuddy.io" or not digest:
                continue
            size = int(digest[2])
            if not 0 < size <= 16 << 20:
                continue
            data = read_api("GetFile", {"uri": uri}, size)
            if len(data) != size or sha256(data).hexdigest() != digest[1]:
                raise ValueError("wrong test log")
            print("test.log", len(data), digest[1])
            return data.decode(errors="replace")
    return ""


def main() -> None:
    for name, invocation in INVOCATIONS.items():
        token = "0000"
        seen = set()
        parts = []
        try:
            while token and token not in seen and len(seen) < 60:
                seen.add(token)
                body = {"selector": {"invocation_id": invocation}, "page_token": token}
                page = json.loads(read_api("GetLog", body, 3 << 20))
                parts.append(page.get("log", {}).get("contents", ""))
                token = page.get("nextPageToken", "")
        except OSError as error:
            print(name, invocation, "log unavailable", type(error).__name__, getattr(error, "code", ""))
        if name == "checkpoint":
            try:
                parts.append(guest_log(invocation))
            except (OSError, ValueError) as error:
                print(name, "test.log unavailable", type(error).__name__)
        text = re.sub(r"\x1b\[[;\d]*[A-Za-z]", "", "".join(parts)).replace("\r", "\n")
        print(name, invocation, "log-bytes", len(text.encode()))
        relevant = (
            r"overlay_poll_waiter_test[.]go:\d+:\d+:.*"
            if name == "compiler"
            else r"(?:unregistered type|unknown type|unrecognized type|unsupported type|unregistered object|cannot save|can't save|could not save|failed to save|error saving|checkpoint failed|unsupported channel|waiter[.]ChannelNotifier|not supported by stateify|not registered with state|failed with signal [0-9]+|FATAL ERROR|error saving sandbox|failed to checkpoint|panic: |\[  FAILED  \]|OVERLAY_POLL_CHILD:|syserror:).*"
        )
        selected = []
        for line in text.splitlines():
            if re.search(r"token|secret|api[._-]?key|credential|authorization|bearer|passwd|gh[pousr]_", line, re.I):
                continue
            m = re.search(relevant, line, re.I)
            if m and m[0][:240] not in selected:
                selected.append(m[0][:240])
        for line in selected[:12]:
            print(line)


if __name__ == "__main__":
    main()
