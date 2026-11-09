"""Read a stock firewall failure and two corrected PacketImpact tests on the common Ubuntu VM."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
import zipfile

import frames

WORK = Path(os.environ["RUNNER_TEMP"]) / "packet-worker"
OUT = WORK / "results"
BASE = "61ff148f0df0efa29c9f500914477525c5debe62"
FIX = "02168427b8d45b05bc4c4bb1409b0d97a75e56d4"
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
TEST = "tcp_handshake_window_size"
PREFIX = "//test/packetimpact/tests:" + TEST
PLATFORM = "//test/rbe/packet_worker:amd64"
RC = WORK / "remote.bazelrc"
REPORT = {"verified": False, "stock": BASE, "fixed": FIX, "public_product": "c3d696ee1bfdf985a971be5854b18eda85b8820e",
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
                assert facts["arch"] == "amd64" and facts["image"] == IMAGE
                assert digest is None or re.fullmatch(r"[a-f0-9]{64}", digest)
                if kind == "TestRunner":
                    assert owner in owners and facts["isolation"] == "firecracker"
                    if not program:  # Bazel may separately generate fallback XML.
                        continue
                    assert Path(program).name == owner.split(":")[1]+"-bench" and owner not in primaries
                    facts.update(user=props.get("dockerUser"), ipv6=props.get("network-enable-ipv6"))
                    primaries[owner] = facts
                    assert facts["remote"] and not cached and facts["user"]=="root" and facts["ipv6"]=="true" and digest
                    assert bool(facts["exit"]) == negative
                else:
                    assert facts["isolation"] == "oci" and "llvm" in exe.lower()
                    facts.update(kind=kind, executable=Path(exe).name)
                    compiles.append(facts)
            assert filtered.wait() == 0
        assert decoder.wait()==0
    assert set(primaries)==set(owners)


def output(item):
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path))
    assert 0 < path.stat().st_size <= 64 << 20
    data = path.read_bytes()
    assert (not item.get("digest") or item["digest"] == sha(data)) and int(item.get("length",len(data)))==len(data)
    return data


def original(event, cell, arm, owner, negative):
    ident, result = event["id"]["testResult"], event["testResult"]
    tag = arm+"-"+owner.split("_")[-2]
    row = cell["results"][owner] = {"config": ident["configuration"]["id"], "status": result["status"], "files": {}}
    assert all(ident.get(k,1)==1 for k in ("attempt","shard","run"))
    assert not result.get("cachedLocally") and not result.get("executionInfo",{}).get("cachedRemotely")
    assert result.get("executionInfo",{}).get("strategy") == "remote"
    docs = {}
    for item in result["testActionOutput"]:
        key=item["name"]
        if key not in ("test.log","test.xml","test.outputs__outputs.zip"):
            continue
        data=output(item)
        assert key not in docs
        docs[key]=data
        row["files"][key]={"sha256":sha(data),"bytes":len(data)}
        if key in ("test.log","test.xml"):
            publish(tag+"-"+key,data)
    text=docs["test.log"].decode(errors="replace")
    xml=ElementTree.fromstring(docs["test.xml"])
    cases=list(xml.iter("testcase"))
    if negative:
        row["missing_iptables"]= "fork/exec /sbin/iptables-nft: no such file or directory" in text
        assert result["status"]=="FAILED" and row["missing_iptables"]
        assert cases and any(c.tag in ("failure","error") for c in xml.iter())
        return
    assert result["status"]=="PASSED" and [c.get("name") for c in cases]==["TestTCPHandshakeWindowSize"]
    assert not any(c.tag in ("failure","skipped","error") for c in xml.iter())
    assert re.findall(r"(?m)^\s*--- (PASS|SKIP|FAIL): (Test\S+) [(]",text)==[("PASS","TestTCPHandshakeWindowSize")]
    with zipfile.ZipFile(io.BytesIO(docs["test.outputs__outputs.zip"])) as archive:
        pcap, = (f for f in archive.infolist() if f.filename.endswith(".pcap"))
        assert re.search(r"_tb-0-test[.]pcap$",pcap.filename) and 0<pcap.file_size<=8<<20
        packet=archive.read(pcap)
    publish(tag+".pcap",packet)
    row["pcap"]={"sha256":sha(packet),"bytes":len(packet)}
    row["wire"]=frames.observe(TEST,{"0":frames.frames(packet)})


def read_bep(directory,cell,arm,owners,negative):
    found=set()
    configs=set()
    with (directory/"bep").open() as events:
        for line in events:
            event=json.loads(line)
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
    args=[str(WORK/"bazel"),"--nosystem_rc","--nohome_rc","--bazelrc="+str(RC),"test","--config=x86_64","-c","opt",
          "--jobs=4","--lockfile_mode=off","--keep_going","--nocache_test_results","--runs_per_test=1",
          "--flaky_test_attempts=1","--test_sharding_strategy=disabled","--test_env=GO_TEST_WRAP_TESTV=1",
          "--test_filter=^TestTCPHandshakeWindowSize$","--test_timeout=360","--zip_undeclared_test_outputs",
          "--host_platform="+PLATFORM,"--extra_execution_platforms="+PLATFORM,
          "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash","--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
          "--noincompatible_exclude_starlark_flags_from_exec_config","--spawn_strategy=remote","--strategy=TestRunner=remote",
          "--noremote_local_fallback","--noremote_upload_local_results","--remote_timeout=600",
          "--remote_download_outputs=minimal","--remote_download_regex=.*/(test[.](log|xml)|outputs[.]zip|posix_server)$",
          "--modify_execution_info=^TestRunner$=+no-remote-cache","--nobuild_event_json_file_path_conversion",
          "--execution_log_compact_file="+str(directory/"compact"),"--build_event_json_file="+str(directory/"bep"),*owners]
    with (directory/"console").open("wb") as log:
        cell["exit"]=subprocess.run(["timeout","--signal=INT","--kill-after=6s","1220s",*args],stdout=log,stderr=subprocess.STDOUT).returncode
    console=(directory/"console").read_bytes()
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
    for arm,head,owners in (("stock",BASE,[PREFIX+"_native_test"]),("fixed",FIX,[PREFIX+"_native_test",PREFIX+"_netstack_test"])):
        try:run(arm,head,owners,arm=="stock")
        except Exception as exc:REPORT["cells"].setdefault(arm,{})["error"]=error_at(exc)
        save()
    REPORT["observed_compiler"]=any(any(a["kind"]=="CppCompile" for a in c.get("posix_server_actions",[])) for c in REPORT["cells"].values())
    REPORT["verified"]=all(c.get("verified",False) for c in REPORT["cells"].values()) and REPORT["observed_compiler"]
    save()
    raise SystemExit(not REPORT["verified"])
