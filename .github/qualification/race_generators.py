"""Observe the four real source-generation actions under the public race config."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import traceback
import uuid


STOCK = "83f2d8568e1799eab47b02dc92da2988bdb001d9"
FIXED = "709cd18c362576aaea12aa6d36c72034dbe001c8"
TMP = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
IMAGE = "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"
ROOTS = {
    "//pkg/ring0/pagetables:generic_walker_arm64": ("GoGenericsTemplate", "go_merge"),
    "//pkg/ring0/pagetables:walker_lookup_arm64": ("GoGenericsInstance", "go_generics"),
    "//pkg/marshal/primitive:primitive_abi_autogen": ("GoMarshal", "go_marshal"),
    "//pkg/marshal/primitive:primitive_state_autogen": ("GoStateify", "stateify"),
}
FLAGS = ["--config=aarch64", "-c", "opt", "--config=race",
         "--platforms=@llvm//platforms:linux_aarch64_glibc_static",
         "--host_platform=//test/rbe/release_prewarmer:amd64",
         "--extra_execution_platforms=//test/rbe/release_prewarmer:amd64",
         "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
         "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
         "--lockfile_mode=off", "--jobs=32", "--remote_download_outputs=toplevel",
         "--remote_download_regex=.*/bin/tools/(go_generics|go_stateify|go_marshal)/.*/(go_merge|go_generics|stateify|go_marshal)$",
         "--color=no", "--curses=no", "--noshow_progress"]
BAZEL = [str(TMP/"bazel"),"--nosystem_rc","--nohome_rc","--bazelrc="+str(TMP/"private.bazelrc")]


def inspect(path):
    raw=path.read_bytes()
    assert raw[:6]==b"\x7fELF\x02\x01" and 0<len(raw)<40*1024**2
    assert struct.unpack_from("<H",raw,18)==(62,)
    out=subprocess.check_output(["readelf","-lW",str(path)])
    matches=re.findall(rb"Requesting program interpreter: ([A-Za-z0-9/_.+-]{1,128})\]",out)
    assert len(matches)==int(b"INTERP" in out)
    return {"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),"machine":62,
            "interpreter":matches[0].decode() if matches else None}


def run(variant,report):
    source=report["source"]
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
    assert not subprocess.check_output(["git","diff","--name-only","HEAD"])
    assert hashlib.sha256(Path("test/rbe/release_prewarmer/BUILD").read_bytes()).hexdigest()=="ae1f23a11f3cb747fbec49a34c510c5ab9dcb75d06d94bfd4d9a49d50c629271"
    out=TMP/variant
    out.mkdir()
    iid=report["invocation"]=str(uuid.uuid4())
    be,sp=out/"actions.bep",out/"execution.compact"
    with (out/"build.log").open("wb") as log:
        report["exit"]=subprocess.run(["timeout","--kill-after=15","720",*BAZEL,"build",*FLAGS,
            "--keep_going","--modify_execution_info=^(GoGenericsTemplate|GoGenericsInstance|GoMarshal|GoStateify)$=+no-remote-cache",
            "--invocation_id="+iid,"--build_event_publish_all_actions","--build_event_json_file="+str(be),
            "--execution_log_compact_file="+str(sp),*ROOTS],stdout=log,stderr=subprocess.STDOUT).returncode
    complete,failures={},{}
    for line in be.open():
        e=json.loads(line)
        key=e["id"]
        if "started" in e:
            assert e["started"]["uuid"]==iid
        if k:=key.get("targetCompleted"):
            if k["label"] in ROOTS and not k.get("aspect") and "completed" in e:
                complete[k["label"]]=bool(e["completed"].get("success"))
        if k:=key.get("actionCompleted"):
            a=e.get("action",{})
            if k["label"] in ROOTS and not a.get("success") and a.get("type")==ROOTS[k["label"]][0]:
                failures[k["label"]]=a.get("exitCode")
    report.update(completed=complete,failed_actions=failures)
    props={"OSFamily":"linux","Arch":"amd64","container-image":IMAGE,"workload-isolation-type":"oci","recycle-runner":"false"}
    observed=report["spawns"]={}
    with subprocess.Popen([TMP/"bb","print","--raw","--compact_execution_log="+str(sp)],
                          stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as reader:
        query="""
          select(.spawn) | .spawn | . as $s |
          select(($owners|index($s.targetLabel))!=null and
                 (.mnemonic|IN("GoGenericsTemplate","GoGenericsInstance","GoMarshal","GoStateify"))) |
          [.targetLabel,.mnemonic,.args[0],.runner,(.cacheHit//false),(.exitCode//0),
           ([.platform.properties[]?|{key:.name,value:.value}]|from_entries|contains($props))]
        """
        with subprocess.Popen(["jq","-c","--argjson","owners",json.dumps(list(ROOTS)),"--argjson","props",json.dumps(props),
                               query],stdin=reader.stdout,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as decoder:
            reader.stdout.close()
            for line in decoder.stdout:
                owner,kind,path,runner,cached,exitcode,platform=json.loads(line)
                assert owner not in observed and ROOTS[owner]==(kind,Path(path).name)
                normalized=Path(path)
                assert len(path)<320 and not normalized.is_absolute() and normalized.parts[0]=="bazel-out" and ".." not in normalized.parts
                observed[owner]={"type":kind,"path":path,"remote":runner=="remote",
                                 "cached":cached,"exit":exitcode,"platform_matches":platform}
            assert decoder.wait()==0
        assert reader.wait()==0
    pattern="mnemonic('Go(GenericsTemplate|GenericsInstance|Marshal|Stateify)',deps(set("+" ".join(ROOTS)+")))"
    with (out/"aquery.json").open("wb") as dest, (out/"query.log").open("wb") as err:
        report["query_exit"]=subprocess.run(["timeout","240",*BAZEL,"aquery",*FLAGS,"--output=jsonproto",
                                             "--include_commandline","--noinclude_artifacts",pattern],stdout=dest,stderr=err).returncode
    assert report["query_exit"]==0
    query=json.loads((out/"aquery.json").read_bytes())
    owners={str(t["id"]):t["label"].removeprefix("@@") for t in query["targets"]}
    tools=report["tools"]={}
    for a in query["actions"]:
        owner=owners[str(a["targetId"])]
        if owner not in ROOTS or a["mnemonic"]!=ROOTS[owner][0]:
            continue
        path=Path(a["arguments"][0])
        assert path.name==ROOTS[owner][1] and path.parts[0]=="bazel-out" and not path.is_absolute() and ".." not in path.parts
        item=tools[owner]={"path":str(path)}
        if owner in observed:
            assert observed[owner]["path"]==str(path)
        if path.is_file():
            assert path.resolve(strict=True).is_relative_to(Path("bazel-out").resolve(strict=True))
            item["elf"]=inspect(path)
    assert set(tools)==set(complete)==set(ROOTS)
    assert all(v["remote"] and not v["cached"] and v["platform_matches"] for v in observed.values())
    if variant=="stock":
        main="//pkg/ring0/pagetables:generic_walker_arm64"
        assert report["exit"]==1 and not complete[main] and failures[main]!=0
        assert observed[main]["exit"]!=0 and tools[main]["elf"]["interpreter"]=="/lib/ld-musl-x86_64.so.1"
    else:
        assert report["exit"]==0 and all(complete.values()) and set(observed)==set(ROOTS)
        assert all(v["exit"]==0 for v in observed.values())
        assert all(v["elf"]["interpreter"] is None for v in tools.values())
    report["status"]="PASS"


mode,=sys.argv[1:]
row={"status":"INCOMPLETE","source":{"stock":STOCK,"fixed":FIXED}[mode],"mode":mode}
try:
    run(mode,row)
except (AssertionError,KeyError,IndexError,ValueError,OSError,subprocess.SubprocessError) as err:
    row["error"]={"type":type(err).__name__,"line":traceback.extract_tb(err.__traceback__)[-1].lineno}
finally:
    (TMP/(mode+".json")).write_text(json.dumps(row,indent=2)+"\n")
print(mode,row["status"])
sys.exit(row["status"]!="PASS")
