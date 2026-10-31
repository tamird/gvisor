"""Read the two archive products of the original BuildBuddy action, without Bazel."""
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import hashlib
import json
import os
import subprocess
import zipfile

folder = Path(os.environ["RUNNER_TEMP"])
original = folder / "original.zip"
receipt_dir = folder / "sysmsg-recovery"
receipt_dir.mkdir()
result = {"source": "060d9517a717efe810d841722fcaf333db2e3a55",
          "original_run": 41252747744, "original_artifact": 13061881875,
          "original_artifact_sha256": "dc51aded4f35363cb1bbe520c610b60db87adfded7f5ee239efc402d1a8aa942",
          "invocation": "22d7398f-b1e9-461d-b762-5eba9ae33a37",
          "started": datetime.now(timezone.utc).isoformat(), "files": []}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

try:
    assert hashlib.sha256(original.read_bytes()).hexdigest() == result["original_artifact_sha256"]
    with zipfile.ZipFile(original) as z:
        previous = json.loads(z.read("result.json"))
        actions = json.loads(z.read("consumer/actions.json"))
    owner = "//pkg/sentry/platform/systrap/sysmsg:sysmsg"
    consumer = previous["consumer"]
    assert consumer["source"] == result["source"] and consumer["invocations"] == [result["invocation"]]
    assert consumer["native_exit"] == 0 and not consumer["failed_actions"]
    assert previous["verdict"] == "INCOMPLETE" and "FileNotFoundError" in previous["error"]
    action, = [a for a in actions if a["label"] == owner and a["mnemonic"] == "GoCompilePkg"]
    props = {p["name"]: p["value"] for p in action["platform"]["properties"]}
    assert props["Arch"] == "arm64" and props["container-image"] == previous["worker_image"]
    assert action["runner"] == "remote" and not any(action.get(k) for k in ("cacheHit", "exitCode", "status"))
    assert {Path(o["path"]).name for o in action["outputs"]} == {"sysmsg.a", "sysmsg.x"}
    result.update(native_exit=consumer["native_exit"], original_verifier=previous["verdict"],
                  action=action["action"], platform=props, original_started=consumer["started"],
                  original_finished=consumer["finished"], generator_counts=consumer["generator_counts"])
    for record in action["outputs"]:
        digest = record["digest"]
        name = Path(record["path"]).name
        resource = digest["hash"] + "/" + digest["sizeBytes"]
        url = "https://app.buildbuddy.io/file/download?" + urlencode({
            "bytestream_url": "bytestream://remote.buildbuddy.io/blobs/" + resource,
            "invocation_id": result["invocation"]})
        row = dict(name=name, expected_sha256=digest["hash"], expected_size=int(digest["sizeBytes"]))
        result["files"].append(row)
        try:
            req = Request(url, headers={"x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
            with build_opener(NoRedirect()).open(req, timeout=30) as response:
                data = response.read(row["expected_size"] + 1)
            row["method"] = "BuildBuddy authenticated file download"
        except (HTTPError, URLError) as err:
            row["http_status"] = getattr(err, "code", None)
            path = folder / name
            proc = subprocess.run([str(folder / "bb"), "download", "--target=grpcs://remote.buildbuddy.io",
                "--output_file=" + str(path), resource], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60)
            row["bytestream_exit"] = proc.returncode
            if proc.returncode:
                raise RuntimeError("Original authenticated download was unavailable")
            data = path.read_bytes()
            row["method"] = "BuildBuddy native ByteStream download"
        row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        assert row["bytes"] == row["expected_size"] and row["sha256"] == row["expected_sha256"]
    result["verdict"] = "ORIGINAL_BYTES_VERIFIED"
except Exception as err:
    result["error"] = type(err).__name__
    result["verdict"] = "INCOMPLETE"
finally:
    result["finished"] = datetime.now(timezone.utc).isoformat()
    (receipt_dir / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    print(result["verdict"])
if result["verdict"] != "ORIGINAL_BYTES_VERIFIED":
    raise SystemExit(1)
