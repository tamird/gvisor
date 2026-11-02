"""Read selected inputs from an original REAPI action without running it."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import subprocess
import urllib.parse
import urllib.request

TMP = Path(os.environ["RUNNER_TEMP"])
KEY = os.environ["BUILDBUDDY_API_KEY"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def fetch(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request("https://app.buildbuddy.io/" + path, data=data,
        headers={"x-buildbuddy-api-key": KEY, "Content-Type": "application/json"})
    with OPENER.open(request, timeout=40) as response:
        body = response.read(64 * 1024 * 1024 + 1)
    assert len(body) <= 64 * 1024 * 1024, "Response exceeds bound"
    return body


def rpc(method, payload):
    return json.loads(fetch("rpc/BuildBuddyService/" + method, payload))


def blob(digest, invocation):
    size = int(digest["sizeBytes"])
    assert 0 < size <= 64 * 1024 * 1024, "Original input exceeds bound"
    uri = "bytestream://remote.buildbuddy.io/blobs/" + digest["hash"] + "/" + str(size)
    data = fetch("file/download?" + urllib.parse.urlencode({"bytestream_url": uri, "invocation_id": invocation}))
    assert len(data) == size and hashlib.sha256(data).hexdigest() == digest["hash"], "Original input digest mismatch"
    return data


def cas(digest, kind):
    # Reuse the official CLI's REAPI decoder, as in llvm_parser_metadata.py.
    # Only the selected ELF and worker fields below leave the coordinator.
    result = subprocess.run([str(TMP / "bb"), "download", digest["hash"] + "/" + str(digest["sizeBytes"]),
                             "--type=" + kind, "--target=grpcs://remote.buildbuddy.io", "--api_key=" + KEY],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    assert result.returncode == 0, "Original CAS metadata unavailable"
    return json.loads(result.stdout)


def input_file(root, path):
    """Follow only the selected path and bounded relative REAPI symlinks."""
    cache = {}
    for _ in range(24):
        path = posixpath.normpath(path)
        assert not path.startswith(("/", "../")) and path != "..", "Input symlink escapes its action root"
        parts = PurePosixPath(path).parts
        directory = root
        for index, part in enumerate(parts):
            key = (directory["hash"], str(directory["sizeBytes"]))
            if key not in cache:
                cache[key] = cas(directory, "Directory")
            node = cache[key]
            links = [item for item in node.get("symlinks", []) if item["name"] == part]
            if links:
                assert len(links) == 1 and not links[0]["target"].startswith("/"), "Invalid input symlink"
                path = posixpath.join(*parts[:index], links[0]["target"], *parts[index + 1:])
                break
            if index == len(parts) - 1:
                files = [item for item in node.get("files", []) if item["name"] == part]
                assert len(files) == 1, "Original input file not found: " + path
                return files[0]
            children = [item for item in node.get("directories", []) if item["name"] == part]
            assert len(children) == 1, "Original input directory not found: " + path
            directory = children[0]["digest"]
        else:
            raise AssertionError("Original input path is empty")
    raise AssertionError("Original input symlink traversal exceeded bound")
