"""Give a consuming root explicit ownership of gVisor's shared Go overrides."""
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
base = "1d96ea04c417f23fd7d90abb453e93163aa83c1a"
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
def archives(root):
    return [(p,sha(p.read_bytes())) for p in (root/"bazel-bin"/"external").glob("gvisor*/pkg/gvisordetect/**/*") if p.is_file() and p.suffix in (".a",".x")]
try:
    started=now()
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==base
    mod=(src/"MODULE.bazel").read_text()
    before,sep,rest=mod.partition("go_deps.module_override(")
    tail=sep+rest
    assert tail.count("go_deps.module_override(")==1 and tail.count("go_deps.gazelle_override(")==9 and not re.search(r"^go_deps\.(?!gazelle_override|module_override)",tail,re.M)
    rows["control"]={"source":"aa767e4c43d011121271551fe24da501a56f729d","receipt":"https://github.com/tamird/gvisor/actions/runs/41220013696/artifacts/13050115599"}
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
        if sha(readopted.encode())!="d1a1239a66efeccb513a0614aa594c93fd932f07c8b5764aaf637c82436080da":errors.append("consuming root policy changed")
        (root/"MODULE.bazel").write_text(manifest+readopted)
        (out/"consumer.MODULE.bazel").write_text((manifest+readopted).replace(archive.as_uri(),"<pinned-upstream-archive-url>"))
        rm=call("root-mapping",root,"mod","dump_repo_mapping","")
        root_map=json.loads(rm.stdout) if rm.returncode==0 else {}
        gm=call("gvisor-mapping",root,"mod","dump_repo_mapping",root_map["gvisor"]) if "gvisor" in root_map else None
        gvisor_map=json.loads(gm.stdout) if gm and gm.returncode==0 else {}
        dev={"lint_inputs","cpplint","buildifier_prebuilt"}
        unexpected=sorted({label for label,canonical in gvisor_map.items() if label in dev or any(name in canonical.split("+") for name in dev)})
        rows["mapping"]={"count":len(gvisor_map),"unexpected":unexpected,"known_production":sorted({"rules_python","org_golang_google_protobuf","bazel_lib"}.intersection(gvisor_map))}
        if len(rows["mapping"]["known_production"])!=3 or unexpected:errors.append("gVisor mapping did not isolate the root-only tools")
        lock_path=root/"MODULE.bazel.lock"
        if lock_path.exists():
            registry=json.loads(lock_path.read_text()).get("registryFileHashes",{})
            rows["mapping"]["unexpected_registry"]=sorted(u for u in registry if any("/modules/"+name+"/" in u for name in dev))
            if rows["mapping"]["unexpected_registry"]:errors.append("registry selected a root-only lint module")
        # Plain Linux client, with the same pure-Go mode as gVisor normally
        # sets in .bazelrc. No local-client paths are sent to a different worker.
        p=call("actual-library",root,"build","--jobs=4","--verbose_failures","--platforms=@platforms//host","--@io_bazel_rules_go//go/config:pure","@gvisor//pkg/gvisordetect")
        outputs=archives(root)
        rows["actual-library"]["outputs"]=[{"path":str(path.relative_to(root)),"size":path.stat().st_size,"sha256":digest} for path,digest in outputs]
        if p.returncode or not outputs:errors.append("no downstream gvisordetect archive")
        # Fetch the selected module by its real target and inspect the actual
        # post-patch source (gvisordetect itself only imports the standard library).
        q=call("protobuf-materialized",root,"query","@org_golang_google_protobuf//internal/detrand:all")
        ob=call("consumer-output-base",root,"info","output_base")
        files=[f for f in (Path(ob.stdout.strip())/"external").glob("*+org_golang_google_protobuf/internal/detrand/rand.go") if f.is_file()] if ob.returncode==0 else []
        rows["protobuf-materialized"]["files"]=[{"sha256":sha(f.read_bytes()),"zero_seed":"var randSeed uint64" in f.read_text(),"binary_hash":"binaryHash()" in f.read_text()} for f in files]
        if q.returncode or len(files)!=1 or not all(sha(f.read_bytes())=="eb7bf9d4c9be19de92651a4dc31701c80beef92e04a5c89ab14a1902aa4618f2" for f in files):errors.append("consumer's actual runtime source was not patched")
except Exception:
    errors.append(traceback.format_exc())
finally:
    (out/"verdict.json").write_text(json.dumps({"source":base,"ok":not errors,"start":started,"finished":now(),"errors":errors,"rows":rows},indent=2)+"\n")
    print(errors,flush=True)
sys.exit(bool(errors))
