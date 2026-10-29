"""Retain only the selected Make build owners and public artifact identities."""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

LABEL = "release-20261029.0"
RELEASE = "//debian:release-artifacts"
EXPECTED = {"//tools/bazeldefs:version", "//sandboxexec/sandbox/python:sandbox_py", RELEASE}


def event_row(path):
    ids = {}
    targets = {}
    options = {}
    row = {}
    for line in path.read_text().splitlines():
        if not line:
            continue
        event = json.loads(line)
        identity = event.get("id", {})
        if "started" in event:
            row.update(invocation=event["started"]["uuid"], start=event["started"].get("startTime"))
        if "finished" in event:
            code = event["finished"]["exitCode"]
            row.update(exit=code.get("code", 0 if code.get("name") == "SUCCESS" else None),
                       status=code.get("name"), finish=event["finished"].get("finishTime"))
        if "structuredCommandLine" in event:
            for section in event["structuredCommandLine"].get("sections", []):
                for opt in section.get("optionList", {}).get("option", []):
                    name = opt.get("optionName")
                    if name in ("compilation_mode", "embed_label", "stamp"):
                        options[name] = opt.get("optionValue")
        if "namedSetOfFiles" in identity:
            ids[identity["namedSetOfFiles"]["id"]] = event["namedSetOfFiles"]
        if "targetCompleted" in identity and identity["targetCompleted"]["label"] in EXPECTED:
            item = event["completed"]
            if item.get("success") and not identity["targetCompleted"].get("aspect"):
                label = identity["targetCompleted"]["label"]
                targets[label] = [s["id"] for g in item.get("outputGroup", []) if g.get("name") == "default" for s in g.get("fileSets", [])]
    row["targets"] = sorted(targets)
    row["options"] = options
    outputs = {}
    pending = list(targets.get(RELEASE, []))
    seen = set()
    while pending:
        cur = pending.pop()
        if cur in seen:
            continue
        seen.add(cur)
        group = ids[cur]
        pending.extend(s["id"] for s in group.get("fileSets", []))
        for output in group.get("files", []):
            # BEP file.name may be joined to pathPrefix; use the full staged
            # suffix so a transitive source or auxiliary file cannot match.
            name = "/".join([*output.get("pathPrefix", []), output["name"]])
            match = re.search(r"(?:^|/)debian/release-artifacts/(x86_64|aarch64)/(runsc[.]deb|gvisor[.]tar[.](?:zstd|bz2))$", name)
            if not match:
                continue
            key = "/".join(match.groups())
            uri = re.search(r"/blobs/([0-9a-f]{64})/(\d+)$", unquote(output.get("uri", "")))
            digest = output.get("digest")
            length = output.get("length")
            if uri:
                assert digest in (None, "", uri[1]) and length in (None, int(uri[2]), uri[2])
                digest, length = uri[1], uri[2]
            assert key not in outputs
            outputs[key] = [int(length) if length is not None else None, digest]
    if RELEASE in targets:
        row["outputs"] = outputs
    return row


def main():
    root, rc = Path(sys.argv[1]), int(sys.argv[2])
    out = root / "result"
    rows = [event_row(p) for p in sorted((root / "native").glob("build-*"))]
    report = {"product": "0ac3fa2856b3e5f918b9e32ca8a345f7d7930bd5", "label": LABEL,
              "shell_exit": rc, "native": rows, "observed_at": datetime.now(timezone.utc).isoformat()}
    # Keep native owners even if an ancillary runtime or publication check fails.
    (out / "native.json").write_text(json.dumps(report, indent=2) + "\n")
    if rc:
        stages = {}
        classes = {"bazel": r"^ERROR:", "make": r"^make: [*][*][*]", "gpg": r"^gpg: (?:can't|error)",
                   "missing_file": r"No such file|FileNotFoundError", "assertion": r"AssertionError",
                   "permission": r"Permission denied", "missing_command": r"command not found"}
        for name in ("prior-opt", "fastbuild", "release", "packages", "signature"):
            log = root / (name + ".log")
            if log.exists():
                content = log.read_bytes()
                stages[name] = sorted(k for k, pat in classes.items() if re.search(pat.encode(), content, re.M | re.I))
        (out / "failure-classes.json").write_text(json.dumps(stages, indent=2) + "\n")
        return
    assert len(rows) == 3 and all(r["exit"] == 0 and r["status"] == "SUCCESS" and r["finish"] for r in rows)
    assert sorted(t for row in rows for t in row["targets"]) == sorted(EXPECTED)
    rel, = [r for r in rows if RELEASE in r["targets"]]
    assert rel["options"]["embed_label"] == LABEL and rel["options"]["compilation_mode"] == "opt"
    packages = json.loads((out / "packages.json").read_text())
    outputs = {f"{arch}/{name}": val for arch, pkg in packages.items() for name, val in pkg["outputs"].items()}
    assert len(outputs) == 6 and rel["outputs"] == outputs, (rel["outputs"], outputs)
    assert len((out / "bin-links.txt").read_text().splitlines()) == 3
    assert (out / "publication.txt").stat().st_size and all((out / f"version-{a}.txt").stat().st_size for a in packages)
    (out / "verified-at.txt").write_text(datetime.now(timezone.utc).isoformat() + "\n")


if __name__ == "__main__":
    main()
