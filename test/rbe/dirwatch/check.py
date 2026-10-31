import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

cwd = Path.cwd()
supplier = cwd / 'supplier'
carrier = cwd / 'carrier'
result = Path(os.environ['RUNNER_TEMP']) / 'dirwatch-report'
result.mkdir(exist_ok=True)
report = {'verdict': 'INCOMPLETE', 'phases': {}, 'errors': []}
tests = ['test_path_readdir_deleted_dir', 'test_path_readdir_deleted_dir_without_watch', 'test_watch_tree_deleted_dir', 'test_path_readdir_unreadable_dir']

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def save():
    (result/'verdict.json').write_text(json.dumps(report, indent=2)+'\n')

def run(name, args, root, expect=True, contains=None, timeout=6000):
    started = time.monotonic()
    row = report['phases'][name] = {'command': list(map(str,args)), 'expected_success': expect, 'started': now()}
    save()
    path = result/(name+'.log')
    with path.open('wb') as log:
        p = subprocess.run(args, cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    text = path.read_text(errors='replace')
    row.update(exit=p.returncode, finished=now(), seconds=round(time.monotonic()-started,2), matches=[i for i in (contains or []) if i in text])
    save()
    if (p.returncode == 0) != expect or len(row['matches']) != len(contains or []):
        print(name, p.returncode, text[-4500:], flush=True)
        raise RuntimeError(name+' unexpected result')
    print(name, p.returncode, flush=True)
    return text

def control(label, binary, fixed):
    root=Path(os.environ['RUNNER_TEMP'])/('watch-'+label)
    root.mkdir()
    (root/'MODULE.bazel').write_text('module(name="probe")\nlint_sources = use_repo_rule("//:lint_sources.bzl", "lint_sources")\nlint_sources(name="lint_inputs")\n')
    (root/'BUILD').touch()
    (root/'.bazelversion').write_text('8.8.2\n')
    (root/'lint_sources.bzl').write_bytes((carrier/'test/rbe/dirwatch/lint_sources.bzl').read_bytes())
    fixed_file=root/'keep.h';fixed_file.touch()
    package=root/'newpkg';nested=package/'nested'
    nested.mkdir(parents=True);(package/'BUILD.bazel').touch();(nested/'BUILD.bazel').touch();(nested/'fresh.h').touch()
    query=[binary, 'cquery','--color=no','--curses=no','--output=files','@lint_inputs//:cpplint']
    run(label+'_new',query,root,contains=['ROOT.d/newpkg/nested/fresh.h','ROOT.d/keep.h'])
    (nested/'fresh.h').unlink();(nested/'BUILD.bazel').unlink();nested.rmdir();(package/'BUILD.bazel').unlink();package.rmdir()
    msg=run(label+'_deleted',query,root,expect=fixed,contains=['ROOT.d/keep.h'] if fixed else ['no longer an existing directory'])
    if fixed:
        if 'ROOT.d/newpkg/nested/fresh.h' in msg: raise ValueError('obsolete source still in list')
        package.mkdir();(package/'BUILD.bazel').touch();(package/'other.h').touch()
        msg=run(label+'_recreated',query,root,contains=['ROOT.d/newpkg/other.h','ROOT.d/keep.h'])
        if 'ROOT.d/newpkg/nested/fresh.h' in msg: raise ValueError('new source still mapped to old package')

def check_xml(name, path, test_names=None):
    xml=ET.parse(path)
    records=[(t.attrib.get('classname'),t.attrib.get('name')) for t in xml.iter('testcase') if t.find('skipped') is None and t.attrib.get('status','run')!='notrun']
    if not records or any(t.tag in ('failure','error') for t in xml.iter()): raise RuntimeError(name+' missing or failing XML test records')
    if test_names:
        if sorted(n for _,n in records)!=sorted(test_names): raise RuntimeError(name+': expected '+str(test_names)+', got '+str(records))
    elif not any('StarlarkRepositoryContextTest' in str(c) for c,_ in records):
        raise RuntimeError('Java test did not include the changed fixture')
    (result/(name+'.xml')).write_bytes(path.read_bytes())
    report['phases'][name]['executed']=len(records)
    report['phases'][name]['cases']=test_names or sorted({c for c,_ in records if c})

shell='//src/test/shell/bazel:starlark_repository_test'
unit='//src/test/java/com/google/devtools/build/lib/bazel/repository/starlark:StarlarkTests'
def suite(name, target, more):
    run(name,['bazel','test','--jobs=4','--test_output=errors','--cache_test_results=no','--test_sharding_strategy=disabled',*more,target],supplier)
    loc=supplier/'bazel-testlogs'/target[2:].replace(':','/')/'test.xml'
    check_xml(name,loc,tests if name=='shell' else None)

try:
    report['source']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=supplier,text=True).strip()
    report['parent']=subprocess.check_output(['git','rev-parse','HEAD^'],cwd=supplier,text=True).strip()
    assert report['source']=='01c3a89b0c045a13f5a437b7fd0300ef749d064e' and report['parent']=='3bfdf3b354ae822628647997e43015744a7acda6'
    report['started']=now();save()
    for name, fn in [('stock', lambda:control('stock','bazel',False)),('build',lambda:run('build',['bazel','build','--jobs=4','--embed_label=8.9.0-dirwatch','//src:bazel'],supplier))]:
        try:fn()
        except (RuntimeError,ValueError,OSError,subprocess.SubprocessError) as ex:report['errors'].append(str(ex));save()
    if report['phases'].get('build',{}).get('exit')==0:
        for name, fn in [('fixed',lambda:control('fixed',str(supplier/'bazel-bin/src/bazel'),True)),('unit',lambda:suite('unit',unit,[])),('shell',lambda:suite('shell',shell,['--test_filter='+':'.join(tests)]))]:
            try:fn()
            except (RuntimeError,ValueError,OSError,subprocess.SubprocessError) as ex:report['errors'].append(str(ex));save()
    required={'stock_new','stock_deleted','build','fixed_new','fixed_deleted','fixed_recreated','unit','shell'}
    report['expected']=sorted(required);report['missing']=sorted(required-set(report['phases']))
    if not report['errors'] and not report['missing']:report['verdict']='PASS'
finally:
    report['verified_at']=now();save()
if report['verdict']!='PASS':sys.exit(1)
