"""Read raw PacketImpact captures and primary actions on common Ubuntu VMs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
import uuid

import frames

WORK = Path(os.environ["RUNNER_TEMP"]) / "packet-worker"
OUT = WORK / "results"
BASE = "61ff148f0df0efa29c9f500914477525c5debe62"
FIX = "43fdbb717c1b94352a5d0c12a0cb6fabad327d4f"
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
TEST = "tcp_handshake_window_size"
PREFIX = "//test/packetimpact/tests:" + TEST
PLATFORM = "//test/rbe/packet_worker:amd64"
RC = WORK / "remote.bazelrc"
REPORT = {"verified": False, "stock": BASE, "fixed": FIX, "public_product": "1c0f505419145e1e550481a3cd98b9a20cf7a496",
          "worker_image": IMAGE, "cells": {}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def publish(name, data):
    assert Path(name).name == name and name != "remote.bazelrc"
    assert not re.search(br"(?i)x-buildbuddy-api-key|authorization\s*[:=]", data)
    key, = re.findall(br"^build --remote_header=x-buildbuddy-api-key=(.+)$", RC.read_bytes(), re.MULTILINE)
    assert key and key not in data
    (OUT / name).write_bytes(data)


def save():
    publish("result.json", (json.dumps(REPORT, indent=2, sort_keys=True) + "\n").encode())


def error_at(exc):
    tb = exc.__traceback__
    while tb.tb_next:
        tb = tb.tb_next
    return {"type": type(exc).__name__, "line": tb.tb_lineno, "file": Path(tb.tb_frame.f_code.co_filename).name}


def label(text):
    return text.removeprefix("@@")


def physical(directory, cell, owners, negative):
    # Project the compact log before recording it. Never emit a complete
    # action environment, command line, BEP, or authenticated Bazel console.
    projection = r'''select(.spawn != null) | .spawn | select(.mnemonic == "TestRunner" or
         ((.mnemonic=="CppCompile" or .mnemonic=="CppLink") and ((.targetLabel // "")|endswith("//test/packetimpact/dut:posix_server")))) |
        [.mnemonic,.targetLabel,.args[0],(.args[1:]|map(select(endswith("-bench")))|first),.runner,
         .cacheHit,.exitCode,.digest.hash,((.platform.properties // [])|map({key:.name,value:.value})|from_entries)]'''
    primaries = cell["workers"] = {}
    compiles = cell["posix_server_actions"] = []
    with subprocess.Popen([WORK / "bb", "print", "--raw", "--compact_execution_log=" + str(directory/"compact")],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", projection], stdin=decoder.stdout, stdout=subprocess.PIPE) as filtered:
            decoder.stdout.close()
            for line in filtered.stdout:
                kind, owner, exe, program, runner, cached, code, digest, props = json.loads(line)
                owner = label(owner)
                facts = {"action_digest": digest, "remote": runner == "remote", "cached": bool(cached),
                         "exit": int(code or 0), "arch": props.get("Arch"), "image": props.get("container-image"),
                         "isolation": props.get("workload-isolation-type")}
                if kind == "TestRunner":
                    if not program:  # Bazel may separately generate fallback XML.
                        continue
                    if owner not in owners:
                        continue
                    if owner in primaries:
                        cell["errors"]["duplicate_primary"] = owner
                    facts["expected_program"] = Path(program).name == owner.split(":")[1]+"-bench"
                    facts.update(user=props.get("dockerUser"), ipv6=props.get("network-enable-ipv6"))
                    primaries[owner] = facts
                else:
                    facts["declared_llvm_driver"] = "llvm" in exe.lower()
                    facts.update(kind=kind, executable=Path(exe).name)
                    compiles.append(facts)
            assert filtered.wait() == 0
        assert decoder.wait()==0
    cell["missing_primaries"] = sorted(set(owners)-set(primaries))
    assert set(primaries)==set(owners)
    for owner, facts in primaries.items():
        assert facts["expected_program"] and facts["remote"] and not facts["cached"]
        assert facts["arch"]=="amd64" and facts["image"]==IMAGE and facts["isolation"]=="firecracker"
        assert facts["user"]=="root" and facts["ipv6"]=="true" and bool(facts["exit"])==negative
        assert re.fullmatch(r"[a-f0-9]{64}", facts["action_digest"] or "")
    for facts in compiles:
        assert facts["arch"]=="amd64" and facts["image"]==IMAGE
        assert facts["isolation"]=="oci" and facts["declared_llvm_driver"]


def output(item, expected):
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path))
    assert path.samefile(expected)
    assert path.stat().st_size <= 64 << 20
    assert path.stat().st_size or item["name"] == "test.outputs_manifest__MANIFEST"
    data = path.read_bytes()
    assert (not item.get("digest") or item["digest"] == sha(data)) and int(item.get("length",len(data)))==len(data)
    return data


def original(event, cell, arm, owner, negative):
    ident, result = event["id"]["testResult"], event["testResult"]
    tag = arm+"-"+owner.split("_")[-2]
    row = cell["results"][owner] = {"config": ident["configuration"]["id"], "status": result["status"], "files": {}}
    row["strategy"]=result.get("executionInfo",{}).get("strategy")
    row["cached"]=bool(result.get("cachedLocally") or result.get("executionInfo",{}).get("cachedRemotely"))
    docs = {}
    pcaps = {}
    for item in result["testActionOutput"]:
        key=item["name"]
        packet = key.startswith("test.outputs/") and key.endswith(".pcap")
        if not packet and key not in ("test.log","test.xml","test.outputs_manifest__MANIFEST"):
            continue
        prefix=Path("bazel-testlogs/test/packetimpact/tests") / owner.split(":")[1]
        local="test.outputs_manifest/MANIFEST" if key=="test.outputs_manifest__MANIFEST" else key
        data=output(item, prefix / local)
        assert key not in docs
        docs[key]=data
        row["files"][key]={"sha256":sha(data),"bytes":len(data)}
        if packet:
            assert 0 < len(data) <= 8 << 20 and re.search(r"_tb-0-test[.]pcap$",key)
            pcaps[key]=data
            publish(tag+".pcap",data)
        elif key in ("test.log","test.xml"):
            publish(tag+"-"+key,data)
    if "test.outputs_manifest__MANIFEST" in docs:
        lines = docs["test.outputs_manifest__MANIFEST"].decode(errors="replace").splitlines()
        row["manifest"] = {"lines":len(lines),"pcap_rows":[]}
        for path, packet in pcaps.items():
            name = path.removeprefix("test.outputs/")
            for line in lines:
                fields = line.split("\t",2)
                if len(fields)==3 and fields[0]==name:
                    mime = fields[2]
                    row["manifest"]["pcap_rows"].append({"size_text":fields[1][:32],"size_matches":fields[1]==str(len(packet)),
                        "mime":mime if re.fullmatch(r"[-\w.+]+/[-\w.+]+",mime) else None,
                        "file_type_unavailable":"Could not establish file type" in mime})
    assert all(ident.get(k,1)==1 for k in ("attempt","shard","run"))
    assert row["strategy"]=="remote" and not row["cached"]
    raw=docs["test.log"].decode(errors="replace")
    text=re.sub(r"(?m)^\x16", "", raw)
    row["manifest_warnings"]={"missing_file":text.count("file: command not found"),"printf_format":text.count("invalid format character")}
    xml=ElementTree.fromstring(docs["test.xml"])
    cases=list(xml.iter("testcase"))
    if negative:
        row["missing_iptables"]="fork/exec /sbin/iptables-nft: no such file or directory" in text
        assert result["status"]=="FAILED" and row["missing_iptables"]
        assert cases and any(c.tag in ("failure","error") for c in xml.iter())
        return
    assert result["status"]=="PASSED" and [c.get("name") for c in cases]==["TestTCPHandshakeWindowSize"]
    assert not any(c.tag in ("failure","skipped","error") for c in xml.iter())
    assert re.findall(r"(?m)^\s*--- (PASS|SKIP|FAIL): (Test\S+) [(]",text)==[("PASS","TestTCPHandshakeWindowSize")]
    (name,packet),=pcaps.items()
    row["pcap"]={"name":name,"sha256":sha(packet),"bytes":len(packet)}
    row["wire"]=frames.observe(TEST,{"0":frames.frames(packet)})


def read_bep(directory,cell,arm,owners,negative):
    found=set()
    configs=set()
    with (directory/"bep").open() as events:
        for line in events:
            event=json.loads(line)
            if "started" in event:
                cell["started_invocation"]=str(uuid.UUID(event["started"]["uuid"]))
            if "action" in event and not event["action"].get("success", False):
                action=event["id"].get("actionCompleted",{})
                owner=label(action.get("label",""))
                if "first_failed_action" not in cell and re.fullmatch(r"[\w@+./~:-]{1,290}",owner):
                    cell["first_failed_action"]={"owner":owner,"type":event["action"].get("type","")[:80]}
            if term:=event["id"].get("targetCompleted"):
                if label(term["label"]) in owners:
                    configs.add((label(term["label"]),term["configuration"]["id"]))
            if "testResult" in event:
                owner=label(event["id"]["testResult"]["label"])
                assert owner in owners and owner not in found
                found.add(owner)
                try:
                    original(event,cell,arm,owner,negative)
                except Exception as exc:
                    cell["errors"][owner]=error_at(exc)
    assert cell.get("started_invocation")==cell["requested_invocation"]
    assert found==set(owners)
    assert all((owner,cell["results"][owner]["config"]) in configs for owner in owners)


def abi(cell):
    path=Path("bazel-bin/test/packetimpact/dut/posix_server")
    if path.is_file():
        blob=path.read_bytes()
        assert blob[:6]==b"\x7fELF\x02\x01" and struct.unpack_from("<H",blob,18)[0] == 62
        versions=subprocess.check_output(["readelf","--version-info","--wide",path],text=True)
        cell["posix_server"]={"bytes":len(blob),"sha256":sha(blob),
                              "required_glibc":sorted(set(re.findall(r"Name: (GLIBC_[.\d]+)",versions)))}


def run(arm,head,owners,negative):
    subprocess.run(["git","switch","--detach","-q",head],check=True)
    build=Path("test/rbe/packet_worker/BUILD")
    build.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(WORK/"platform.BUILD",build)
    directory=WORK/arm
    directory.mkdir()
    cell=REPORT["cells"][arm]={"results":{},"errors":{}}
    cell["requested_invocation"]=str(uuid.uuid4())
    args=[str(WORK/"bazel"),"--nosystem_rc","--nohome_rc","--bazelrc="+str(RC),"test","--config=x86_64","-c","opt",
          "--jobs=4","--lockfile_mode=off","--keep_going","--nocache_test_results","--runs_per_test=1",
          "--flaky_test_attempts=1","--test_sharding_strategy=disabled","--test_env=GO_TEST_WRAP_TESTV=1",
          "--test_filter=^TestTCPHandshakeWindowSize$","--test_timeout=360","--invocation_id="+cell["requested_invocation"],
          "--host_platform="+PLATFORM,"--extra_execution_platforms="+PLATFORM,
          "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash","--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
          "--noincompatible_exclude_starlark_flags_from_exec_config","--spawn_strategy=remote","--strategy=TestRunner=remote",
          "--noremote_local_fallback","--noremote_upload_local_results","--remote_timeout=600",
          "--remote_download_outputs=minimal","--remote_download_regex=.*/(test[.](log|xml)|test[.]outputs(/.*)?|MANIFEST|posix_server)$",
          "--modify_execution_info=^TestRunner$=+no-remote-cache","--nobuild_event_json_file_path_conversion",
          "--execution_log_compact_file="+str(directory/"compact"),"--build_event_json_file="+str(directory/"bep"),*owners]
    with (directory/"console").open("wb") as log:
        cell["exit"]=subprocess.run(["timeout","--signal=INT","--kill-after=6s","660s" if negative else "1220s",*args],stdout=log,stderr=subprocess.STDOUT).returncode
    console=(directory/"console").read_bytes()
    for line in console.decode(errors="replace").splitlines():
        if not re.search(r"^ERROR:|[.](?:go|cc|S|bzl):\d+:",line) or re.search(r"(?i)authorization|credential|token|api.?key|header|grpcs?://|https?://|--",line):
            continue
        line=re.sub(r"\x1b\[[0-9;]*m","",line)
        if len(line)<=500:
            cell["first_diagnostic"]=line
            break
    cell["signals"]=[key for key in ("Error downloading","No matching toolchains","No registered executors",
                                     "No such file or directory","FAILED_TO_FETCH","undefined reference","GLIBC_") if key.encode() in console]
    cell["public_fetch_urls"]=sorted(set(p.decode() for p in re.findall(rb'https://(?:github[.]com|mirror[.]bazel[.]build|releases[.]bazel[.]build)/[A-Za-z0-9_./+%=-]+',console)))[:12] if b"Error downloading" in console else []
    for name,f in (("bep",lambda:read_bep(directory,cell,arm,owners,negative)),
                   ("actions",lambda:physical(directory,cell,owners,negative)),("abi",lambda:abi(cell))):
        try: f()
        except Exception as exc:cell["errors"][name]=error_at(exc)
        save()
    cell["verified"]=not cell["errors"] and cell["exit"]==(3 if negative else 0)
    save()


if __name__=="__main__":
    start=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
    for arm,head,owners in (("fixed",FIX,[PREFIX+"_native_test",PREFIX+"_netstack_test"]),("stock",BASE,[PREFIX+"_native_test"])):
        try:run(arm,head,owners,arm=="stock")
        except Exception as exc:REPORT["cells"].setdefault(arm,{})["error"]=error_at(exc)
        save()
    platform_file=Path("test/rbe/packet_worker/BUILD")
    if platform_file.is_file() and platform_file.read_bytes()==(WORK/"platform.BUILD").read_bytes():
        platform_file.unlink()
    subprocess.run(["git","switch","--detach","-q",start],check=True)
    REPORT["fresh_compiler"]=any(a["kind"]=="CppCompile" and a["remote"] and not a["cached"] and a["exit"]==0 and a["action_digest"] for c in REPORT["cells"].values() for a in c.get("posix_server_actions",[]))
    REPORT["verified"]=all(c.get("verified",False) for c in REPORT["cells"].values())
    save()
    raise SystemExit(not REPORT["verified"])
