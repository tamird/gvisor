"""Reconcile the two original fork bundles without re-running a Bazel action."""
from pathlib import Path
import datetime
import json
import sys

directory = Path(sys.argv[1])
kinds = [
    ('first-results', 'e734aa067e340dd5840c4b4492918096052f12fd',
     {'pgalloc/TestAsyncLoadCompletionBatch','pgalloc/TestAsyncLoadCompletionBatch/truncated'}, (1,2), {
         'page read completions: 4096, 4096', 'loader finished with <nil>', 'page read completions: 4096, 0', 'loader finished with I/O error',
         'MapInternal stayed blocked after the page read and loader both finished'}),
    ('refs-results', '896bb88aa8bb7e2cd0bf676376c3f3d4bc5cbb59',
     {'pgalloc/TestAsyncLoadReleasesBackgroundReads', 'pgalloc/TestAsyncLoadReleasesBackgroundReads/shortRead','pgalloc/TestAsyncLoadReleasesBackgroundReads/failedWait'},(0,3),{
         'mode=shortRead page=0 unique=false','mode=shortRead page=1 unique=true',
         'mode=failedWait page=0 unique=false','mode=failedWait page=1 unique=false'}),
]
summary = {}
for name, original, cases, counts, fingerprints in kinds:
    folder = directory / name
    bundle = json.loads((folder / 'summary.json').read_text())
    assert not bundle['missing'] and set(bundle['executed']) == set(bundle['expected'])
    restored = []
    for arm in bundle['executed']:
        rec = json.loads((folder / (arm + '.json')).read_text())
        assert rec['arm'] == arm and not rec['errors'] and not rec['missing']
        assert not rec['capture_exit'] and not rec['case_counts']['skipped']
        if arm == 'stock':
            assert rec['source'] == original and rec['status'] == 'FAIL'
            assert rec['native_exit'] == rec['bep_finish']['exitCode']['code'] == 3
            assert {n for e in rec['failures'] for n in e.get('cases', [])} == cases
            assert (rec['case_counts']['passed'],rec['case_counts']['failed']) == counts
            assert set(fingerprints) == {obs.split(': ', 1)[-1] if name == 'first-results' else obs for obs in rec['observed']}
            assert rec['verification_errors'] == (['did not reproduce only the intended blocked waiter with the intact control passing'] if name=='first-results' else ['expected real retained reference failure'])
        else:
            assert rec['verified'] and rec['status'] == 'PASS' and rec['native_exit'] == 0 and rec['case_counts']['passed'] > 0
        restored.append({'arm':arm, 'invocation':rec['invocation'],'source':rec['source'],'status':rec['status'],'counts':rec['case_counts'],'finished':rec['execution_finished_at']})
    summary[name] = restored
print(json.dumps({'status':'PASS','runs':summary,'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat()},indent=2))
