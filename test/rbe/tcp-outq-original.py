"""Reconcile the original Linux, unchanged TCP, and corrected guest assertions."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

actions_source = '41256062589'
def check(results, src):
    negative = results['duplex-stock']
    good = [results[k] for k in ('duplex-native', 'duplex-fixed')]
    fixtures = set()
    names = {f'AllTCPSockets/TCPSocketPairTest.OutputQueueThroughFinAndReset/{i}' for i in range(12)}
    names.add('TcpOutputQueueTest.SocketThatNeverEstablished')
    for key, run in [('fixture',good[0]),('fixture',negative),('candidate',good[1])]:
        assert run['source'] == src[key] and set(run['executed'])==set(run['expected'])==set(run['outcomes'])==names and not run['missing']
        fixtures.add(run['fixture'])
    assert len(fixtures)==1
    assert negative['exit']==3
    for case, outcome in negative['outcomes'].items():
        details = outcome['details']
        assert outcome['status']=='FAIL' and ('ioctl(writer, 0x5411, &queued)' in details[0] or 'ioctl(client.get(), 0x5411, &queued)' in details[0]) and 'errno=92 Protocol not available' in details[0]
        if case.startswith('AllTCPSockets/'):
            assert len(details)==2 and 'await_queue(0) doesn\'t generate new fatal failures' in details[1]
        else:
            assert len(details)==1
    for run in good:
        assert run['exit']==0 and all(x['status']=='OK' for x in run['outcomes'].values())
        assert run['refused']==[['1','1']]
        obs=run['observations']
        assert len(obs)==36 and all(sum(r[0]==stage for r in obs)==12 for stage in range(3))
        for path, sent, unread, fin, final in obs:
            assert unread > 0 and sent > unread and fin==sent-unread+1
            assert final==(fin if path==2 else 0)
    return {'verified':True,'original_run':actions_source,'source':src,'fixture_sha256':fixtures.pop(),'cases_each':len(names),'missing':[],'skipped':[],'linux':13,'stock_ioctl_ENOPROTOOPT':13,'corrected':13,'linux_and_corrected_refused_syn_outq':1,'completed_at':max(r['completed'] for r in [negative,*good]),'verified_at':datetime.now(timezone.utc).isoformat()}

if __name__=='__main__':
    import copy
    old=json.loads(Path(sys.argv[1]).read_text())
    src=json.loads(Path(sys.argv[2]).read_text())
    # Challenge the exact error, missing cases, and equality to Linux.
    for change in ('error','missing','different'):
        sample=copy.deepcopy(old); target=sample['duplex-stock']['outcomes']; one=next(iter(target))
        if change=='missing': del target[one]
        elif change=='error': target[one]['details'][0]=target[one]['details'][0].replace('errno=92','errno=22')
        else: sample['duplex-fixed']['refused']=[['0','0']]
        try: check(sample,src)
        except AssertionError: pass
        else: raise ValueError(f'recovery accepted {change}')
    Path(sys.argv[3]).write_text(json.dumps(check(old,src),indent=2)+'\n')
