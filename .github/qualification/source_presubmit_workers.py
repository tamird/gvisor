"""Read original actions for one native test per worker architecture and class."""

import json
import os
from pathlib import Path
import subprocess
import uuid

import exec_cas as cas

SOURCE = "a39f38e89dc7110a5bc35cd70821388d2d22f62a"
CPU = {"amd64", "arm64"}
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
SAMPLES = {
    "//pkg/atomicbitops:atomicbitops_test": ("amd64", "oci"),
    "//pkg/atomicbitops:atomicbitops_test_presubmit_arm64": ("arm64", "oci"),
    "//test/syscalls:uname_test_native": ("amd64", "firecracker"),
    "//test/syscalls:uname_test_native_presubmit_arm64": ("arm64", "firecracker"),
}


def platform_fields(message):
    p={}
    for k,v in cas.fields(message):
        if k==1:
            prop=dict(cas.fields(v))
            if 1 in prop and 2 in prop:p[prop[1].decode()]=prop[2].decode()
    return {"cpu":p.get("Arch") if p.get("Arch") in CPU else None,"linux":p.get("OSFamily")=="linux",
        "ubuntu":p.get("container-image")==IMAGE,
        "class":p.get("workload-isolation-type") if p.get("workload-isolation-type") in ("oci","firecracker") else None,
        "requested_nobody":p.get("dockerUser")=="65534:65534","nonroot_workspace":p.get("nonroot-workspace")=="true",
        "root":p.get("dockerUser")=="root","ipv6":p.get("network-enable-ipv6")=="true"}


def main():
    root = Path(os.environ["RUNNER_TEMP"])
    path = root / "source-presubmit-results" / "receipt.json"
    receipt = json.loads(path.read_bytes())
    plan = json.loads((root / "llvm-source-selection.json").read_bytes())
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    assert receipt["source"] == plan["source"] == SOURCE
    assert plan["oracle_sha256"] == "c3a872ce3dc993deafe0c9d847e44deae1b870e33ef3ea6583413c9598456f18"
    cas.INV = str(uuid.UUID(receipt["invocation"]))
    r = {"workers": {}, "diagnostics": {}}
    for label, (cpu, isolation) in SAMPLES.items():
        try:
            rows=cas.executions(label,"TestRunner")
            assert len(rows)<=15
            for e in rows:
                meta=e["actionDigest"]
                action=dict(cas.fields(cas.blob(meta,128*1024)))
                cmd=list(cas.fields(cas.blob(cas.digest_msg(action[1]),4*1024*1024)))
                args=[v.decode() for t,v in cmd if t==1]
                rep=e.get("executeResponse",{})
                if "result" not in rep:
                    continue
                out=rep["result"]
                if not (any(s.endswith("/test-setup.sh") for s in args) or
                    any(str(f.get("path","")).endswith("/test.cache_status") for f in out.get("outputFiles",[]))):
                    continue
                p=platform_fields(action[10]) if 10 in action else next((platform_fields(v) for t,v in cmd if t==5),{})
                row={"platform":p,"action_sha256":meta["hash"],"exit":out.get("exitCode",0),"rpc":rep.get("status",{}).get("code",0),
                     "cached":bool(rep.get("cachedResult")),"qemu":any("qemu-" in s for s in args)}
                row["matches"]=(p.get("cpu")==cpu and p.get("class")==isolation and (isolation!="firecracker" or p.get("root") and p.get("ipv6")) and p.get("linux") and p.get("ubuntu")
                                and not row["cached"] and not row["rpc"] and not row["exit"] and not row["qemu"])
                r["workers"].setdefault(label,[]).append(row)
        except (OSError,ValueError,KeyError,AssertionError,IndexError) as ex:
            r["diagnostics"][label]=type(ex).__name__
    r["verified"] = (r["workers"].keys() == SAMPLES.keys() and
                     all(len(rows) == 1 and rows[0]["matches"] for rows in r["workers"].values()))
    receipt["worker_samples"] = r
    path.write_text(json.dumps(receipt,sort_keys=True,separators=(",", ":"))+"\n")


if __name__ == "__main__":
    main()
