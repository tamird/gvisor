"""Prove the OCI root separately from the real foreign_cc compiler action."""
import hashlib
import io
import json
import re
import subprocess
import tarfile
import urllib.request
import uuid

from qualify import COMPILER_IMAGE, OUT, TMP, audit, now

ZERO = "docker://docker.io/tianon/scratch@sha256:3d455195f1141b05cb8cc6deb07ea96b617e5436c7a259d9f3e7b1985f4d3f51"
IMAGES = {"no_layers": (ZERO, "tianon/scratch"), "one_file": (COMPILER_IMAGE, "library/hello-world")}
PLATFORM = "//test/rbe/empty_worker:amd64_musl"


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, *args):
        nxt = super().redirect_request(req, *args)
        if nxt and nxt.host != req.host:
            nxt.remove_header("Authorization")
        return nxt


def inventory(name):
    image, repo = IMAGES[name]
    client = urllib.request.build_opener(Redirect())
    with client.open("https://auth.docker.io/token?service=registry.docker.io&scope=repository:" + repo + ":pull", timeout=30) as f:
        token = json.load(f)["token"]

    def get(part, digest):
        req = urllib.request.Request("https://registry-1.docker.io/v2/" + repo + "/" + part + "/" + digest,
                                     headers={"Authorization": "Bearer " + token, "Accept": ", ".join((
                                         "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                                         "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json"))})
        with client.open(req, timeout=30) as f:
            raw = f.read(2 << 20)
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == digest
        return raw

    digest = image.split("@")[1]
    manifest = json.loads(get("manifests", digest))
    if "manifests" in manifest:
        digest, = [m["digest"] for m in manifest["manifests"] if m["platform"]["os"] == "linux" and m["platform"]["architecture"] == "amd64"]
        manifest = json.loads(get("manifests", digest))
    config = json.loads(get("blobs", manifest["config"]["digest"]))
    assert (config["os"], config["architecture"]) == ("linux", "amd64")
    files = {}
    for layer in manifest["layers"]:
        with tarfile.open(fileobj=io.BytesIO(get("blobs", layer["digest"])), mode="r:*") as tf:
            for entry in tf:
                path = entry.name.removeprefix("./")
                if path in ("", "."):
                    assert entry.isdir()
                    continue
                assert path == "hello" and name == "one_file" and entry.isfile() and path not in files
                files[path] = hashlib.sha256(tf.extractfile(entry).read()).hexdigest()
    assert list(files) == ([] if name == "no_layers" else ["hello"])
    assert len(manifest["layers"]) == len(config["rootfs"].get("diff_ids", [])) == len(files)
    return {"image": image, "platform_manifest": digest, "config_digest": manifest["config"]["digest"],
            "rootfs": config["rootfs"], "layers": [layer["digest"] for layer in manifest["layers"]], "files": files}


def checkpoint(record):
    (OUT / "image.json").write_text(json.dumps(record, indent=2) + "\n")


def reasons(text):
    # Whitelisted provider diagnostics only; never export host mount paths, opaque IDs, or the environment.
    return sorted(set(re.findall(r"(?:mount overlayfs: (?:invalid argument|no such file or directory)|"
                                 r"(?:image|manifest) (?:has|contains) no (?:filesystem )?layers|"
                                 r"(?:no|empty) lowerdir(?:s)?(?: specified)?|"
                                 r"(?:error|failed) (?:pulling|fetching) image|"
                                 r"TOOMANYREQUESTS|MANIFEST_UNKNOWN|unsupported media type)", text, re.I)))


def run_probe(record, name, bazel, flags):
    from cas_inputs import cas, rpc
    row = record["probes"][name] = {"started": now(), "invocation": str(uuid.uuid4()), "verified": False}
    checkpoint(record)
    row["inventory"] = inventory(name)
    checkpoint(record)
    target = "//test/rbe/empty_worker:" + name
    compact = TMP / (name + ".compact")
    bep = TMP / (name + ".bep")
    cmd = [*bazel, "build", *flags, "--invocation_id=" + row["invocation"], "--noremote_accept_cached",
           "--remote_retries=0", "--execution_log_compact_file=" + str(compact), "--build_event_json_file=" + str(bep), target]
    p = subprocess.run(["timeout", "--kill-after=15s", "5m", *cmd], capture_output=True, text=True, errors="replace")
    row.update(exit=p.returncode, finished=now(), stderr_sha256=hashlib.sha256(p.stderr.encode()).hexdigest(), diagnostics=reasons(p.stderr))
    checkpoint(record)
    if p.returncode:
        try:
            executions = rpc("GetExecution", {"executionLookup": {"invocationId": row["invocation"]}, "inlineExecuteResponse": True}).get("execution", [])
            row["attempts"] = []
            for e in executions:
                if e.get("targetLabel", "").lstrip("@") != target or e.get("actionMnemonic") != "ObserveWorkerRoot":
                    continue
                item = {"digest": e["actionDigest"], "stage": e.get("stage"), "target": target, "mnemonic": "ObserveWorkerRoot"}
                row["attempts"].append(item)
                checkpoint(record)
                command = cas(cas(e["actionDigest"], "Action")["commandDigest"], "Command")
                props = {p["name"]: p["value"] for p in command["platform"]["properties"]}
                assert props == {"Arch": "amd64", "OSFamily": "linux", "container-image": IMAGES[name][0],
                                 "recycle-runner": "false", "workload-isolation-type": "oci"}
                argv = command["arguments"]
                assert "bootstrap_busybox" in argv[0] and argv[0].endswith("/busybox") and argv[1:3] == ["ash", "-c"]
                assert argv[4] == "root-probe" and argv[5].endswith("/" + name + ".txt")
                statuses = [e.get("status", {}), e.get("executeResponse", {}).get("status", {})]
                item.update(platform=props, codes=[st.get("code", 0) for st in statuses],
                            diagnostics=sorted({d for st in statuses for d in reasons(st.get("message", ""))}))
                if e.get("stage") in (4, "COMPLETED") and any(item["codes"]) and any(
                        re.search(r"mount overlayfs|no (?:filesystem )?layers|lowerdir", d, re.I) for d in item["diagnostics"]):
                    row["bound_instantiation_failure"] = True
                checkpoint(record)
        except Exception as exc:
            row["lookup_error"] = type(exc).__name__
        checkpoint(record)
        return False
    rows = audit(compact)
    action, = [a for a in rows if a["mnemonic"] == "ObserveWorkerRoot" and a["targetLabel"].lstrip("@") == target]
    row["action"] = action
    props = {p["name"]: p["value"] for p in action["platform"]}
    assert props == {"Arch": "amd64", "OSFamily": "linux", "container-image": IMAGES[name][0], "recycle-runner": "false", "workload-isolation-type": "oci"}
    assert action["runner"] == "remote" and not any(action.get(k) for k in ("cacheHit", "status", "exitCode"))
    assert any(e.get("id", {}).get("targetCompleted", {}).get("label", "").lstrip("@") == target and e.get("completed", {}).get("success")
               for e in map(json.loads, bep.read_text().splitlines()))
    from pathlib import Path
    output = Path("bazel-bin/test/rbe/empty_worker", name + ".txt").read_bytes()
    row["report_sha256"] = hashlib.sha256(output).hexdigest()
    lines = output.decode().splitlines()
    row["roots"] = [l[5:] if re.fullmatch(r"root /(?:proc|sys|dev|etc|workspace|buildbuddy-execroot|hello|tmp|bin|lib|usr|opt|workdir|\.dockerenv)", l) else "<other>" for l in lines if l.startswith("root ")]
    row["mounts"] = [l[6:] for l in lines if re.fullmatch(r"mount [a-z0-9/<>_-]+ [a-z0-9_]+", l)]
    row["tmp"] = [l[4:] for l in lines if l.startswith("tmp ")]
    standard = {"/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin", ".", "/tmp", "<unset>", ""}
    row["probe_env"] = {k: [part if part in standard else "<other>" for part in v.split(":")]
                        for l in lines if l.startswith("env ") for k, v in [l[4:].split("=", 1)] if k in ("PATH", "TMPDIR")}
    row["etc_entries"] = [l[4:] for l in lines if re.fullmatch(r"etc (?:hosts|hostname|resolv[.]conf|mtab|<other>)", l)]
    row["exec"] = [l[5:] for l in lines if l.startswith("exec ")]
    checkpoint(record)
    assert "machine x86_64" in lines and len(row["roots"]) > 0 and len(row["mounts"]) > 0
    assert row["tmp"] in (["present"], ["absent"])
    assert sorted(row["exec"]) == sorted(p + " no 127" for p in ("/bin/sh","/bin/bash","/usr/bin/cc","/usr/bin/gcc","/usr/bin/clang"))
    got = [l[10:] for l in lines if l.startswith("hello-sha ")]
    assert got == list(row["inventory"]["files"].values())
    row["verified"] = True
    checkpoint(record)
    return True


def choose(bazel, flags):
    record = {"probes": {}, "selected": None}
    for name in IMAGES:
        if run_probe(record, name, bazel, flags):
            record["selected"] = name
            checkpoint(record)
            return IMAGES[name][0]
        if name == "no_layers" and record["probes"][name].get("bound_instantiation_failure"):
            continue
        raise RuntimeError("Worker preflight incomplete; no alternate image is justified")
    raise RuntimeError("No worker preflight passed")
