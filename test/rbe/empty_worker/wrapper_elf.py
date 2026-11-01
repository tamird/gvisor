"""Verify each linker wrapper as an input to an actual vDSO CppLink action."""
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import hashlib
import json
import os
import posixpath
import re
import subprocess
import zipfile

tmp = Path(os.environ["RUNNER_TEMP"])
folder = tmp / "package-evidence"
original = tmp / "original-links.zip"
assert hashlib.sha256(original.read_bytes()).hexdigest() == "f38a762fe5b640d194fbcd5b8aa8d7178d9bd269f0226d81c76afd6800ac259d"
with zipfile.ZipFile(original) as z:
    before = json.loads(z.read("result.json"))
after = json.loads((folder / "result.json").read_text())
assert before["corrected"] == "e81ef88b9338f673a9cea261f124d17bccd45e1c" and before["consumer"]["native_exit"] == 1
assert before["worker_image"] == after["worker_image"]
key = os.environ["BUILDBUDDY_API_KEY"]
cache = {}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def resource(digest):
    return digest["hash"] + "/" + str(digest["sizeBytes"])

def proto(kind, digest):
    k = kind, resource(digest)
    if k not in cache:
        run = subprocess.run([str(tmp / "bb"), "download", "--target=grpcs://remote.buildbuddy.io",
                              "--type=" + kind, "--api_key=" + key, k[1]],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45)
        if run.returncode:
            raise RuntimeError("Unable to download original " + kind + " by digest, exit " + str(run.returncode))
        cache[k] = json.loads(run.stdout)
    return cache[k]

def file_in_action(action, path):
    root = proto("Action", action)["inputRootDigest"]
    for _ in range(12):  # Follow any input-tree symlinks from the recorded action.
        parts = Path(path).parts
        cur = root
        for i, part in enumerate(parts):
            node = proto("Directory", cur)
            files = {n["name"]: n["digest"] for n in node.get("files", [])}
            dirs = {n["name"]: n["digest"] for n in node.get("directories", [])}
            links = {n["name"]: n["target"] for n in node.get("symlinks", [])}
            if part in files:
                assert i == len(parts)-1
                return files[part]
            if part in dirs:
                cur = dirs[part]
                continue
            if part in links:
                path = posixpath.normpath(posixpath.join(*parts[:i], links[part], *parts[i+1:]))
                assert not path.startswith("/") and not path.startswith("../")
                break
            raise RuntimeError("Missing recorded executable input at " + "/".join(parts[:i+1]))
        else:
            raise RuntimeError("Executable ended in a directory")
    raise RuntimeError("Too many input symlinks")

def read_elf(inv, digest):
    r = resource(digest)
    url = "https://app.buildbuddy.io/file/download?" + urlencode({
        "bytestream_url":"bytestream://remote.buildbuddy.io/blobs/" + r, "invocation_id":inv})
    req = Request(url, headers={"x-buildbuddy-api-key":key})
    with build_opener(NoRedirect()).open(req, timeout=30) as response:
        data = response.read(int(digest["sizeBytes"]) + 1)
    assert len(data) == int(digest["sizeBytes"]) and hashlib.sha256(data).hexdigest() == digest["hash"]
    path = tmp / ("wrapper-" + digest["hash"])
    path.write_bytes(data)
    output = subprocess.check_output(["readelf","-h","-lW","-dW",str(path)],text=True)
    interp = re.findall(r"Requesting program interpreter: ([^\]]+)", output)
    deps = re.findall(r"Shared library: \[([^\]]+)\]", output)
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20],"little")==183
    return dict(machine="AArch64", size=len(data), sha256=digest["hash"], interpreter=interp, needed=deps)

proof = {}
try:
    for label, doc in (("original",before),("candidate",after)):
        run = doc["consumer"]
        inv, = run["invocations"]
        rows = [a for a in run["actions"] if a["label"] == "//vdso:vdso.so" and a["mnemonic"] == "CppLink"]
        sources = [a for a in run["vdso_actions"] if a["label"] == "//vdso:vdso.so" and a["mnemonic"] == "CppLink"]
        assert len(rows) == len(sources) == 2
        evidence = proof[label] = {"source":run["source"],"invocation":inv,"actions":[]}
        elfs = {}
        for row, record in zip(rows, sources):
            exe = row["executable"]
            assert exe.endswith("/link-wrapper-clang++")
            dig = file_in_action(record["action"], exe)
            if dig["hash"] not in elfs: elfs[dig["hash"]] = read_elf(inv,dig)
            evidence["actions"].append({"action":record["action"],"executable":exe,"status":row["status"],
                                        "runner":row["runner"],"tool":elfs[dig["hash"]]})
        assert len(elfs)==1
        elf,=elfs.values()
        if label=="original": assert elf["interpreter"] and elf["needed"]
        else: assert not elf["interpreter"] and not elf["needed"]
    proof["verdict"] = "BOTH_ORIGINAL_INPUT_TREES_VERIFIED"
except Exception as err:
    proof["error"] = str(err)[:480] if isinstance(err,(AssertionError,RuntimeError,KeyError)) else type(err).__name__
    proof["verdict"] = "INCOMPLETE"
finally:
    (folder/"wrapper-elf.json").write_text(json.dumps(proof,indent=2)+"\n")
    print(proof["verdict"],proof.get("error",""))
if proof["verdict"]=="INCOMPLETE": raise SystemExit(1)
