#!/usr/bin/env python3
"""Read only the original sidecar identities and the failed ARM fetch log."""

import base64
import hashlib
import io
import json
import re
import traceback
import urllib.error
from urllib.parse import urlsplit
import zipfile

from cas_inputs import rpc
import full
import qualify as q


def sidecars() -> dict:
    original = full.partial()
    raw = (q.TMP / "release.zip").read_bytes()
    assert len(raw) == 6330 and hashlib.sha256(raw).hexdigest() == "58bdc86cb8651fe3abe87a7ac7f2b8127766f60c4fb5a80799966e9053610dcc"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert len(archive.infolist()) == 4 and archive.getinfo("amd64.json").file_size == 15660
        previous = json.loads(archive.read("amd64.json"))
    assert previous["source"] == "5388293bdd842102babe313ae951489e78a6dfae" and previous["status"] == "PASS"
    assert {r["owner"] for r in original["output_check_errors"]} == set(full.C_LINKS)
    result = {}
    for owner, public in full.C_LINKS.items():
        action, = [r for r in original["actions"] if r["label"] == owner]
        assert action["mnemonic"] == "CppLink" and action["runner"] == "remote" and not action["cache"]
        assert action["worker_matches"] and action["exit"] == 0 and not action["status_nonempty"]
        assert action["executable"] == "external/llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-amd64/bin/clang++"
        output, = action["outputs"]
        name = full.SIDECARS[public]
        assert output["path"].endswith("/bin/" + owner[2:].replace(":", "/"))
        evidence, = [r for r in original["build"]["link_results"] if r["owner"] == owner and r["output"] == output["path"]]
        assert evidence["success"] and evidence["exit"] == 0 and original["build"]["configurations"][evidence["configuration"]] == "k8"
        binary = previous["binaries"]["gvisor-bin/" + name]
        assert binary["sha256"] == output["digest"]["hash"] and binary["bytes"] == int(output["digest"]["sizeBytes"])
        assert binary["machine"] == 62 and binary["load"] and binary["entry"] and not binary["interp"] and not binary["dynamic"]
        if public != "//runsc/fdparking:fdparking_binary":
            assert binary["bytes"] <= 4096
        result[owner] = binary | {"scope": "ORIGINAL_FULL_BYTES_JOIN", "prior_byte_artifact": 13391208856,
                                  "current_action_artifact": 13402650978, "configuration": evidence["configuration"]}
    return {"status": "VERIFIED_IDENTITY_JOIN", "binaries": result,
            "preserved_original_reader_errors": original["output_check_errors"],
            "scope": "Three original fresh links joined to prior full-byte reads; no rebuild or new ELF read"}


def arm_fetch() -> dict:
    # The SHA binds this sole original UUID and its pre-configuration failure.
    full.partial()
    with zipfile.ZipFile(q.TMP / "partial.zip") as archive:
        assert archive.getinfo("arm64.json").file_size == 62070
        original = json.loads(archive.read("arm64.json"))
    invocation = "84e55ceb-c11f-4168-b7c7-0d6c0bcc1ee1"
    assert original["source"] == "c43bcb023757ee2a2d31e656ac495a27cb29e3be"
    assert original["build"]["invocation"] == invocation and original["build"]["native_exit"] == 1
    assert original["coverage"]["configured"] == 0 and not original.get("actions")
    # v2.322.0 proto/eventlog.proto: an omitted chunk_id reads the last chunk.
    # Only one bounded original chunk is read; no paging or live log stream.
    reply = rpc("GetEventLogChunk", {"invocationId": invocation, "type": "BUILD_LOG"})
    assert not reply.get("live")
    encoded = reply.get("buffer", "")
    assert len(encoded) <= 3 * 1024 * 1024
    raw = base64.b64decode(encoded, validate=True)
    assert len(raw) <= 2 * 1024 * 1024
    classes, repositories, domains, codes = set(), set(), set(), set()
    reasons = ("failed to fetch", "no such package", "error downloading", "checksum", "timed out",
               "connection reset", "connection refused", "certificate", "pkix", "not found", "no space left",
               "permission denied", "unable to find valid certification", "could not resolve", "unknown host")
    allowed_domains = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                       "mirror.bazel.build", "bcr.bazel.build", "go.dev", "dl.google.com", "storage.googleapis.com"}
    for line in raw.decode(errors="replace").splitlines():
        if not any(word in line.lower() for word in ("error", "failed", "fetch", "download", "no such package", "caused by")):
            continue
        classes.update(word for word in reasons if word in line.lower())
        for name in re.findall(r"(?:repository ['\"]|no such package ['\"]@@?)([A-Za-z0-9_+.-]{1,200})(?:['\"]|//)", line):
            repositories.add(name)
        for url in re.findall(r"https?://[^\s'\"]+", line):
            if (domain := urlsplit(url).hostname) in allowed_domains:
                domains.add(domain)
        codes.update(re.findall(r"(?:HTTP|response code|status code)[: ]+([1-5][0-9][0-9])", line))
    return {"status": "ORIGINAL_LOG_PROJECTED", "invocation": invocation, "native_exit": 1,
            "configured": 0, "source": original["source"], "classes": sorted(classes),
            "repositories": sorted(repositories)[:12], "public_domains": sorted(domains),
            "http_codes": sorted(codes), "provider_detail_retained": bool(repositories),
            "scope": "One original final log chunk; no raw text, URL, environment or source logs published"}


def main() -> int:
    report = {"source_artifact": 13402650978, "source_zip_sha256": full.PARTIAL_SHA,
              "scope": "Independent original-only recovery; does not determine the new build verdict"}
    for name, operation in (("sidecars", sidecars), ("arm_fetch", arm_fetch)):
        try:
            report[name] = operation()
        except (AssertionError, KeyError, ValueError, OSError, urllib.error.URLError, zipfile.BadZipFile) as error:
            report[name] = {"status": "UNAVAILABLE", "type": type(error).__name__,
                            "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    report["verified_at"] = q.now()
    q.save("original-runsc", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
