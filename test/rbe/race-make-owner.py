"""Retain only scheduler and usage numbers from the two original ABI runs."""
import http.client
import json
import os
import pathlib

host = 'app.buildbuddy.io'
report = {}
for name, invocation in [('failed', '6bf8c9ef-9996-4cd8-9ddf-9244d5318563'), ('previousOwner', '7636224d-ea53-4778-8467-c87769d265ae')]:
    records = []
    try:
        for label in ['//pkg/abi/linux:linux', '@@//pkg/abi/linux:linux']:
            conn = http.client.HTTPSConnection(host, timeout=40)
            payload = {'executionLookup': {'invocationId': invocation, 'targetLabel': label}}
            conn.request('POST', '/rpc/BuildBuddyService/GetExecution', json.dumps(payload), {'Content-Type': 'application/json', 'x-buildbuddy-api-key': os.environ['BUILDBUDDY_API_KEY']})
            response = conn.getresponse()
            assert response.status == 200, 'metadata HTTP %d' % response.status
            data = response.read(16 * 1024 * 1024 + 1)
            assert len(data) <= 16 * 1024 * 1024, 'metadata exceeds bound'
            conn.close()
            for e in json.loads(data).get('execution', []):
                if e.get('targetLabel') != label or e.get('actionMnemonic') != 'GoStaticAnalysis': continue
                meta = e.get('executedActionMetadata', {})
                size = e.get('taskSize') or meta.get('estimatedTaskSize') or {}
                used = meta.get('usageStats', {})
                records.append({'action':e.get('actionDigest'), 'status':e.get('status',{}).get('code',0), 'exitCode':e.get('exitCode',0), 'workerStart':meta.get('executionStartTimestamp'), 'workerEnd':meta.get('executionCompletedTimestamp'), 'estimatedMemoryBytes':size.get('estimatedMemoryBytes'), 'estimatedMilliCpu':size.get('estimatedMilliCpu'), 'peakMemoryBytes':used.get('peakMemoryBytes'), 'cpuNanos':used.get('cpuNanos')})
            if records: break
        report[name] = {'invocation':invocation, 'records':records}
    except (OSError, ValueError, AssertionError, http.client.HTTPException) as err:
        report[name] = {'invocation':invocation, 'error':type(err).__name__, 'records':records}
report['previousOwnerStrategy'] = 'local Linux sandbox; only the later test is a remote execution'
pathlib.Path(os.environ['RUNNER_TEMP'], 'race-make/records/owner.json').write_text(json.dumps(report,indent=2)+'\n')
