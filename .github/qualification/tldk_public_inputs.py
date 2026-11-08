"""Materialize public Bazel distfiles with the immutable registry or original receipt SHA."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import public_assets as assets


ARCHIVE = re.compile(
    r"https://github[.]com/([\w.-]+)/([\w.-]+)/archive/"
    r"((?:refs/tags/)?[\w.+-]+)[.](tar[.]gz|zip)"
)
FAILED_URL = re.compile(r"https://github[.]com/[\w./+-]+")
LIMIT = 1 << 30
MAX_FILE = 256 << 20


def components(url):
    match = ARCHIVE.fullmatch(url) or assets.URL.fullmatch(url)
    if not match or any(part in (".", "..") or len(part) > 160 for part in match.groups()):
        raise ValueError("unsupported GitHub archive or release URL")
    return match


def registry(module, version, url):
    for part in (module, version):
        if not re.fullmatch(r"[\w.+-]{1,160}", part) or part in (".", ".."):
            raise ValueError("invalid module coordinate")
    path = f"modules/{module}/{version}/source.json"
    data = json.loads(assets.fetch(f"https://raw.githubusercontent.com/bazelbuild/bazel-central-registry/{assets.REGISTRY}/{path}", 256 << 10))
    if data["url"] != url:
        raise ValueError("registry URL mismatch")
    scheme, encoded = data["integrity"].split("-", 1)
    digest = base64.b64decode(encoded, validate=True)
    if scheme != "sha256" or len(digest) != 32:
        raise ValueError("registry integrity must be SHA-256")
    return digest.hex()


def materialize(module, version, url, digest, size, asset_id, budget):
    match = components(url)
    if module:
        registered = registry(module, version, url)
        if digest and digest != registered:
            raise ValueError("original and registry digests differ")
        digest = registered
    if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        raise ValueError("missing verified original")
    limit = min(MAX_FILE, budget)
    if limit <= 0 or size is not None and not 0 < size <= limit:
        raise ValueError("archive byte budget")
    basename = url.rsplit("/", 1)[-1]
    directory = assets.DIST / "content" / digest
    dest = directory / basename
    entry = {"source": url, "sha256": digest}
    if module:
        entry.update(module=module, version=version, registry=assets.REGISTRY)
    if dest.is_file():
        blob = dest.read_bytes()
        assert len(blob) <= limit and (size is None or size == len(blob)) and hashlib.sha256(blob).hexdigest() == digest
        return directory, dict(entry, bytes=len(blob), via="existing")
    if match.re is ARCHIVE:
        owner, repo, ref, ext = match.groups()
        tag = ref.removeprefix("refs/tags/")
        if tag in (".", "..") or (ref == tag and not re.fullmatch(r"(?:[0-9a-f]{40}|v?[0-9][\w.+-]*)", tag)):
            raise ValueError("expected a release tag or full commit")
        candidates = [(f"https://codeload.github.com/{owner}/{repo}/{ext}/{ref}", False), (url, False)]
    else:
        candidates = [(url, False), (url + "?download=1", False)]
    candidates.append(("https://mirror.bazel.build/" + url.removeprefix("https://"), False))
    if asset_id and match.re is assets.URL:
        owner, repo, _, _ = match.groups()
        api = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{int(asset_id)}"
        candidates.extend([(api + "?download=1", True), (api, True)])
    for candidate, is_asset in candidates:
        blob = assets.fetch(candidate, size or limit, asset=is_asset)
        if not blob or (size is not None and len(blob) != size) or hashlib.sha256(blob).hexdigest() != digest:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            staged = Path(tmp) / basename
            staged.write_bytes(blob)
            os.replace(staged, dest)
        return directory, dict(entry, bytes=len(blob), via=candidate)
    raise ValueError("public transports returned no matching bytes")


def seed(rows):
    assert len(rows) <= 64 and len({row[2] for row in rows}) == len(rows)
    assert sum(row[4] for row in rows) <= LIMIT
    def read(row):
        try:
            return materialize(*row, row[4])
        except (OSError, ValueError, KeyError, AssertionError) as exc:
            return None, {"source": row[2], "module": row[0], "unavailable": str(exc)[:160]}
    with ThreadPoolExecutor(max_workers=4) as workers:
        yield from workers.map(read, rows)
