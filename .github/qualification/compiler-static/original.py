"""Inspect only the eight previously successful invocations; submit no builds."""
import hashlib
import http.client
import json
import mmap
import os
from pathlib import Path
import re
import struct
import time
from urllib.parse import urlsplit

from check import arch, go_info, hook, image, labels, launch, out, sources

token = os.environ.pop("BUILDBUDDY_API_KEY")
inputs = json.loads(Path(__file__).with_name("originals.json").read_text())[arch]
ledger = {"arch": arch, "original_receipt_sha256": inputs["sha256"], "phases": []}
report = out / "original.json"
blobdir = out / "blobs"
blobdir.mkdir(exist_ok=True)
verified = set()


def request(path, body, cap, dest=None):
    assert path in {"/api/v1/GetInvocation", "/api/v1/GetAction", "/api/v1/GetFile",
                    "/rpc/BuildBuddyService/GetTarget", "/rpc/BuildBuddyService/GetExecution"}
    for attempt in range(3):
        conn = http.client.HTTPSConnection("app.buildbuddy.io", timeout=150)
        try:
            conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "x-buildbuddy-api-key": token})
            with conn.getresponse() as response:
                if response.status != 200:
                    if response.status in [429, 502, 503, 504] and attempt < 2:
                        continue
                    raise ValueError(f"{path} HTTP {response.status}")
                if dest is None:
                    data = response.read(cap + 1)
                    assert len(data) <= cap, "response cap"
                    return json.loads(data)
                h = hashlib.sha256()
                n = 0
                with dest.open("wb") as f:
                    while data := response.read(1 << 20):
                        n += len(data)
                        assert n <= cap, "file cap"
                        h.update(data)
                        f.write(data)
                return h.hexdigest(), n
        except (OSError, http.client.HTTPException):
            if attempt == 2:
                raise
        finally:
            conn.close()
    raise ValueError("retry limit")


def identity(uri):
    u = urlsplit(uri)
    assert u.scheme == "bytestream" and u.hostname == "remote.buildbuddy.io", "original URI authority"
    m = re.fullmatch(r"(?:/[^/]+)*/blobs/([a-f0-9]{64})/(\d+)", u.path)
    assert m, "original URI digest"
    return m[1], int(m[2])


def actual_file(uri):
    key, size = identity(uri)
    assert 0 < size <= 192 << 20
    path = blobdir / key
    if (key,size) not in verified:
        pending = path.with_suffix(".part")
        assert request("/api/v1/GetFile", {"uri": uri}, size, pending) == (key, size), "GetFile integrity"
        pending.chmod(0o500)
        pending.replace(path)
        verified.add((key,size))
    return path


def public(iid, label):
    raw = request("/rpc/BuildBuddyService/GetTarget", {"invocationId": iid, "targetLabel": label}, 16 << 20)
    groups = raw.get("targetGroups", raw.get("targetGroup", []))
    files = [f for group in groups for row in group.get("targets", [])
             if row["metadata"]["label"] == label for f in row.get("files", [])]
    if not files:
        raw = request("/api/v1/GetAction", {"selector": {"invocation_id": iid, "target_label": label}}, 16 << 20)
        files = [f for row in raw.get("action", []) for f in row.get("file", [])]
    return files


def funcnames(path):
    # ELF64 little-endian program already verified by image(). Go 1.27 keeps
    # the function table even when the normal ELF symbol table is stripped.
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        shoff = struct.unpack_from("<Q",raw,40)[0]
        entsize, nsection, strs = struct.unpack_from("<HHH", raw,58)
        def section(i):
            return struct.unpack_from("<IIQQQQ",raw,shoff+i*entsize)
        names_off = section(strs)[4]
        for i in range(nsection):
            name, _, _, _, off, size = section(i)
            nameoff = names_off+name
            if raw[nameoff:raw.index(b"\0", nameoff)] == b".gopclntab":
                break
        else:
            return {"format": "section not found"}
        if raw[off:off+8] != b"\xf1\xff\xff\xff\x00\x00\x01\x08" and not (
            raw[off:off+4] == b"\xf1\xff\xff\xff" and raw[off+7]==8
        ):
            return {"format": "unexpected pclntab header", "header":raw[off:off+8].hex()}
        nfunc, _, _, funcs, _, _, _, pcln = struct.unpack_from("<8Q",raw,off+8)
        assert 0 < nfunc < 200000 and pcln < size and funcs < size
        representatives = set()
        for i in range(nfunc):
            _, rel = struct.unpack_from("<II", raw, off+pcln+8*i)
            n = struct.unpack_from("<i", raw, off+pcln+rel+4)[0]
            assert 0 <= n < size - funcs
            start=off+funcs+n
            name=raw[start:raw.index(b"\0",start)].decode()
            short = name.removesuffix(".abi0")
            if short in {"runtime/cgo.set_crosscall2","runtime.racefuncenter","runtime.racefuncexit",
                         "runtime.raceReadObjectPC","runtime.RaceRead","net._Cfunc_getaddrinfo","runtime.racefini"}:
                representatives.add(name)
        return {"format":"go120","function_count":nfunc,"witness":sorted(representatives)}


def executions(iid, label, sha, size):
    r = request("/rpc/BuildBuddyService/GetExecution",
                {"executionLookup": {"invocationId": iid, "targetLabel": label}, "inlineExecuteResponse": True}, 32 << 20)
    rows = []
    for e in r.get("execution", []):
        resp = e.get("executeResponse", {})
        res = resp.get("result", {})
        for f in res.get("outputFiles", []):
            d = f.get("digest", {})
            if d.get("hash") == sha and int(d.get("sizeBytes",-1)) == size:
                rows.append({"owner": e.get("targetLabel"),"mnemonic": e.get("actionMnemonic"),
                             "worker": bool(res.get("executionMetadata",{}).get("worker") or e.get("executedActionMetadata",{}).get("worker")),
                             "exit": int(res.get("exitCode",0)), "status": int(resp.get("status",{}).get("code",0)),
                             "cached":resp.get("cachedResult",False),"path":f.get("path"),"action":e.get("actionDigest")})
    return {"queried":label,"executions":len(r.get("execution",[])),"equal_outputs":rows}


def one(iid, arm, mode, original, label):
    files = public(iid, label)
    name = label.split(":")[-1]
    known = original["outputs"].get(label)
    match = []
    for f in files:
        uri = f.get("uri","")
        try:
            ident=identity(uri)
        except AssertionError:
            continue
        if known:
            if ident != (known["sha256"],known["bytes"]):
                continue
        elif Path(f.get("name","")).name != name:
            continue
        match.append((uri, f.get("name"),ident))
    # The only default file on this frontend is the transitioned binary
    # (frontend.bzl exports target[DefaultInfo].files). Some RPCs return it twice.
    if len({x[2] for x in match}) != 1:
        raise ValueError(f"{len(match)} matched of {len(files)} files: {[f.get('name','')[:160] for f in files][:6]}")
    uri, filename, (sha,size) = match[0]
    p = actual_file(uri)
    result = {"native_output":filename,"sha256":sha,"bytes":size,"known":bool(known),**image(p)}
    if label == hook:
        return result
    result["go_info"] = go_info(p)
    result["pclntab"] = funcnames(p)
    if arm == "explicit" and not known:
        owner = label+"_"+"/"+name
        try:
            result["link"]=executions(iid,owner,sha,size)
        except (KeyError, ValueError, OSError, http.client.HTTPException, AssertionError) as e:
            result["link_error"]=str(e)[:300]
    if label == labels[0]:
        result["startup"] = launch(p,sha)
    return result


def main():
    ok = True
    for orig in inputs["phases"]:
        iid, arm, mode = (orig[k] for k in ["invocation","setting","mode"])
        entry = {"source":orig["source"],"setting":arm,"mode":mode,"invocation":iid,"outputs":{},"errors":{}}
        ledger["phases"].append(entry)
        assert sources[arm]==orig["source"] and orig["native_exit"]==0
        [native] = request("/api/v1/GetInvocation",{"selector":{"invocation_id":iid}},1<<20)["invocation"]
        assert native["id"]["invocationId"]==iid and native.get("success") and native.get("command")=="build","original invocation"
        entry["recorded_commit"]=native.get("commitSha")
        entry["native_success"]=True
        for label in [*labels, hook]:
            try:
                row=one(iid,arm,mode,orig,label)
                entry["outputs"][label]=row
                if label != hook:
                    assert row["pclntab"]["format"]=="go120", "missing original Go function table"
                if label == labels[0]:
                    s=row["startup"]
                    empty=s["empty_chroot"]
                    assert s["ordinary"]["exit"]==0 and s["ordinary"]["pcap_header"]
                    if not row["interpreter"]:
                        assert empty.get("exit")==0 and empty["pcap_header"],"static empty-root launch"
                    else:
                        assert empty.get("exec_errno")==2,"missing dynamic interpreter"
            except (KeyError, ValueError, OSError, http.client.HTTPException, AssertionError, RuntimeError) as exc:
                entry["errors"][label]=f"{type(exc).__name__}: {exc}"[:500]
                ok=False
            report.write_text(json.dumps(ledger,sort_keys=True,indent=2)+"\n")
        if hook in entry["outputs"] and labels[2] in entry["outputs"]:
            h,s = (entry["outputs"][l]["sha256"] for l in [hook,labels[2]])
            with (blobdir/s).open("rb") as stream, mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as raw:
                entry["hook_offset"]=raw.find((blobdir/h).read_bytes())
            if entry["hook_offset"]<0:
                ok=False
    return ok


if __name__=="__main__":
    started=time.time()
    try:
        status=main()
        ledger["status"]="PASS" if status else "FAIL"
        if not status:
            raise SystemExit(1)
    finally:
        ledger["duration_s"]=time.time()-started
        report.write_text(json.dumps(ledger,sort_keys=True,indent=2)+"\n")
