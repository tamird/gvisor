"""Give a consuming root explicit ownership of gVisor's shared Go overrides."""
import ast
import base64
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

src = Path.cwd()
temp = Path(os.environ["RUNNER_TEMP"])
out = temp / "go-graph-report"
out.mkdir()
base = "aa767e4c43d011121271551fe24da501a56f729d"
repos = ["org_golang_google_protobuf", "org_golang_google_grpc", "com_github_containerd_containerd_v2", "com_github_containerd_cgroups_v3", "com_github_containerd_errdefs", "com_github_containerd_log", "com_github_googleapis_gax_go_v2", "com_github_googleapis_gnostic", "io_k8s_api", "io_k8s_apimachinery"]
rows, errors = {}, []
def now(): return datetime.now(timezone.utc).isoformat()
def sha(data): return hashlib.sha256(data).hexdigest()
def call(key, root, *args):
    t=now()
    p=subprocess.run(["bazel","--ignore_all_rc_files",*args],cwd=root,capture_output=True,text=True)
    rows[key]={"source":base,"start":t,"finished":now(),"exit":p.returncode,"args":args,
               "stderr_sha256":sha(p.stderr.encode()),"stdout_sha256":sha(p.stdout.encode()),
               "errors":[l for l in p.stderr.splitlines() if "ERROR:" in l or "Error in fail:" in l][-8:]}
    (out/(key+".stderr")).write_text(p.stderr)
    (out/(key+".stdout")).write_text(p.stdout)
    print(key,p.returncode,flush=True)
    return p
def definition(key,root,*opts):
    p=call(key,root,"mod","show_repo",*opts,*["@"+n for n in repos])
    result={}
    for block in re.findall(r"(?ms)^[ \t]*go_repository\(.*?^[ \t]*\)",p.stdout):
        try:
            node=ast.parse(block.strip()).body[0].value
            attrs={}
            for kw in node.keywords:
                if kw.arg in ("importpath","name","version","sum","build_file_generation","build_directives","patches","patch_args","patch_cmds","build_extra_args","build_file_proto_mode","build_naming_convention"):
                    try: attrs[kw.arg]=ast.literal_eval(kw.value)
                    except ValueError: attrs[kw.arg]=ast.unparse(kw.value)
        except (SyntaxError,ValueError) as e:
            rows[key].setdefault("parse_errors",[]).append(str(e))
            continue
        name=next((n for n in repos if attrs.get("name","").split("+")[-1]==n),None)
        if name:
            attrs.pop("name")
            if attrs.get("patches"):
                # The same gVisor patch has different canonical labels when
                # gVisor itself is the root. Retain the raw declarations too.
                attrs["patches"]=["//tools:protobuf.disable-binary-reading-on-init.patch" if "//tools:protobuf.disable-binary-reading-on-init.patch" in x else x for x in attrs["patches"]]
            result[name]=attrs
    rows[key]["repositories"]=result
    return result
def graph_has_patch(graph):
    return any("protobuf.disable-binary-reading-on-init.patch" in x for x in graph.get(repos[0],{}).get("patches",[]))
def archives(root):
    return [(p,sha(p.read_bytes())) for p in (root/"bazel-bin"/"external").glob("gvisor*/pkg/gvisordetect/**/*") if p.is_file() and p.suffix in (".a",".x")]
try:
    started=now()
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==base
    mod=(src/"MODULE.bazel").read_text()
    before,sep,rest=mod.partition("go_deps.module_override(")
    tail=sep+rest
    assert tail.count("go_deps.module_override(")==1 and tail.count("go_deps.gazelle_override(")==9 and not re.search(r"^go_deps\.(?!gazelle_override|module_override)",tail,re.M)
    stock=definition("root-normal",src)
    stock_lean=definition("root-ignore-dev",src,"--ignore_dev_dependency")
    if len(stock)!=10 or not graph_has_patch(stock):errors.append("stock definition coverage")
    if stock_lean!=stock:errors.append("stock named repository definitions differ with --ignore_dev_dependency")
    with tempfile.TemporaryDirectory(dir=temp) as w:
        work=Path(w)
        # Pinned, untouched upstream source. The consumer's archive_override
        # removes only these declarations from the dependency's copy of MODULE.
        archive=work/"gvisor.tar.gz"
        subprocess.run(["git","archive","--format=tar.gz","--prefix=gvisor/","-o",archive,base],check=True)
        root=work/"consumer"
        root.mkdir()
        (root/".bazelversion").write_bytes((src/".bazelversion").read_bytes())
        patch="".join(difflib.unified_diff(mod.splitlines(True),before.splitlines(True),fromfile="a/MODULE.bazel",tofile="b/MODULE.bazel"))
        (root/"root-policy.patch").write_text(patch)
        (out/"root-policy.patch").write_text(patch)
        (root/"BUILD.bazel").write_text('exports_files(["root-policy.patch"])\n')
        deps="\n".join(re.search(r'^bazel_dep\(name = "'+n+r'",.*?\)$',mod,re.M)[0] for n in ("rules_go","gazelle","platforms"))
        manifest='module(name="gvisor_consumer")\nbazel_dep(name="gvisor",version="0.0.1")\n'+deps+'\n'
        manifest+=f'archive_override(module_name="gvisor",urls=["{archive.as_uri()}"],strip_prefix="gvisor",patches=["//:root-policy.patch"],patch_strip=1,integrity="sha256-{base64.b64encode(bytes.fromhex(sha(archive.read_bytes()))).decode()}")\n'
        manifest+='go_deps = use_extension("@bazel_gazelle//:extensions.bzl","go_deps")\n'
        manifest+='use_repo(go_deps,'+",".join(json.dumps(n) for n in repos)+')\n'
        readopted=tail.replace('patches = ["//tools:', 'patches = ["@gvisor//tools:')
        assert readopted!=tail
        rows["fixture"]={"archive_sha256":sha(archive.read_bytes()),"archive_git_source":base,"stock_module_sha256":sha(mod.encode()),"dependency_module_sha256":sha(before.encode()),"patch_sha256":sha(patch.encode()),"root_policy_sha256":sha(readopted.encode())}
        (root/"MODULE.bazel").write_text(manifest)
        bare=definition("consumer-without-policy",root,"--ignore_dev_dependency")
        if len(bare)!=10 or graph_has_patch(bare):errors.append("bare consumer did not demonstrate unpatched shared graph")
        (root/"MODULE.bazel").write_text(manifest+readopted)
        (out/"consumer.MODULE.bazel").write_text((manifest+readopted).replace(archive.as_uri(),"<pinned-upstream-archive-url>"))
        adopted=definition("consumer-adopts-policy",root,"--ignore_dev_dependency")
        if adopted!=stock:errors.append("adopted consumer definition mismatch")
        # Plain Linux client, with the same pure-Go mode as gVisor normally
        # sets in .bazelrc. No local-client paths are sent to a different worker.
        p=call("actual-library",root,"build","--jobs=4","--ignore_dev_dependency","--platforms=@platforms//host","--@io_bazel_rules_go//go/config:pure","@gvisor//pkg/gvisordetect")
        outputs=archives(root)
        rows["actual-library"]["outputs"]=[{"path":str(path.relative_to(root)),"size":path.stat().st_size,"sha256":digest} for path,digest in outputs]
        if p.returncode or not outputs:errors.append("no downstream gvisordetect archive")
        # Fetch the selected module by its real target and inspect the actual
        # post-patch source (gvisordetect itself only imports the standard library).
        q=call("protobuf-materialized",root,"query","--ignore_dev_dependency","@org_golang_google_protobuf//internal/detrand:all")
        ob=call("consumer-output-base",root,"info","output_base")
        files=[f for f in (Path(ob.stdout.strip())/"external").glob("*+org_golang_google_protobuf/internal/detrand/rand.go") if f.is_file()] if ob.returncode==0 else []
        rows["protobuf-materialized"]["files"]=[{"sha256":sha(f.read_bytes()),"zero_seed":"var randSeed uint64" in f.read_text(),"binary_hash":"binaryHash()" in f.read_text()} for f in files]
        if q.returncode or len(files)!=1 or not all("var randSeed uint64" in f.read_text() and "binaryHash()" not in f.read_text() for f in files):errors.append("consumer's actual runtime source was not patched")
except Exception:
    errors.append(traceback.format_exc())
finally:
    (out/"verdict.json").write_text(json.dumps({"source":base,"ok":not errors,"start":started,"finished":now(),"errors":errors,"rows":rows},indent=2)+"\n")
    print(errors,flush=True)
sys.exit(bool(errors))
