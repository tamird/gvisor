"""Existing fixed-origin metadata transport, without CLI or input-tree traversal."""

import json
import os
import urllib.request

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
        body = response.read(1024 * 1024 + 1)
    assert len(body) <= 1024 * 1024, "Response exceeds bound"
    return body
