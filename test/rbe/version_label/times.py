"""Read only the final events of the two original, authenticated invocations."""
import json
import os
from pathlib import Path
from http.client import HTTPSConnection


def main():
    key = os.environ.pop('BUILDBUDDY_API_KEY')
    result = {}
    for kind, invocation in (('last_arm_package','070dffb1-f1c3-4fb4-b790-830038c2c0b9'),
                             ('restore','deb53d6d-e9d1-4b1a-93e4-f519128fa9af')):
        conn=HTTPSConnection('app.buildbuddy.io', timeout=45)
        try:
            conn.request('POST','/rpc/BuildBuddyService/GetInvocation',
                         json.dumps({'lookup':{'invocationId':invocation}}),
                         {'Content-Type':'application/json','x-buildbuddy-api-key':key})
            with conn.getresponse() as res:
                assert res.status==200, (kind,res.status)
                data=res.read((64<<20)+1)
            assert len(data)<=64<<20
            [row]=json.loads(data)['invocation']
            assert row['invocationId']==invocation and row['success']
            [finish]=[e['buildEvent']['finished'] for e in row['event'] if 'finished' in e.get('buildEvent',{})]
            assert finish['exitCode']['name']=='SUCCESS'
            result[kind]={'id':invocation,'finish':finish.get('finishTime'),'finish_millis':finish.get('finishTimeMillis')}
            assert result[kind]['finish'] or result[kind]['finish_millis']
        finally:
            conn.close()
    dest=Path(os.environ['RUNNER_TEMP'])/'original-times.json'
    dest.write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    main()
