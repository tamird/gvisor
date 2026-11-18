"""Reject contradictory or incomplete native test action outcomes."""
import json
import sys
from pathlib import Path

path, output = map(Path, sys.argv[1:3])
expected = set(sys.argv[3:])
results = []
summaries = {}
finished = []
for line in path.read_text().splitlines():
    event = json.loads(line)
    if 'testResult' in event:
        label = event['id']['testResult']['label']
        results.append({'label': label, 'status': event['testResult'].get('status')})
    if 'testSummary' in event:
        label = event['id']['testSummary']['label']
        assert label not in summaries, label
        summaries[label] = event['testSummary']
    if 'finished' in event:
        finished.append(event['finished'])
assert set(summaries) == expected
assert len(results) == len(expected) and {r['label'] for r in results} == expected
assert all(r['status'] == 'PASSED' for r in results), results
assert all(s.get('overallStatus') == 'PASSED' and s.get('totalRunCount') == 1 for s in summaries.values())
assert len(finished) == 1 and finished[0].get('overallSuccess') is True
output.write_text(json.dumps({'testResults': results, 'summaryTargets': sorted(summaries), 'passed': True}, indent=2) + '\n')
