"""Classify the original checksum test's TLS failure without repeating any download."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.request import Request, build_opener, HTTPRedirectHandler

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None

inv = "b290902a-b0de-4d40-aa10-47d44617bfd5"
sha = "e5a2bcced9e94074191486ad5d0e4780e82756113af74b27691e6661a004fb74"
size = 36796
url = f"https://remote.buildbuddy.io/file/download?invocation_id={inv}&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F{sha}%2F{size}"
key = os.environ.pop("BUILDBUDDY_API_KEY")
with build_opener(NoRedirect).open(Request(url,headers={"x-buildbuddy-api-key":key}),timeout=45) as res:
    data = res.read(size+1)
assert len(data)==size and hashlib.sha256(data).hexdigest()==sha
reasons = {}
for line in data.decode(errors="replace").splitlines():
    text = line.lower()
    if not any(x in text for x in ("checksum", "failed to download", "download.nvidia", "tls:")):
        continue
    for cause in ("certificate signed by unknown authority","tls handshake timeout","connection refused","network is unreachable","no such host","i/o timeout","certificate has expired","handshake failure","certificate is valid for","certificate verify failed","connection reset by peer","tls: internal error","context deadline exceeded"):
        if cause in text:
            reasons[cause] = reasons.get(cause,0)+1
    if ("tls" in text or "certificate" in text) and not any(c in text for c in reasons):
        reasons["other TLS error"] = reasons.get("other TLS error",0)+1
(Path(os.environ["RUNNER_TEMP"])/"original-nvidia-tls.json").write_text(json.dumps({"invocation":inv,"sha256":sha,"bytes":size,"causes":reasons}))
