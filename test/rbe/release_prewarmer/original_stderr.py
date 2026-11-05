"""Read the first compiler errors from four completed seccheck CppCompile actions."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
import zipfile

from cas_inputs import fetch

ROOT = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
ARCHES = {"amd64": "17efbeba-7def-406e-aebf-a2f5fe2df4d0", "arm64": "66ffbadd-2836-44bc-a4a3-7a93fe4d979c"}
OWNERS = ("//examples/seccheck:server_cc", "//pkg/sentry/seccheck/points:points_proto")
SOURCE = "7dcefe2b25074371960c9a1372f2423eaece2694"
ORIGINAL = "a71aca92d3cf0f87cc0a9e8082a1dab45d21d8b85cbcd43a07faf1228d690777"
evidence = {"source": SOURCE, "run": 42028439038, "artifact": 13416191933, "sha256": ORIGINAL, "arches": {}}


def save():
    (ROOT / "public/original-stderr.json").write_text(json.dumps(evidence) + "\n")


PROOF_SHA = "bbee035bffda780f1e218b02637490c1af1d449e861ec5a2af945b1b4143c8ac"
WORDS = ("error", "fatal", "warning", "note", "clang++", "clang", "lld", "compiler", "wrapper", "preprocessor",
         "file not found", "no such file", "not found", "cannot find", "unsupported", "unrecognized",
         "undeclared", "unknown", "undefined", "invalid", "incompatible", "candidate", "required", "missing",
         "unable", "failed", "exception", "killed", "permission denied", "segmentation fault",
         "musl", "protobuf", "absl", "cstdlib", "include", "static assertion", "linker")
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
QUOTE = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "})
LABEL = re.compile(r"(?<![\w#])((?:fatal |internal compiler )?error|fatal|exception|panic):\s*(.+)$", re.I)
DRIVER = re.compile(r"^\s*((?:[^\s:]+/)?(?:clang(?:\+\+)?(?:-[0-9]+)?|cc1plus|cc1|ld(?:[.]lld)?|lld|collect2|cc-wrapper|compiler-wrapper|bazel-out/[^ ]*/cc_wrapper)[^:]*):\s*(.+)$", re.I)
FAILURE = re.compile(r"\b(?:killed|segmentation fault|permission denied|not found|not supported|unable to|cannot |failed to|terminated|assertion failed)\b", re.I)


def projection(raw):
    text = raw.decode(errors="replace")
    norm, n_osc = OSC.subn("", text)
    norm, n_csi = CSI.subn("", norm)
    norm = norm.translate(QUOTE)
    errors = []
    shape = {"lines": 0, "csi": n_csi, "osc": n_osc, "colon_error": 0, "driver": 0, "rejected": 0}
    # These vocabulary tokens reveal the shape of an unfamiliar diagnostic
    # without publishing the original command, source excerpt or stderr.
    tokens = []
    for line in norm.splitlines():
        shape["lines"] += 1
        low = line.lower()
        for word in sorted((w for w in WORDS if w in low and w not in tokens), key=low.index):
            if len(tokens) < 14:
                tokens.append(word)
        match = LABEL.search(line)
        if match:
            mode, prefix, msg = "colon_error", match[1].lower(), match[2]
        else:
            match = DRIVER.match(line)
            if not match or not FAILURE.search(match[2]):
                continue
            mode, prefix, msg = "driver", "compiler driver", match[2]
        shape[mode] += 1
        # Ignore source-code and command echoes even if they include "error:".
        if (len(line) > 1500 or not msg.isascii() or not msg.isprintable() or
                re.search(r"(?i)https?://|bearer |authorization|api[_-]?key|token[ =:]|x-amz|remote_header|bes_header", msg) or
                re.match(r"\s*(?:\d+\s*\||\^|\||#(?:define|error))", line)):
            shape["rejected"] += 1
            continue
        msg = re.sub(r"(?<![A-Za-z0-9_./-])/(?:[^\s'\"<>;:,)]+)", "<path>", msg)
        msg = re.sub(r"[A-Za-z0-9_+/=-]{40,}", "<identifier>", msg)[:340]
        entry = prefix + ": " + msg
        if entry not in errors and len(errors) < 3:
            errors.append(entry)
    return {"errors": errors, "shape": shape, "first_known_tokens": tokens}


def read(invocation, previous, row):
    sha, size = previous["sha256"], previous["bytes"]
    assert 0 < size <= 131072 and re.fullmatch(r"[0-9a-f]{64}", sha)
    assert previous["invocation"] == invocation and previous["exit"] == 1 and previous["status"] == "NO_COMPILER_MESSAGE"
    assert re.fullmatch(r"[0-9a-f]{64}", previous["action_sha256"])
    resource = "bytestream://remote.buildbuddy.io/blobs/" + sha + "/" + str(size)
    raw = fetch("file/download?" + urlencode({"invocation_id": invocation, "bytestream_url": resource}))
    assert len(raw) == size and hashlib.sha256(raw).hexdigest() == sha
    row.update(action_sha256=previous["action_sha256"], bytes=size, sha256=sha, exit=1, **projection(raw))
    row["status"] = "READ" if row["errors"] else "NO_COMPILER_MESSAGE"


def main():
    save()
    path = ROOT / "original.zip"
    assert path.stat().st_size == 4192 and hashlib.sha256(path.read_bytes()).hexdigest() == ORIGINAL
    proof_path = ROOT / "stderr-identities.zip"
    assert proof_path.stat().st_size == 841 and hashlib.sha256(proof_path.read_bytes()).hexdigest() == PROOF_SHA
    with zipfile.ZipFile(proof_path) as z:
        assert z.namelist() == ["original-stderr.json"]
        proof = json.loads(z.read("original-stderr.json"))
    assert proof["source"] == SOURCE and proof["artifact"] == 13416191933 and proof["sha256"] == ORIGINAL
    assert set(proof["arches"]) == set(ARCHES) and all(set(rows) == set(OWNERS) for rows in proof["arches"].values())
    evidence.update(identity_artifact=13415703414, identity_zip_sha256=PROOF_SHA)
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {"amd64.json", "arm64.json", "gnu.json", "verdict.json"}
        for arch, invocation in ARCHES.items():
            old = json.loads(archive.read(arch + ".json"))
            assert old["source"] == SOURCE and old["requested_invocation"] == old["events"]["invocation"] == invocation
            assert old["native_exit"] == old["events"]["exit"] == 1 and old["outcomes"] == {"NOT_EXECUTED": 1}
            assert Counter((r["type"], r["owner"]) for r in old["events"]["failures"]) == Counter(("CppCompile", o) for o in OWNERS)
            rows = evidence["arches"][arch] = {}
            for owner in OWNERS:
                row = rows[owner] = {"invocation": invocation, "status": "INCOMPLETE"}
                try:
                    read(invocation, proof["arches"][arch][owner], row)
                except (AssertionError, OSError, KeyError, TypeError, ValueError) as err:
                    row.update(error=type(err).__name__, http=getattr(err, "code", None))
                save()
    good = all(row["status"] == "READ" for rows in evidence["arches"].values() for row in rows.values())
    evidence["complete"] = good
    save()
    return good


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
