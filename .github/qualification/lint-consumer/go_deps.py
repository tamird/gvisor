"""Observe the actual sources produced by the root and external Gazelle usages."""
from hashlib import sha256
from pathlib import Path
import re
import subprocess
import sys
import audit as h

PROTO = '@org_golang_google_protobuf//internal/detrand:rand.go'
CONTAINERD = '@com_github_containerd_containerd_v2//:all'
PRODUCT = 'e275071db84cbcdad75df9fed1361a5a0e719c85'
h.KEYS += ('Using the "go_deps.gazelle_override" tag in a non-root Bazel module',
           'Using the "go_deps.module_override" tag in a non-root Bazel module',
           'kernel_headers.configure', 'not defined', 'does not have the specified attribute', 'cannot be used in non-root modules',
           'can only be used in the root module', 'non-root module', 'not generated',
           'does not generate repository', 'does not contain symbol', 'no such package',
           'does not appear to be a workspace', 'GoCompilePkg', 'GoStdlib', 'GoToolchainBinaryBuild',
           'gazelle_override', 'module_override', 'build_directives', 'detrand', 'protobuf',
           'not found in repository', 'Analysis failed', 'Unable to complete request',
           'NonZeroExit', 'No matching toolchains', 'failed to fetch', 'certificate', '403 Forbidden', 'timed out')

def public_diagnosis(row):
    s = (h.RAW/(row['name']+'.stderr')).read_text(errors='replace')
    row['known_details'] = sorted({m for m in h.KEYS if m in s})
    # Names from parsed repository diagnostics, not full command strings.
    repos = ('kernel_headers','go_sdk','gazelle','go_deps','rules_go','bazel_skylib','rules_cc','llvm','zstd','coral_crosstool','libpluginstack','google_benchmark','protobuf','com_google_protobuf','org_golang_google_protobuf','com_github_containerd_containerd_v2','gvisor','rules_python','rules_proto','com_github_burntsushi_toml','bazel_tools','platforms','com_github_bazelbuild_rules_go')
    row['diagnostic_repo_names'] = sorted({k for k in repos if any(k in line for line in s.splitlines() if 'error' in line.lower() or 'fail' in line.lower())})
    row['forbidden_tags'] = sorted(set(re.findall(r'Using the "(go_deps[.][a-z_]+)" tag in a non-root Bazel module',s)))
    row['sdk_scope'] = [k for k in ('go_sdk.from_file','go_sdk.download', 'go_sdk.host', 'patches') if k in s]
    row['unknown_symbols'] = [k for k in ('GoSDK','CcInfo','GoArchive','GoInfo','go_cross_binary','go_info','configure','GoSDKInfo') if k in s and 'does not contain symbol' in s]
    h.publish()

def run(name,cwd,command,args,**kwargs):
    row=h.run(name,cwd,command,args,**kwargs)
    public_diagnosis(row)
    return row

def source_file(name,cwd,flags):
    row=run(name,cwd,'query',[*flags,'--output=location',PROTO])
    if row['exit']:
        return
    loc=(h.RAW/(name+'.stdout')).read_text().splitlines()
    matches=[m.group(1) for line in loc if (m:=re.match(r'^(.*?):\d+:\d+: .*',line))]
    if len(matches)!=1:
        row['probe_error']='missing unique input location'
        h.publish()
        return
    p=Path(matches[0])
    # An implicit Bazel InputFile sometimes reports its owning BUILD location.
    if p.name in ('BUILD', 'BUILD.bazel') and p.parent.as_posix().endswith('internal/detrand'):
        p=p.with_name('rand.go')
    q=p if p.is_absolute() else cwd/p
    if not q.is_file():
        info=subprocess.check_output(['bazelisk','--nosystem_rc','--nohome_rc','--noworkspace_rc','info','output_base'],cwd=cwd,stderr=subprocess.DEVNULL).decode().strip()
        q=Path(info)/p
    if not q.as_posix().endswith('/internal/detrand/rand.go') or not q.is_file():
        row['probe_error']='cannot resolve declared protobuf source path'
        h.publish()
        return
    b=q.read_bytes()
    row['protobuf_source']={'sha256':h.sha(b),'size':len(b),'reads_executable':b'os.Executable()' in b and b'os.Open(' in b,'disabled_seed':b'var randSeed uint64' in b}
    h.publish()

def one(name,cwd,flags=(),build=False):
    source_file(name+'_proto',cwd,flags)
    row=run(name+'_policy',cwd,'query',[*flags,'--output=location','buildfiles('+CONTAINERD+')'])
    if row['exit']==0:
        names=(h.RAW/(row['name']+'.stdout')).read_text().splitlines()
        paths={Path(m.group(1)) for line in names if (m:=re.match(r'^(.*?):\d+:\d+: .*',line)) and '/com_github_containerd_containerd_v2/' in line}
        # Canonical repo names have a '+' prefix before the hub's apparent name.
        paths|={Path(m.group(1)) for line in names if (m:=re.match(r'^(.*?):\d+:\d+: .*',line)) and 'com_github_containerd_containerd_v2/BUILD' in line}
        candidates=[p for p in paths if p.is_file() and p.name in ('BUILD','BUILD.bazel')]
        if len(candidates)==1:
            b=candidates[0].read_bytes()
            row['containerd_root_build']={'sha256':h.sha(b),'size':len(b),'proto_disable':b'# gazelle:proto disable\n' in b}
            h.publish()
    run(name+'_waiter',cwd,'query',[*flags,'//pkg/waiter:waiter' if cwd==h.SOURCE else h.TARGET])
    if build:
        run(name+'_build',cwd,'build',[*flags,'--jobs=24','//pkg/waiter:waiter' if cwd==h.SOURCE else '//:pub'],remote=True)

def main():
    for p in (h.AREA,h.CONSUMER,h.RAW,h.OUT): p.mkdir(exist_ok=True)
    h.select(h.MASTER)
    src=(h.SOURCE/'MODULE.bazel').read_text()
    before='go_deps.module_override('
    tags=before+src.split(before,1)[1]
    assert tags.count('go_deps.gazelle_override(')==9 and tags.count('go_deps.module_override(')==1
    consumer_prefix=f'''module(name="consumer")
bazel_dep(name="gvisor", version="0.0.1")
local_path_override(module_name="gvisor", path="{h.SOURCE}")
bazel_dep(name="gazelle", version="0.51.3", repo_name="bazel_gazelle")
go_deps=use_extension("@bazel_gazelle//:extensions.bzl","go_deps")
use_repo(go_deps,"org_golang_google_protobuf","com_github_containerd_containerd_v2")
'''
    (h.CONSUMER/'BUILD').write_text(f'alias(name="pub",actual="{h.TARGET}")\n')
    outer=h.CONSUMER/'MODULE.bazel'
    outer.write_text(consumer_prefix)
    h.REPORT.update(parent=h.MASTER,product=PRODUCT,policies={'gazelle_overrides':9,'module_overrides':1,'tags_sha256':h.sha(tags.encode()),'consumer_base':h.sha(consumer_prefix.encode())})
    try:
        run('stock_outer_go_deps',h.CONSUMER,'query',['--output=location',PROTO])
        one('stock_root',h.SOURCE,build=True)
        h.select(PRODUCT)
        assert (h.SOURCE/'MODULE.bazel').read_text().endswith(tags.replace('go_deps.','root_go_deps.'))
        one('normal_root',h.SOURCE)
        one('ignored_root',h.SOURCE,['--ignore_dev_dependency'])
        one('outer_own_defaults',h.CONSUMER)
        outer_tags=tags.replace('"//tools:protobuf.disable-binary-reading-on-init.patch"', '"@gvisor//tools:protobuf.disable-binary-reading-on-init.patch"')
        assert outer_tags!=tags
        outer.write_text(consumer_prefix+outer_tags)
        h.REPORT['policies']['consumer_adoption']=h.sha(outer.read_bytes())
        one('adopting_outer',h.CONSUMER,build=True)
        rows={r['name']:r for r in h.REPORT['rows']}
        def proto(n): return rows[n+'_proto'].get('protobuf_source',{})
        h.REPORT['controls']={
            'stock_patched':proto('stock_root').get('disabled_seed') is True,
            'normal_root_preserved':bool(proto('stock_root')) and proto('stock_root')==proto('normal_root'),
            'ignore_drops_patch':proto('ignored_root').get('reads_executable') is True,
            'plain_consumer_drops_patch':proto('outer_own_defaults').get('reads_executable') is True,
            'adopting_root_preserves_patch':bool(proto('stock_root')) and proto('adopting_outer')==proto('stock_root'),
        }
    finally:
        h.publish()
    return 0 if all(h.REPORT['controls'].values()) else 1

if __name__=='__main__':
    sys.exit(main())
