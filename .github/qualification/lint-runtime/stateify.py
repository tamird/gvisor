"""Project the original failing actions from two verified invocations."""

from collections import Counter
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile

import check as h

sys.path.insert(0, str(h.ROOT))
from cas_inputs import blob, fetch, rpc

PRODUCT = '778375bc602d883faf89a2f5b7bdf4016804da10'
CARRIER = '674c9bf24453910174c718b2fcc4365bbab248c6'
INV = {'candidate_imports': '97df76f6-fe2c-4ec1-9864-8b75fcee554f',
       'five_with_seccomp_inputs': '602e0b38-0fdb-4ba7-b747-4ff3075eee95'}
REPORT = h.ROOT / 'stateify-native-review.json'
OUT = dict(verdict='INCOMPLETE', product=PRODUCT, source=CARRIER, workload_repeated=False,
           started=h.utc(), original_artifact=13255863288, invocations={})
ROOTS = ('pkg/', 'runsc/', 'tools/', 'external/', 'test/', 'bazel-out/')
PAT = re.compile(r'(?<![a-zA-Z0-9_/.+-])(?://|@@?[A-Za-z0-9_+.-]+//)[A-Za-z0-9_./+-]*:[A-Za-z0-9_.+-]+')
CODES = ('NO_USABLE_STRATEGY_FOUND', 'NON_ZERO_EXIT', 'EXECUTION_DENIED', 'REMOTE_EXECUTOR_OVERLOADED',
         'REMOTE_ERROR', 'UNAVAILABLE', 'OUT_OF_MEMORY', 'MISSING_INPUT', 'MISSING_DEPENDENCY',
         'SOURCE_INPUT_MISSING', 'UNDECLARED_INCLUDE', 'PERMISSION_DENIED', 'PACKAGE_LOADING_FAILURE',
         'REPOSITORY_FETCH_FAILED', 'ACTION_NOT_UP_TO_DATE', 'OUTPUT_MISSING', 'UNKNOWN')
TOOLS = ('GoStateify', 'PrecompileSeccomp', 'GoStdlib', 'GoStdlibList', 'GoCompilePkg', 'GoLink', 'GoAsm',
         'GoCompile', 'GoPack', 'GoStaticAnalysis', 'GoNogo', 'GoMarshal', 'GoImports', 'Genrule',
         'GoTemplate', 'GoToolBinary', 'CppCompile', 'CppLink', 'TemplateExpand', 'CheckDeps')
PHRASES = ('ERROR:', 'FAILED:', 'error executing', 'failed to execute', 'Exec format error', 'could not import',
           'undefined:', 'no such package', 'no such target', 'not visible', 'cannot find package',
           'missing strict dependencies', 'missing strict deps', 'no matching toolchains', 'No usable strategy',
           'execvp(', 'No such file or directory', 'not found', 'Permission denied', 'Operation not permitted',
           'internal compiler error', 'no space left on device', 'Cannot allocate memory', 'Killed',
           'error running subcommand', 'error running the compiler', 'error running the linker',
           'wrong ELF class', 'syntax error', 'could not load', 'invalid import path', 'not used',
           'does not implement', 'cannot convert', 'mismatched types', 'has no field or method',
           'out of memory', 'signal:', 'undeclared inclusion', 'not declared', 'not a package',
           'build constraints exclude', 'incompatible', 'cannot use', 'too many arguments',
           'not enough arguments', 'not in std', 'missing tool', 'inconsistent vendoring', 'exec format',
           'no import for', 'generated code uses', 'cannot refer to unexported', 'expected',
           'finding', 'FileNotFoundError', 'fatal error:', 'Toolchain', 'unrecognized command-line',
           'conflicts with', 'is not allowed', 'invalid reference', 'duplicate', 'import cycle',
           'not supported', 'deadline exceeded', 'connection refused', 'failed to find', 'failed to open',
           'not an executable', 'go:embed', 'failed to resolve', 'not under', 'not available', 'No source files')
WORDS = set(TOOLS + CODES + ('internal/testpty','clock','shapes','peer','Record','StateFields','RegisterIdent',
             'StateSave','StateLoad','StateTypeName','BPFInstruction','nogo','testpty','filterStdPackages',
             'FilterStdPackages','source','sourceFiles','stdlib','goimports','go','compilepkg','compile','go_marshal',
             'gcc','clang','aarch64-linux-gnu-gcc','x86_64-linux-gnu-gcc','bash','cat','sh','grep','qemu',
             'gvisor.dev/gvisor/pkg/abi/linux','gvisor.dev/gvisor/pkg/state','example.invalid/unrelated/peer',
             'archive','GoArchive','buildifier','analysis','analyzing','analyzer','config','stateify','GOCACHE',
             'GOARCH','GOOS','GOROOT','toolchain','amd64','arm64','linux','state','reference','plugin',
             'srcs','importpath','compilepkg','sdk','x86_64-linux-gnu-as','go_asm','cc','cc1','cgo','ld',
             'marshal','types','link','linker','builder','strict','deps','export','data','precompile','go_deps',
             'failed','cannot','error','missing','shell','file','package','directory','format','platform','worker'))
# Only source-known package paths are public; external labels can be queried but their
# original spelling never leaves the reader.
PKGS = {str(Path(p).parent) for p in h.git('ls-tree','-r','--name-only',CARRIER).decode().splitlines()
        if Path(p).name in ('BUILD','BUILD.bazel')}
SYMBOLS = set(re.findall(r'[A-Za-z_]\w{2,}', h.git('show',PRODUCT+':tools/go_stateify/main.go').decode()))
TOKEN = re.compile(r'[A-Za-z0-9_./+-]+')
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def save():
    REPORT.write_text(json.dumps(OUT,sort_keys=True,indent=2)+'\n')


def safe_label(label):
    if label.startswith('//') and label[2:].split(':')[0] in PKGS and PAT.fullmatch(label):
        # Rule names are compiler-generated or source-owned; require static identifier characters.
        return label
    return 'external-or-unknown:'+h.digest(label.encode())[:16]


def project(data):
    text=ANSI.sub('',data.decode(errors='replace'))
    rows=[]
    for line in text.splitlines():
        line=line[:8192]
        marks=[p for p in PHRASES if p.casefold() in line.casefold()]
        codes=[v for v in CODES if v in line]
        if not marks and not codes:
            continue
        labels=sorted({safe_label(l) for l in PAT.findall(line)})[:8]
        words=[w for w in TOKEN.findall(line) if w in WORDS or w in SYMBOLS][:40]
        rows.append(dict(sha256=h.digest(line.encode()),classes=marks,codes=codes,owners=labels,words=words))
        if len(rows)==48:
            break
    return dict(bytes=len(data),sha256=h.digest(data),lines=rows)


def inspect(original,name,uid):
    prior=original['phases'][name]
    assert prior['invocation']==uid and prior['exit']==1
    targets={l for l,ok in prior['completed'].items() if not ok}
    if name=='candidate_imports':
        targets={l for l in targets if l.endswith(':qualification_identtype')}
    assert len(targets)==(1 if name=='candidate_imports' else 5)
    row=OUT['invocations'][name]=dict(invocation=uid,original_exit=1,original_failures=sorted(targets),console={},actions=[])
    save()
    def guard(tag,func):
        try:
            func()
        except Exception as exc:
            row.setdefault('unavailable',{})[tag]=type(exc).__name__
        save()

    def logs():
        # API tail and full forward stream have distinct semantics.
        seq=[]
        for tail in (True,False):
            token=None if tail else '0'
            chunks=[]
            complete=False
            for _ in range(1 if tail else 96):
                payload={'selector':{'invocation_id':uid}}
                if token is not None: payload['page_token']=token
                page=json.loads(fetch('api/v1/GetLog',payload))
                chunk=page.get('log',{}).get('contents','').encode()
                chunks.append(chunk)
                assert sum(map(len,chunks)) <= 12*1024**2
                token=page.get('next_page_token',page.get('nextPageToken'))
                if not token:
                    complete=True
                    break
            data=b''.join(chunks)
            row['console']['tail' if tail else 'forward']=dict(complete=complete, pages=len(chunks),**project(data))
            for line in ANSI.sub('',data.decode(errors='replace')).splitlines():
                if 'failed' in line.lower() or 'error:' in line.lower():
                    seq.extend(PAT.findall(line[:8192]))
            save()
        return seq
    captured=[]
    guard('console',lambda:captured.extend(logs()))
    def roots():
        res=rpc('GetTarget',dict(invocationId=uid,status=3))
        for gr in res.get('targetGroups',[]):
            for target in gr.get('targets',[]):
                if target.get('rootCause') or target.get('metadata',{}).get('rootCause'):
                    captured.append(target['metadata']['label'])
            row['root_list_more']=bool(gr.get('nextPageToken'))
    guard('roots',roots)

    seen=set()
    candidates=sorted(targets)+list(dict.fromkeys(captured))
    for label in candidates:
        if label in seen or len(seen)>=25 or not PAT.fullmatch(label): continue
        seen.add(label)
        def actions():
            res=rpc('GetTarget',dict(invocationId=uid,targetLabel=label,status=3))
            for group in res.get('targetGroups',[]):
                for target in group.get('targets',[]):
                    assert target['metadata']['label']==label
                    for event in target.get('actionEvents',[]):
                        ident=event['id']['actionCompleted']; act=event['action']
                        assert ident['label']==label and re.fullmatch('[a-f0-9]{64}',ident['configuration']['id'])
                        code=act.get('exitCode',0)
                        if not code: continue
                        rec=dict(label=safe_label(label),config=ident['configuration']['id'],exit=code,
                                 mnemonic=act['type'] if act.get('type') in TOOLS else 'other',streams=[])
                        row['actions'].append(rec);save()
                        for key in ('failureDetail','failure_detail'):
                            if key in act: rec['failure']=project(json.dumps(act[key],sort_keys=True).encode())
                        for stream in ('stderr','stdout'):
                            f=act.get(stream,{})
                            m=re.fullmatch(r'bytestream://remote\.buildbuddy\.io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/(\d+)',f.get('uri',''))
                            if m and 0<int(m[2])<=2*1024**2:
                                data=blob({'hash':m[1],'sizeBytes':m[2]},uid)
                                assert len(data)==int(m[2]) and h.digest(data)==m[1]
                                rec['streams'].append(dict(kind=stream,**project(data)))
                        save()
        guard(safe_label(label),actions)
    row['owners_queried']=len(seen)
    row['observed_diagnostic']=any(rec.get('failure',{}).get('lines') or any(s['lines'] for s in rec['streams'])
                                   for rec in row['actions']) or any(c['lines'] for c in row['console'].values())
    save()


try:
    raw=(h.ROOT/'original.zip').read_bytes();assert h.digest(raw)=='6fea03c7b4adfccecd0fd4ad524fefcaa4ff58897ab5c90a48f7c6d88b96436e'
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member,=archive.namelist();assert member=='stateify-native-review.json'
        original=json.loads(archive.read(member))
    assert original['source']==PRODUCT and original['verdict']=='FAIL'
    assert original['phases']['exported_tree_480']['exact']==480 and original['phases']['seccomp_exported_inputs_only']['exact']==5
    assert original['phases']['in_tree_sibling_hook']['load_hook_xml_cases']==16
    assert 'SECRET_EXAMPLE' not in json.dumps(project(b'ERROR: Authorization: Bearer SECRET_EXAMPLE'))
    OUT['original_zip_sha256']=h.digest(raw)
    for name, uid in INV.items(): inspect(original,name,uid)
    OUT['verdict']='ORIGINAL_RECORDS_READ' if all(r.get('observed_diagnostic') for r in OUT['invocations'].values()) else 'INCOMPLETE'
except Exception as exc:
    OUT['reader_error']=type(exc).__name__
finally:
    OUT['finished']=h.utc();save()
sys.exit(OUT['verdict']!='ORIGINAL_RECORDS_READ')
