"""Reconcile two architecture filters from the original passing terminal-error invocation."""
import os,json,hashlib,re,urllib.parse,subprocess,io,zipfile,sys
from pathlib import Path
from datetime import datetime,timezone
from cas_inputs import blob, fetch
LABEL='//pkg/tcpip/transport/tcp:tcp_nogo'
nows=lambda: datetime.now(timezone.utc).isoformat()
t=Path(os.environ['RUNNER_TEMP'])
j={'status':'INCOMPLETE','original_run':41565161188,'artifact':13198748523,'logs':{}}
z=(t/'original.zip').read_bytes();assert hashlib.sha256(z).hexdigest()=='0e2294c436c6276326d462115099e8d4cc27351ab47de9274bab5457d4722798'
v=json.loads(zipfile.ZipFile(io.BytesIO(z)).read('verdict.json'))
j['source']='a795edf944519fde538e137b44875d852fe66e96';j['original_verdict_at']=v['verdict_at'];j['original_rows']=v['rows']
try:
 for k in ('native','stock','fixed','amd64-nogo'):assert v['rows'][k]['verified']
 assert not v['verified'] and not v['rows']['arm64-nogo']['verified']
 for rowname,uid,passed in [('amd64-nogo','6ce1bf3f-3a07-4691-9b4e-5b4b686bf233',True),('arm64-nogo','5a71b052-7640-483f-9003-20d346ba2f03',False)]:
  row=v['rows'][rowname];assert row['invocation']==uid and row['source']==j['source'] and row['attempts']=={LABEL:[{'runner':'remote','cached':False,'status':'PASSED' if passed else 'FAILED'}]}
  out=j['logs'][rowname]={'invocation':uid}
  reply=json.loads(fetch('api/v1/GetAction',{'selector':{'invocation_id':uid,'target_label':LABEL}}));assert not reply.get('next_page_token',reply.get('nextPageToken'))
  (act,file),=[(a,f) for a in reply['action'] for f in a.get('file',[]) if f['name']=='test.log']
  assert act.get('target_label',act.get('targetLabel'))==LABEL and act['id'].get('invocation_id',act['id'].get('invocationId'))==uid and all(int(act.get(k,0))==1 for k in ('run','shard','attempt'))
  m=re.fullmatch(r'bytestream://remote\.buildbuddy\.io/(?:[^?#]*/)?blobs/([0-9a-f]{64})/(\d+)',file['uri']);assert m;d,size=m.groups();assert 0<int(size)<1048576
  raw=fetch('file/download?'+urllib.parse.urlencode({'bytestream_url':file['uri'],'invocation_id':uid}));assert len(raw)==int(size) and hashlib.sha256(raw).hexdigest()==d
  lines=raw.decode().splitlines();out.update(digest=d,size=int(size),action=act['id'])
  out['architecture_filters']=[line.strip() for line in lines if re.fullmatch(r'(?:amd64|arm64)\.\.\.PASS',line.strip())]
  out['format_errors']=[re.search(r'(?:cannot execute binary file[^\n:]*: ?)?Exec format error',x,re.I).group() for x in lines if 'exec format error' in x.lower()]
  if passed:assert sorted(out['architecture_filters'])==['amd64...PASS','arm64...PASS']
  else:assert out['format_errors'] and row['exit']==3 and any('Exit 126' in x for x in row['build_errors'])
 j['status']='VERIFIED'
except Exception as e:j['error']=type(e).__name__
finally:
 j['verdict_at']=nows();data=(json.dumps(j,indent=2)+'\n').encode();assert os.environ['BUILDBUDDY_API_KEY'].encode() not in data and b'--remote_header' not in data;(t/'checked-refusal.json').write_bytes(data);print(j['status'],j['logs'])
sys.exit(j['status']!='VERIFIED')
