"""Read only the four immutable boot failure logs; do not execute source."""

import hashlib
import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from cas_inputs import fetch

ROOT = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
SOURCE = "cc47a1a5856bbfc2c42cf52add1aebf49eb509d5"
ZIP_SHA = "4c129e177c233c650b7c20a6671a1b385d2ec924a74d853c0dad0110cf550025"
OWNER = "//runsc/boot:boot_test"
EXPECTED = {
    "amd64-boot-named": ("e90d653d-0f74-4580-af04-bb785d4ff86e", "1c6056b1050dbd0be8be9016da36bc82717b02b558177510f22c4ef87e65e9cc", 4684),
    "arm64-boot-named": ("c5d3592b-0db6-4919-970e-5a87e89858f3", "9ee9f1fd6e6a11f998439789e0bfc4245f7bb7b98f1b5d66957bca50d8e795b9", 4806),
    "amd64-boot-full": ("d56baf58-c4cb-4cc2-80d9-4db435185085", "3a39b0ad33983d6a09dc1b861bbcb9b9c912cf428d1d2ed677e13f07f4b6d267", 23115),
    "arm64-boot-full": ("4c13cf42-d43a-41ca-b83a-4292b8cb3e04", "3ec58143fffb29c45e97983f5aeec1a8ca0eb77ed344e6963e35b720ab6ca341", 23230),
}


def project(raw):
    lines = []
    source_files = r"(?:loader|vfs|lifecyclecpus|fscheckpoint|mount_hints|restore|compat|seccomp)_test[.]go"
    for line in raw.decode("utf-8", errors="replace").splitlines():
        match = re.fullmatch(r"\s*(" + source_files + r"):(\d+):\s+(.*)", line)
        if not match:
            continue
        message = re.sub(r"https?://\S+", "<url>", match[3])
        message = re.sub(r"/(?:buildbuddy-execroot|tmp|home|__w)/[^\s\"']+", "<worker-path>", message)
        message = re.sub(r"0x[0-9a-fA-F]{8,}", "<address>", message)
        lines.append({"file": match[1], "line": int(match[2]), "message": message[:512]})
    return {"source_lines": lines[:24], "source_line_count": len(lines)}


def main():
    output = {"source": SOURCE, "original_artifact": 13422210533, "original_zip_sha256": ZIP_SHA,
              "scope": "original test.log reads only; zero builds or tests", "logs": {}, "status": "INCOMPLETE"}
    destination = ROOT / "public" / "original-boot-logs.json"

    def save():
        destination.write_text(json.dumps(output, indent=2) + "\n")

    save()
    try:
        archive = (ROOT / "original.zip").read_bytes()
        assert len(archive) == 21514 and hashlib.sha256(archive).hexdigest() == ZIP_SHA
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            for name, (invocation, digest, size) in EXPECTED.items():
                row = output["logs"][name] = {"invocation": invocation, "sha256": digest, "bytes": size, "status": "INCOMPLETE"}
                save()
                try:
                    info = z.getinfo(name + ".json")
                    assert info.file_size <= 65536
                    original = json.loads(z.read(info))
                    assert original["source"] == SOURCE and original["requested_invocation"] == invocation
                    assert original["native_exit"] == original["events"]["exit"] == 3
                    target = original["targets"][OWNER]
                    result, = target["results"]
                    assert target["fresh_native"] and result["status"] == "FAILED"
                    assert result["failure_log"]["sha256"] == digest and result["failure_log"]["bytes"] == size
                    response = json.loads(fetch("api/v1/GetAction", {"selector": {"invocation_id": invocation, "target_label": OWNER}}))
                    assert not response.get("nextPageToken", response.get("next_page_token"))
                    action, = [a for a in response["action"] if any(f["name"] == "test.log" for f in a.get("file", []))]
                    assert action.get("targetLabel", action.get("target_label")) == OWNER
                    assert action["id"].get("invocationId", action["id"].get("invocation_id")) == invocation
                    assert all(int(action[k]) == 1 for k in ("run", "shard", "attempt"))
                    file, = [f for f in action["file"] if f["name"] == "test.log"]
                    match = re.fullmatch(r"bytestream://remote[.]buildbuddy[.]io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/([0-9]+)", file["uri"])
                    assert match and match[1] == digest and int(match[2]) == size
                    raw = fetch("file/download?" + urlencode({"bytestream_url": file["uri"], "invocation_id": invocation}))
                    assert len(raw) == size and hashlib.sha256(raw).hexdigest() == digest
                    row.update(project(raw), status="READ_VERIFIED")
                except (OSError, AssertionError, ValueError, KeyError) as error:
                    row["error_type"] = type(error).__name__
                save()
        if all(row["status"] == "READ_VERIFIED" for row in output["logs"].values()):
            output["status"] = "READ_VERIFIED"
    except (OSError, AssertionError, ValueError, KeyError, zipfile.BadZipFile) as error:
        output["error_type"] = type(error).__name__
    output["verified_at"] = datetime.now(timezone.utc).isoformat()
    save()


if __name__ == "__main__":
    main()
