import json, re, statistics, sys, traceback
from pathlib import Path
from xml.etree import ElementTree as ET

root=Path(sys.argv[1]); errors=[]; stats={}; shapes={}; cases={}
def expect(ok,msg):
    if not ok: errors.append(msg)

try:
    for arm,source in [('control','44b82ca830a948708638f20de8e92a025f8c3409'),('candidate','bc0fee8429bb9d1b0bd623803f404d9eea3999d1')]:
        d=root/arm
        try:
            v=json.loads((d/'verdict.json').read_text())
            expect(v['status']=='PASS' and v['source']==source and not v['missing'] and not v['errors'] and v['native_exit']==0, f'{arm}: target verdict {v["status"]}: {v["errors"][:3]}')
            for run in v['executed']:
                expect(run['status']=='PASSED' and not run['cached_locally'] and not run['execution_info'].get('cachedRemotely'),f'{arm}: non-executed/cached target')
            observed={}
            for f in (d/'original').rglob('test.xml'):
                for case in ET.parse(f).iter('testcase'):
                    name=case.get('name')
                    expect(name not in observed,f'{arm}: duplicate {name}')
                    observed[name]=('FAIL' if case.find('failure') is not None or case.find('error') is not None else 'SKIP' if case.find('skipped') is not None or case.get('status')=='notrun' or case.get('result')=='suppressed' else 'PASS')
            want={x:'PASS' for x in ['TestIndexSparseAliasing','TestAddrMap','TestSliceOfArraysInteriorPointer','TestSharedSliceWindows','TestSharedSliceWindows/wire','TestSharedSliceWindows/graph','TestOverlappingArrayPointersAndSlicesGraph']}
            for name in ['SliceThenPtr','PtrThenSlice','SameRangeSliceThenPtrThenOverlappingSlice','AllPermutationsOverlappingArrayPtrAndSlices','OverlappingArrayPointersOnly']:want['TestSliceOfArraysInteriorPointer/'+name]='PASS'
            for name in ['ArrayPtrBeforeOverlappingSlice','SliceBeforeOverlappingArrayPtr','TwoOverlappingArrayPointers','TwoOverlappingInexactInterfaceArrayPointers','MisalignedNestedArrayPointers','CoarseSliceUnusedCapOverlappingFinerArrayPtr','CoarseSliceUnusedCapInsideFinerMergedArray']:want['TestOverlappingArrayPointersAndSlicesGraph/'+name]='PASS'
            for engine in ('wire','graph'):
                for window in ('prefix','middle','empty','suffix','beyond_len','full'):
                    for order in ('true','false'):
                        want[f'TestSharedSliceWindows/{engine}/{window}/window_first={order}']='SKIP' if engine=='wire' and window!='full' else 'PASS'
            absent={k:(s,observed.get(k)) for k,s in want.items() if observed.get(k)!=s}
            expect(not absent,f'{arm}: missing/changed cases {absent}')
            expect(set(observed)==set(want),f'{arm}: unexpected cases {sorted(set(observed)-set(want))}')
            cases[arm]=observed
        except Exception as e: errors.append(f'{arm}: case evidence: {e}')
        try:
            sample={}
            for i in range(1,13):
                rows={}
                for line in (d/f'round-{i}.log').read_text().splitlines():
                    if not line.startswith('Benchmark'):continue
                    words=line.split()
                    name=re.sub(r'-\d+$','',words[0])
                    units={u:float(v) for v,u in zip(words[2::2],words[3::2])}
                    expect(name not in rows,f'{arm}: duplicate {name}')
                    expect(all(u in units for u in ('ns/op','encoded-B','B/op','allocs/op')),f'{arm}: missing metric {name}')
                    rows[name]=units
                expect(set(rows)=={'BenchmarkGraphSave','BenchmarkSparseIndexSave/1','BenchmarkSparseIndexSave/65536','BenchmarkSparseIndexSave/16777216'},f'{arm} round {i}: {sorted(rows)}')
                for n,ms in rows.items():sample.setdefault(n,[]).append(ms)
            stats[arm]={n:{u:statistics.median([x[u] for x in xs]) for u in xs[0]} for n,xs in sample.items()}
            shapes[arm]={n:sorted({r['encoded-B'] for r in xs}) for n,xs in sample.items()}
        except Exception as e: errors.append(f'{arm}: benchmark evidence: {e}')
    planned=[f'{i} {a}' for i in range(1,13) for a in (('control','candidate') if i%2 else ('candidate','control'))]
    expect((root/'order').read_text().splitlines()==planned,'missing paired run(s) or ordering mismatch')
    expect(len((root/'benchstat.txt').read_text().strip())>0,'missing benchstat')
    expect((root/'control'/'bin.sha256').read_text() != (root/'candidate'/'bin.sha256').read_text(),'identical binaries')
    for n in shapes.get('control',{}):
        if n.startswith('BenchmarkSparse'):
            expect(shapes['control'][n]==shapes['candidate'].get(n) and len(shapes['control'][n])==1,f'sparse wire length differs for {n}')
    if 'x86_64' not in (root/'native').read_text():errors.append('not native AMD64')
except Exception:
    errors.append(traceback.format_exc())
result=dict(status='FAIL' if errors else 'PASS',errors=errors,case_counts={arm:{s:list(c.values()).count(s) for s in ['PASS','SKIP','FAIL']} for arm,c in cases.items()},wire_sizes=shapes,medians=stats)
(root/'case-records.json').write_text(json.dumps(cases,indent=2)+'\n')
(root/'pair-verdict.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
sys.exit(bool(errors))
