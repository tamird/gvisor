"""Run the real format entry point against a disposable source checkout."""
from datetime import datetime, timezone
from hashlib import sha256
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

SOURCE = "3cd9523a5b5a94d16df9b4f6383316e5591ac56a"
TREE = "91e311fb62db0d862a490a58ba100bab54339e1d"
SUPPLIER = "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7"
IMAGE = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
out = Path(os.environ["RUNNER_TEMP"])
root = Path.cwd()
report = dict(verdict="INCOMPLETE", phases={})
entry = "//tools/lint:format"
check_target = "//tools/lint:buildifier"
expected = ['initial_check','compatibility','reject_cpp','format_one','format_both','recheck','config_only','new_style_fix','require_root','scratch_amd64','scratch_arm64']
report["expected"] = expected

def now():
    return datetime.now(timezone.utc).isoformat()

def save():
    (out / "workspace-format.json").write_text(json.dumps(report, indent=2) + "\n")

def require(ok, message):
    if not ok:
        raise RuntimeError(message)

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

def command(phase, mode, target, cwd=root, diagnostic=None, arch=None):
    invocation = str(uuid.uuid4())
    row = report['phases'][phase] = dict(target=target, mode=mode, started=now(), invocation=invocation, expected_success=diagnostic is None)
    opts = ['--jobs=24', '--invocation_id='+invocation]
    if arch is None:
        opts += ['--config=x86_64','--remote_default_exec_properties=OSFamily=linux','--remote_default_exec_properties=Arch=amd64','--remote_default_exec_properties=EstimatedMemory=2GB','--remote_default_exec_properties=workload-isolation-type=firecracker','--remote_default_exec_properties=container-image='+IMAGE]
        if mode == 'test': opts += ['--cache_test_results=no','--test_output=errors']
    else:
        platform='@cpplint_qualification//:scratch_'+arch
        opts += ['--host_platform='+platform,'--extra_execution_platforms='+platform,'--platforms=//tools/bazeldefs:linux_'+('amd64' if arch=='amd64' else 'arm64'),'--inject_repository=cpplint_qualification='+str(out/'cpplint_qualification'),'--noremote_accept_cached','--execution_log_json_file='+str(out/(phase+'.exec'))]
    save()
    result=subprocess.run(['bazel','--bazelrc='+os.environ['QA_RC'],mode,*opts,target],cwd=cwd,capture_output=True,text=True)
    msg=result.stdout+result.stderr
    row.update(exit=result.returncode,finished=now(),matched=diagnostic is None or diagnostic in msg)
    if result.returncode:
        row['messages']=[s[:600] for s in msg.splitlines() if any(x in s.lower() for x in ('error:', 'warning:', 'failed', 'panic:', 'cannot', 'denied', 'incorrect')) and not any(x in s.lower() for x in ('credential','api-key','header=x-','authorization','running command line'))][-12:]
    print(phase,invocation,result.returncode,row.get('messages',[]),flush=True);save()
    require((result.returncode == 0) == (diagnostic is None) and row['matched'],phase+' unexpected result')
    if arch:
        raw=(out/(phase+'.exec')).read_text(); dec=json.JSONDecoder();spawns=[]
        while raw.strip():
            raw=raw.lstrip(); record,n=dec.raw_decode(raw);raw=raw[n:];
            if record.get('mnemonic')=='ClangFormatCheck':spawns.append(record)
        require(len(spawns)==1,'expected one actual ClangFormatCheck action')
        act=spawns[0]; props={p['name']:p['value'] for p in act['platform']['properties']}; files={p['path'].split('/ROOT.d/',1)[1] for p in act['inputs'] if '/ROOT.d/' in p['path']}; tracked=set(git('ls-files','*.c','*.cc','*.h','.clang-format').splitlines());args=act['commandArgs'];row['scratch']=dict(arch=props.get('Arch'),image=props.get('container-image'),runner=act.get('runner'),cache=act.get('cacheHit',False),inputs=len(files),tracked=len(tracked),exit=act.get('exitCode',0),formatter=next(a for a in args if a.endswith('/bin/clang-format')))
        require(tracked<=files and act.get('runner')=='remote' and not act.get('cacheHit') and props.get('Arch')==arch and 'hello-world@sha256:' in props.get('container-image',''),'wrong scratch run or omitted tracked sources');save()

def main():
    report.update(source=git('rev-parse','HEAD'),tree=git('rev-parse','HEAD^{tree}'))
    require(report['source']==SOURCE and report['tree']==TREE and not git('status','--porcelain'),'wrong source/tree')
    command('initial_check','build','//tools/lint:check')
    case=root/'test/util/.clang_format_proof';case.mkdir();(case/'nested').mkdir();(case/'nested/MODULE.bazel').touch()
    control=case/'nested/foreign.cc';control.write_text('int       outside;\n')
    style=case/'.clang-format';style.write_text('BasedOnStyle: LLVM\nDerivePointerAlignment: false\nPointerAlignment: Left\n')
    (case/'.clang-format-ignore').write_text('ignored.cc\n')
    ignored=case/'ignored.cc';ignored.write_text('int      skipped;\n')
    build=case/'BUILD.bazel';build.write_text('filegroup(name="foo")\n'); cc=case/"dollar $ and 'quote.cc";dirty='int     *    ptr;\n';cc.write_text(dirty)
    command('reject_cpp','build','//tools/lint:clang_format',diagnostic=cc.name)
    require(cc.read_text()==dirty,'check mutated C++')
    command('format_one','run','//tools/lint:format_clang',cwd=root/'tools');require(cc.read_text()=='int* ptr;\n' and build.read_text()=='filegroup(name="foo")\n','wrong named fix, style or scope');cc.write_text(dirty)
    command('format_both','run','//tools/lint:format',cwd=root/'tools');require(cc.read_text()=='int* ptr;\n' and build.read_text()=='filegroup(name = "foo")\n','wrong aggregate fix')
    command('recheck','build','//tools/lint:check');require(not git('diff','--name-only'),'modified tracked files')
    style.write_text(style.read_text().replace('Left','Right'));command('config_only','build','//tools/lint:clang_format',diagnostic=cc.name)
    command('new_style_fix','run','//tools/lint:format_clang');require(cc.read_text()=='int *ptr;\n','ignored directory style edit')
    require(control.read_text()=='int       outside;\n' and ignored.read_text()=='int      skipped;\n','rewrote excluded source')
    root_cfg=root/'.clang-format';saved=root_cfg.read_bytes();root_cfg.unlink()
    try: command('require_root','build','//tools/lint:clang_format',diagnostic='.clang-format')
    finally: root_cfg.write_bytes(saved)
    shutil.rmtree(case);require(not git('status','--porcelain'),'dirty final source')

try:
    errors = []
    for label, run in [('workspace', main), ('compatibility', lambda: command('compatibility','test','//tools:lint_test')), *[('scratch_'+arch, lambda arch=arch: command('scratch_'+arch,'build','//tools/lint:clang_format',arch=arch)) for arch in ('amd64','arm64')]]:
        if label.startswith('scratch_'):
            subprocess.run(['git', 'restore', '--', '.clang-format'], check=True)
            shutil.rmtree(root/'test/util/.clang_format_proof', ignore_errors=True)
        try: run()
        except (RuntimeError, ValueError, OSError, IndexError, KeyError) as exc:
            print(label, exc, flush=True);errors.append(label+': '+str(exc))
    report['errors']=errors
    require(not errors and set(report['phases'])==set(expected), 'failed or missing phases')
    report['verdict']='PASS'
finally:
    report['verified_at']=now();save()
