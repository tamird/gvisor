"""Read one immutable RSEQ linker diagnostic through the existing CAS route."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import traceback
import zipfile

from cas_inputs import blob

ROOT = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
SHA = "c12d20039f18ded628c9c94446645f63fb19a4e0595641fd777399a1d0f201e4"
SOURCE = "3e7f20cd67e5ef4b3c6715a167d5fbc9fab357aa"
OWNER = "//test/syscalls/linux/rseq:rseq"
ORIGINALS = {
    "amd64": ("0409305b-5a65-4040-8173-92038d20b979", "74502e906727bb07803b4980efdbf74c8981d6a2bf5ec9c5b7906664b22531fd"),
    "arm64": ("9201f32f-391d-494d-a327-cf552fb09742", "e53a24b5d46a10317b78d683700580ab7d22cb5b307af6afc5ec91edda02f10b"),
}


def main() -> int:
    result = {"source": SOURCE, "carrier": os.environ["GITHUB_SHA"], "status": "INCOMPLETE",
              "original_run": 42013558121, "original_artifact": 13408670826,
              "scope": "Original stderr read only; no build, test, or source execution", "actions": []}
    try:
        raw = (ROOT / "original.zip").read_bytes()
        assert len(raw) == 173029 and hashlib.sha256(raw).hexdigest() == "370eacc2134cf49e094cb933820fd72c9982a114782cf59bb554bbf08b6323d6"
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert len(archive.infolist()) == 6
            for arch, (invocation, digest) in ORIGINALS.items():
                member = archive.getinfo(arch + "-compiled.json")
                assert member.file_size < 1024 * 1024
                row = json.loads(archive.read(member))
                assert row["source"] == SOURCE and row["build"]["invocation"] == invocation
                assert row["build"]["native_exit"] == row["build"]["exit"] == 1
                failed, = row["build"]["failed_actions"]
                assert failed["owner"] == OWNER and failed["mnemonic"] == "CppLink" and failed["exit"] == 1
                assert failed["stderr"] == {"bytes": 484, "scope": "CAS_IDENTITY_ONLY", "sha256": SHA}
                action, = [a for a in row["actions"] if a["label"] == OWNER and a["mnemonic"] == "CppLink"]
                assert action["digest"] == {"hash": digest, "sizeBytes": "376"}
                assert action["exit"] == 1 and action["runner"] == "remote" and action["worker_matches"] and not action["cache"]
                result["actions"].append({"arch": arch, "invocation": invocation, "digest": action["digest"], "exit": 1})
        # Both original actions name this same immutable stderr object. One
        # authenticated read is sufficient; no action or command is replayed.
        data = blob({"hash": SHA, "sizeBytes": "484"}, ORIGINALS["amd64"][0])
        assert len(data) == 484 and hashlib.sha256(data).hexdigest() == SHA
        text = data.decode()
        libraries = re.findall(r"unable to find library -l([A-Za-z0-9_+.-]+)", text)
        allowed = {"c", "c++", "c++abi", "stdc++", "unwind", "gcc", "gcc_s", "m", "rt", "dl", "pthread", "atomic", "tcmalloc"}
        symbols = re.findall(r"undefined symbol: ([A-Za-z0-9_.$@:+-]+)", text)
        known_symbols = {"_start", "main", "__libc_start_main", "__stack_chk_fail", "malloc", "free", "memcpy", "memmove", "memset"}
        result["stderr"] = {"sha256": SHA, "bytes": len(data),
            "missing_libraries": sorted(set(libraries) & allowed),
            "unclassified_library_count": len(set(libraries) - allowed),
            "undefined_symbols": sorted(set(symbols) & known_symbols),
            "unclassified_symbol_count": len(set(symbols) - known_symbols),
            "classes": [reason for reason in ("no such file or directory", "cannot find", "unable to find library", "undefined symbol", "unknown argument", "unused during compilation", "cannot open", "permission denied", "attempted static link of dynamic object", "linker command failed") if reason in text.lower()]}
        result["status"] = "RECOVERED"
    except (AssertionError, KeyError, ValueError, OSError) as error:
        result["error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    finally:
        result["verified_at"] = datetime.now(timezone.utc).isoformat()
        destination = ROOT / "public" / "original-stderr.json"
        destination.write_text(json.dumps(result, indent=2) + "\n")
    return int(result["status"] != "RECOVERED")


if __name__ == "__main__":
    raise SystemExit(main())
