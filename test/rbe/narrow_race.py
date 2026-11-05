"""Qualify the public race executables and their existing analysis rules."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SOURCE = "3dab3494e2a2154244e8b2fc77699c02c2277a56"
ROOTS = ("//runsc:runsc-race", "//runsc/cmd/sentry:gvisor_sentry-race")
NOGO = tuple(x + "_nogo" for x in ROOTS)
POLICY = ("//runsc:runsc_deps_test", "//runsc/cmd/sentry:gvisor_sentry_deps_test")
DIR = Path(os.environ["CHECK_DIR"])
PUBLIC = DIR / "public"
BASE = ["-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
RACE = [*BASE, "--config=race"]
LINES = ("no matching", "not available", "not declared", "No attribute", "no such",
         "not visible", "does not refer", "error loading", "analysis failed", "cannot parse",
         "unable to find library", "exceeded", "not found", "error executing", "visibility",
         "invalid", "does not match", "has no attribute", "unknown", "not a valid", "undefined")

def sha(b):
    return hashlib.sha256(b).hexdigest()

def sig(p):
    return {"bytes":p.stat().st_size,"sha256":sha(p.read_bytes())}

def save():
    (PUBLIC / (mode + ".json")).write_text(json.dumps(r,indent=2))

def command(name, argv, cap, require=True):
    out,err=DIR/(name+".out"),DIR/(name+".err")
    with out.open("wb") as a,err.open("wb") as b:
        proc=subprocess.run(["timeout","-k","15",str(cap),*argv],stdout=a,stderr=b)
    tail=err.read_bytes()[-65536:].decode(errors="replace")
    r["commands"][name]={"exit":proc.returncode,"stdout_bytes":out.stat().st_size,
        "stderr_sha256":sig(err)["sha256"],"hints":[x for x in LINES if x.lower() in tail.lower()]}
    if proc.returncode:
        for line in tail.splitlines():
            if not (line.startswith("ERROR:") or "Error in " in line or "No attribute " in line):continue
            line=re.sub(r"/(?:home|tmp|opt|usr|var)/[^ ]+","[path]",line)
            if len(line)<1200 and all(ord(c)<128 for c in line) and not any(t in line.lower() for t in ("token","authorization","password","http","credential","secret","environment")):
                r["commands"][name]["first_error"]=line[:450];break
    save()
    if require and proc.returncode:raise ValueError(name)
    return out

def evaluate(s, name, flags, expr):
    return command(name,["bazel","cquery",*flags,"--output=starlark","--starlark:expr="+s,expr],300).read_text()

def local(l):
    return "//"+l.split("//",1)[-1]

def norm(name):
    return name.lstrip("/").replace(":","/")

PRIOR = '{"artifact":13459164255,"zip_sha256":"a48815b0a4dbcadd6a6e1a25d85b95839759ebac0f553bce489b578ef15a5286","tests":{"//runsc/cmd/sentry:gvisor_sentry_deps_test":{"status":"PASSED","fresh":true,"run":1,"shard":1,"attempt":1,"test.log":{"bytes":186,"sha256":"a5697b950118257fbc3fbc0dd9ea232bcf18c80cbaa29cace0d632e547e996ae"},"test.xml":{"bytes":614,"sha256":"c7cf6c2ad14ce75b7871ab758f7865d289e3b2dd920e0ab608d6874961ccd8c2"},"cases":1,"skipped_or_failed":0},"//runsc:runsc_deps_test":{"status":"PASSED","fresh":true,"run":1,"shard":1,"attempt":1,"test.log":{"bytes":167,"sha256":"5a5f9abc15fff907e6031b7fee21114b9bb83be6beeb7a1b839017bc13c508b9"},"test.xml":{"bytes":557,"sha256":"f38dc2714a85be4d60eede114a48b4ca4ef49195bef787043653449e375a2cde"},"cases":1,"skipped_or_failed":0},"//runsc:runsc-race_nogo":{"status":"PASSED","fresh":true,"run":1,"shard":1,"attempt":1,"test.log":{"bytes":193,"sha256":"ebf1a6cc815a15419d7d769eb7a2bff43e2b65306cb61a766b153119c60e7862"},"test.xml":{"bytes":583,"sha256":"40cf466412b4c68ae74ba93a8c4a8b2c78e70c3e88513e986270c2b936b340ea"},"cases":1,"skipped_or_failed":0},"//runsc/cmd/sentry:gvisor_sentry-race_nogo":{"status":"PASSED","fresh":true,"run":1,"shard":1,"attempt":1,"test.log":{"bytes":212,"sha256":"9f22e72a9ddb5784730ab21e29c67513cae28f3c531d67a0595325bd52f19d4b"},"test.xml":{"bytes":640,"sha256":"59b40f9bde054372beaae6180a1bcdef573abfd13b5f72c240e3b30b77299a0e"},"cases":1,"skipped_or_failed":0}},"analysis_actions":[{"label":"//runsc:runsc-race_impl","primary":"bazel-out/k8-opt-ST-75f02a4985e4/bin/runsc/runsc-race_impl.findings","cfg":"393a8a86f8c8b7cd64f41f93aa408c9566240fc221debee73c93286d75bf689e","success":true},{"label":"//runsc:runsc-race_impl","primary":"bazel-out/aarch64-opt-ST-0c1838a08b24/bin/runsc/runsc-race_impl.findings","cfg":"c65b75b8d4fc3ad4809abb559a92bc288ba4f3429b8cc51fc16a5477f031acbf","success":true},{"label":"//runsc/cmd/sentry:gvisor_sentry-race_impl","primary":"bazel-out/k8-opt-ST-75f02a4985e4/bin/runsc/cmd/sentry/gvisor_sentry-race_impl.findings","cfg":"393a8a86f8c8b7cd64f41f93aa408c9566240fc221debee73c93286d75bf689e","success":true},{"label":"//runsc/cmd/sentry:gvisor_sentry-race_impl","primary":"bazel-out/aarch64-opt-ST-0c1838a08b24/bin/runsc/cmd/sentry/gvisor_sentry-race_impl.findings","cfg":"c65b75b8d4fc3ad4809abb559a92bc288ba4f3429b8cc51fc16a5477f031acbf","success":true}],"gnu_toolchains":"@@rules_cc++cc_configure_extension+local_config_cc_toolchains//:all","gnu_implementation_repo":"rules_cc++cc_configure_extension+local_config_cc"}'

def original():
    prior = json.loads(PRIOR)
    assert prior["artifact"] == 13459164255
    assert set(prior["tests"]) == set(NOGO + POLICY)
    assert all(v["status"] == "PASSED" and v["fresh"] and v["run"] == v["shard"] == v["attempt"] == v["cases"] == 1 and not v["skipped_or_failed"] for v in prior["tests"].values())
    r["original"] = prior
    save()
    return prior


def filters():
    prior = original()
    # Match test's Go configuration and explicitly include its tagged roots.
    flags = [*BASE, "--@io_bazel_rules_go//go/config:pure", "--build_tag_filters="]
    roots = "set(" + " ".join(NOGO) + ")"
    expr = 'mnemonic("GoStaticAnalysis", '+roots+')'
    args = ["--include_aspects", "--include_param_files", "--noinclude_artifacts", "--output=jsonproto"]
    out = command("public_filters", ["bazel","aquery",*flags,*args,expr], 480)
    data = json.loads(out.read_bytes())
    if not data.get("actions"):
        # Inspect actions from a real analysis-only build of these named roots.
        command("configure_filters", ["bazel","build",*flags,"--nobuild","--output_groups=nogo_findings",*NOGO], 480)
        expr='outputs(".*race_nogo[.](amd64|arm64)[.]findings$", mnemonic("GoStaticAnalysis"))'
        out = command("configured_filters", ["bazel","aquery",*flags,"--skyframe_state",*args,expr], 300)
        data = json.loads(out.read_bytes())
    targets={str(t["id"]):local(t["label"]) for t in data.get("targets",[])}
    r["public_filter_joins"] = []
    for a in data.get("actions", []):
        owner = targets.get(str(a.get("targetId")))
        if a.get("mnemonic") != "GoStaticAnalysis" or owner not in NOGO:continue
        argv = a.get("arguments", []) + [x for f in a.get("paramFiles",[]) for x in f.get("arguments",[])]
        if "filter" not in argv:continue
        dest, = [x.removeprefix("-output=") for x in argv if x.startswith("-output=")]
        for cpu, prefix in (("amd64","k8"),("arm64","aarch64")):
            if not dest.endswith("/"+norm(owner)+"."+cpu+".findings"):continue
            raw,=[e["primary"] for e in prior["analysis_actions"] if e["success"] and e["label"]==owner.removesuffix("_nogo")+"_impl" and e["primary"].startswith("bazel-out/"+prefix+"-")]
            matches = [x for x in argv if x.endswith("/"+norm(owner.removesuffix("_nogo"))+"_impl.findings")]
            r["public_filter_joins"].append({"owner":owner,"arch":cpu,"output":dest,"original":raw,"inputs":matches,"exact":raw in matches})
            save()
    rows = r["public_filter_joins"]
    assert len(rows)==4 and {(x["owner"],x["arch"]) for x in rows if x["exact"]} == {(l,c) for l in NOGO for c in ("amd64","arm64")}


def gnu():
    prior=original()
    flags=[*RACE,"--extra_toolchains="+prior["gnu_toolchains"]]
    label=ROOTS[0]
    cc="@bazel_tools//tools/cpp:current_cc_toolchain"
    # Query the resolved provider directly and compare against actual implementations.
    fields='str(target.label)+"\\t"+providers(target)["ToolchainInfo"].cc.toolchain_id+"\\t"+providers(target)["ToolchainInfo"].cc.compiler_executable'
    _,ident,driver=evaluate(fields,"selected_provider",flags,cc).strip().split("\t")
    assert re.fullmatch(r"[A-Za-z0-9_+.-]+",ident)
    prefix="@@"+prior["gnu_implementation_repo"]+"//"
    optional='("@@"+target.label.repo_name+"//"+target.label.package+":"+target.label.name+"\\t"+providers(target)["ToolchainInfo"].cc.toolchain_id+"\\t"+providers(target)["ToolchainInfo"].cc.compiler_executable) if "ToolchainInfo" in providers(target) else "-"'
    q=command("local_implementations",["bazel","cquery",*flags,"--output=starlark","--starlark:expr="+optional,'kind("cc_toolchain rule",'+prefix+':all)'],300,False)
    opts=[line for line in q.read_text().splitlines() if line.count("\t")==2]
    selected=[l for v in opts for l,j,d in [v.split("\t")] if j==ident and d==driver and l.lstrip("@").startswith(prefix.lstrip("@"))]
    row={"selected_id":ident,"selected_labels":selected}
    out=command("gnu_link_action",["bazel","aquery",*flags,"--include_param_files","--noinclude_artifacts","--output=jsonproto",'mnemonic("GoLink",deps('+label+',2))'],360)
    aq=json.loads(out.read_bytes())
    targets={str(t["id"]):local(t["label"]) for t in aq.get("targets",[])}
    a,=[a for a in aq["actions"] if targets.get(str(a["targetId"]))==label+"_impl"]
    argv=a.get("arguments",[])+[v for pf in a.get("paramFiles",[]) for v in pf.get("arguments",[])]
    same=driver in argv or any(x in ("-extld="+driver,"-extld "+driver) for x in argv)
    p=Path(driver);real=p.resolve()
    assert p.is_absolute() and re.fullmatch(r"(?:[A-Za-z0-9_.+-]*-)?(?:gcc|g\+\+)(?:-[0-9]+)?",real.name)
    libc=Path(command("gnu_static_lib",[driver,"-print-file-name=libc.a"],20).read_text().strip())
    assert libc.is_absolute() and libc.is_file() and libc.stat().st_size>100000
    row.update(compiler=real.name,same_driver=same,static=any("-static" in x for x in argv),libc=sig(libc))
    r["toolchain"]=row;save()
    assert same and row["static"]
    bep=DIR/"gnu.bep"
    command("gnu_build",["bazel","build",*flags,"--build_event_publish_all_actions","--build_event_json_file="+str(bep),label],1800,False)
    link=None;target=None
    if bep.is_file():
        for line in bep.open():
            e=json.loads(line);aid=e["id"].get("actionCompleted",{});tid=e["id"].get("targetCompleted",{})
            if tid.get("label")==label and not tid.get("aspect"):target=e.get("completed",{}).get("success",False)
            if e.get("action",{}).get("type")=="GoLink" and aid.get("label")==label+"_impl":
                link={"label":aid["label"],"primary":aid.get("primaryOutput"),"success":bool(e["action"].get("success")),"exit":e["action"].get("exitCode",0)}
    r["completed"]=target;r["link"]=link;save()
    assert r["commands"]["gnu_build"]["exit"]==0 and target and link and link["success"] and link["exit"]==0
    p=Path(evaluate('str(target.files.to_list()[0].path) if len(target.files.to_list())==1 else "NOT_SINGLETON"',"gnu_output",flags,label).strip())
    assert str(p).endswith("/bin/"+norm(label)) and p.is_symlink() and p.samefile(link["primary"])
    elf=command("gnu_elf",["readelf","-hlW",str(p)],30).read_text();raw=p.read_bytes()
    r["binary"]=dict(**sig(p),machine=int.from_bytes(raw[18:20],"little"),static=" INTERP " not in elf and " DYNAMIC " not in elf,race=b"runtime.raceinit" in raw and b"__tsan_" in raw)
    save()
    assert r["binary"]["machine"]==62 and r["binary"]["static"] and r["binary"]["race"]
    assert SOURCE[:9] in command("gnu_version",[str(p),"--version"],20).read_text()
    r["cli_stamp"]=SOURCE[:9];save()
    assert len(selected)==1

mode=sys.argv[1]
r={"source":SOURCE,"carrier":os.environ["GITHUB_SHA"],"status":"INCOMPLETE","commands":{}}
try:
    {"filters":filters,"gnu":gnu}[mode]()
    r["status"]="PASS"
except (OSError,ValueError,KeyError,TypeError,IndexError,AssertionError) as e:
    r["error"]=type(e).__name__;r["line"]=e.__traceback__.tb_next.tb_lineno if e.__traceback__.tb_next else e.__traceback__.tb_lineno
save()
raise SystemExit(r["status"]!="PASS")
