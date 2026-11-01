"""Reconcile original incomplete owners without rebuilding the wildcard."""
from pathlib import Path
exec(Path(__file__).with_name("wildcard-base.py").read_text().partition('baseline="f906c5dd')[0])
expected=json.loads((tmp/"failed-owners.json").read_text())
headers=["@@c-ares+//:ares_build_h","@@c-ares+//:ares_config_h"]
pattern=r".*/(protoc|protoc_minimal|grpc_cpp_plugin_native|protoc-gen-upb_stage0|protoc-gen-upb_minitable_stage0)$"

def logged_outputs(compact,folder):
    expr=r'''select(.mnemonic=="CppLink" or .mnemonic=="GenUpbStage1" or ((.targetLabel // "")|contains("hostlayout_test_gen"))) | {label:.targetLabel, mnemonic, argv0:.commandArgs[0], runner, cacheHit, exitCode, outputs:[.actualOutputs[]?|select(.path|test("(_generated.go|/(protoc|protoc_minimal|grpc_cpp_plugin_native|protoc-gen-upb_stage0|protoc-gen-upb_minitable_stage0))$"))|{path,digest}],failed:((.exitCode // 0)!=0)} | select((.outputs|length)>0 or .failed or .mnemonic=="GenUpbStage1")'''
    with subprocess.Popen([str(tmp/"bb"),"print","--compact_execution_log="+str(compact)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as de:
        values=subprocess.check_output(["jq","-c",expr],stdin=de.stdout)
        de.stdout.close()
        if de.wait(): raise ValueError("could not decode compact actions")
    execroot=Path(subprocess.check_output([*bzl,"info","execution_root"],text=True).strip())
    rows=[]
    for line in values.splitlines():
        action=json.loads(line)
        for output in action["outputs"]:
            f=execroot/output["path"]
            if f.is_file():
                with f.open("rb") as s:
                    head=s.read(20);s.seek(0)
                    sha=hashlib.file_digest(s,"sha256").hexdigest()
                if sha!=output["digest"]["hash"]:raise ValueError("digest mismatch for "+f.name)
                output["downloaded_bytes"]=f.stat().st_size
                if head.startswith(b"\x7fELF"):
                    dump=subprocess.check_output(["readelf","-ldW",f],text=True)
                    interp=re.search(r"Requesting program interpreter: ([^\]]+)",dump)
                    output.update(elf_machine=int.from_bytes(head[18:20],"little"),interpreter=interp.group(1) if interp else None,needed=re.findall(r"Shared library: \[([^\]]+)\]",dump))
        rows.append(action)
    write(folder/"tools.json",rows)
    return rows

def measure(cpu):
    cfg={"amd64":"x86_64","arm64":"aarch64"}[cpu]
    folder=out/cpu;folder.mkdir(exist_ok=True)
    log,bep,compact=[tmp/(cpu+"-replay."+x) for x in ("log","bep","binpb")]
    args=["build","--config="+cfg,"--compilation_mode=fastbuild","--host_platform=//test/rbe/empty_worker:"+cpu,
          "--extra_execution_platforms=//test/rbe/empty_worker:"+cpu,"--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
          "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+cpu+"_ash","--keep_going","--jobs=40","--spawn_strategy=remote",
          "--noremote_local_fallback","--remote_accept_cached","--noremote_upload_local_results","--remote_download_outputs=toplevel",
          "--remote_download_regex="+pattern,"--execution_log_compact_file="+str(compact),"--build_event_json_file="+str(bep),*expected,*headers]
    record={"source":source,"arch":cpu,"expected":expected,"witnesses":headers,"started":now(),"args":args}
    with log.open("w") as f:
        record["exit"]=subprocess.run(["timeout","--signal=INT","--kill-after=30s","37m",*bzl,*args],stdout=f,stderr=subprocess.STDOUT).returncode
    record["execution_finished"]=now()
    ev=events(bep)
    record.update(first_errors=diagnostics(log,folder),invocation=ev["invocation"],finish=ev["finish"],
                  owners={l:[(c,ev["completed"].get((l,c),"MISSING")) for c in ev["configured"].get(l,[])] for l in sorted(set(expected)|{l for l in ev["configured"] if l.startswith(("@@c-ares","@c-ares"))})})
    write(folder/"receipt.json",record)
    if compact.is_file():
        record["actions"]=stats(compact,cpu,folder)
        affected=logged_outputs(compact,folder)
        outputs=[o for a in affected for o in a["outputs"] if o["path"].endswith("_generated.go")]
        record["hostlayout_source_outputs"]={o["path"].split("/")[-1]:o["digest"] for o in outputs}
        record["hostlayout_source_bytes"]=sum(int(v["sizeBytes"]) for v in record["hostlayout_source_outputs"].values())
    record["hostlayout_pass"]=all([status for _,status in record["owners"][l]]==["built"] for l in expected if "_hostlayout_test" in l)
    record["service_pass"]=all([status for _,status in record["owners"][l]]==["built"] for l in expected if "taskserver:" in l)
    record["verified_at"]=now()
    write(folder/"receipt.json",record)
    return record

subprocess.run(["git","checkout","--detach",source],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
summary={"source":source,"original":41276314920,"platform_image":image,"architectures":{}}
for cpu in ("amd64","arm64"):
    try:
        summary["architectures"][cpu]=measure(cpu)
    except Exception as error:
        summary["architectures"][cpu]={"error":repr(error)[:1200]}
    write(out/"summary.json",summary)
    print(cpu,{k:summary["architectures"][cpu].get(k) for k in ("exit","hostlayout_pass","service_pass","hostlayout_source_bytes","error","invocation")},flush=True)
summary["complete"]=all(m.get("exit")==0 and m.get("hostlayout_pass") and m.get("service_pass") for m in summary["architectures"].values())
summary["verified_at"]=now()
write(out/"summary.json",summary)
sys.exit(not summary["complete"])
