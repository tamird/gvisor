"""Read just the two recorded ARM64 release CLI objects, without redirects."""

import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


FILES = {
    "direct": ("fe9e302d-c042-4218-a150-85d9a1213a85", "bbaf50e2fcba01307c17dd01ed0b7f9c74905daa3f9de32b3e6c2e080b62b994", 18177393),
    "paired": ("078dbb74-7b6d-44e9-a00f-276d3b82d5de", "110be0bec5113bc84911410ab256804294af0c7537266927a4b92278885b5783", 18178113),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(work, name, entry, key):
    invocation, expected, size = entry
    result = {"invocation": invocation, "sha256": expected, "size": size, "verified": False}
    obj = "bytestream://remote.buildbuddy.io/blobs/" + expected + "/" + str(size)
    query = urlencode({"invocation_id": invocation, "bytestream_url": obj})
    req = Request("https://app.buildbuddy.io/file/download?" + query, headers={"x-buildbuddy-api-key": key})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=75) as response:
            content = response.read(size + 1)
        result["bytesRead"] = len(content)
        if len(content) == size and hashlib.sha256(content).hexdigest() == expected:
            with (work / name).open("xb") as output:
                output.write(content)
            result["verified"] = True
    except HTTPError as err:
        result["httpCode"] = err.code
    except (URLError, OSError) as err:
        result["errorClass"] = type(err).__name__
    return result


if __name__ == "__main__":
    work = Path(sys.argv[1])
    key = os.environ.pop("ACCESS")
    assert key
    result = {}
    for name, entry in FILES.items():
        result[name] = fetch(work, name, entry, key)
        (work / "fetch.json").write_text(json.dumps(result, indent=2) + "\n")
