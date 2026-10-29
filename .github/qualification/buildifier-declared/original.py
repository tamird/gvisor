"""Read only the two original native logs and publish bounded diagnostics."""

from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re

KEY = os.environ.pop("ORIGINAL_READ_KEY")
IDS = {
    "first-amd64": "2f6c6a37-ed3c-4ec7-862b-1e8aaea64696",
    "corrected-arm64": "ebe9628b-afe6-471a-beb0-ad3913859100",
}
VOCAB = set("""error traceback most recent call last file line in at of the for from expected path or not directory open repository repositories regarded as invalidated package packages target targets visibility no matching toolchain toolchains found registered using resolution compatible compatible_with incompatibility executable binary rule configured buildifier native source module root src main external public default entry platform platforms exec cpu host target build constraints configuration constraint arm64 aarch64 amd64 x86_64 linux go go_sdk go_toolchain rules_go io_bazel_rules_go standard library cc compiler sdk gcc clang bzlformat tools bazeldefs buildifier_platforms linux_arm64 linux_amd64 starlark_sources bazel_tools local_config_cc go_host_compatible_sdk_label does not contain any toolchains matching a target pattern parsing failed skipped skipping analyzing analysis target buildifier check cannot no such file directory package package not found could not resolve missing fetched unsupported target cpu detected current_cc_toolchain current_cc_toolchain_incompatible_feature unsupported name code script produced by external file input directory symlink symbolic link source tree maximum length cycle dependency invalid label package packagepath use linux_arm64 crosstool_top tool go_host_compatible_sdk_label detect platform cpu constraint compatible containment list protobuf reformat load loaded loading attempted unconfigured starlark repository_rule implementation path realpath following readdir stat symlink loop prefix mkdir failed to create is an existing file exists file permissions denied source reformat duplicate key value syntax configurable attribute select ambiguous resolved for buildifier_prebuilt downloaded distribution context from java subprocess rule dependency evaluation undefined name no MODULE BUILD WORKSPACE source sources.bzl defs.bzl BUILD.bazel config_setting local_config_platform exec_compatible_with action graph restart local_config_cc_toolchains version feature flag reset target command repository_mapping .bazelrc toolchain_type no_linux_platform warning cannot execute on arm generator otherwise please invalid corrupted workspace suffix not valid outdated module running interpreter path python target pattern error while parsing top level target does not conflict internal error public visibility missing advertised providers relative to root repos path buildifierCheck""".lower().split())
report = {}
for arm, ident in IDS.items():
    page, chunks, seen = "0000", [], set()
    while page:
        if page in seen or len(seen) > 32:
            raise ValueError("invalid GetLog pagination")
        seen.add(page)
        c = HTTPSConnection("app.buildbuddy.io", timeout=30)
        c.request("POST", "/api/v1/GetLog", json.dumps({"selector":{"invocationId":ident}, "pageToken":page}), {"content-type":"application/json", "x-buildbuddy-api-key":KEY})
        with c.getresponse() as r:
            if r.status != 200:
                raise ValueError(f"GetLog HTTP {r.status}")
            data = r.read((2 << 20) + 1)
            if len(data) > 2 << 20:
                raise ValueError("oversized page")
            obj = json.loads(data)
        c.close()
        chunks.append(obj.get("log", {}).get("contents", ""))
        page = obj.get("nextPageToken", "")
    full = "".join(chunks).encode()
    diagnostics = []
    for line in full.decode().splitlines():
        if not (line.startswith("ERROR:") or "Traceback " in line or "Error in " in line or "no matching toolchains" in line.lower() or " # reformat" in line):
            continue
        # No raw log/error strings: restrict each word to a known neutral
        # Bazel vocabulary, ELF names, and numeric source locations.
        parts = []
        for token in re.findall(r"[a-zA-Z0-9_.+-]+|[:/@]", line):
            low = token.lower()
            if low in VOCAB or low in {":","/","@"} or (token.isdigit() and len(token) < 7):
                parts.append(token)
            else:
                parts.append("{" + sha256(token.encode()).hexdigest()[:8] + "}")
        diagnostics.append(" ".join(parts[:150]))
    if not full or not diagnostics:
        raise ValueError("no original diagnostic")
    report[arm] = {"invocation":ident, "bytes":len(full), "sha256":sha256(full).hexdigest(), "pages":len(seen), "diagnostics":diagnostics[:15]}
(Path(os.environ["RUNNER_TEMP"]) / "original-diagnostics.json").write_text(json.dumps(report,indent=2)+"\n")
