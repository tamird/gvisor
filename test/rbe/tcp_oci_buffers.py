from pathlib import Path
import json,re,sys,xml.etree.ElementTree as ET
p=Path(sys.argv[1]);source=sys.argv[2];name='TestTCPBufferOCIAndGuestWrite'
v={'source':source,'target':'//runsc/container:container_test','expected':[name,name+'/OCI',name+'/GUEST_WRITE'],'executed':{},'observations':{},'verdict':'INCOMPLETE','errors':[]}
try:
 assert (p/'source').read_text().strip()==source
 v['bazel_exit']=int((p/'exit').read_text())
 root=ET.parse(p/'test.xml'); log=(p/'test.log').read_text()
 for t in root.iter('testcase'):
  n=t.get('name'); status=('skip' if t.find('skipped') is not None or t.get('status')=='notrun' else 'fail' if t.find('failure') is not None or t.find('error') is not None else 'pass')
  assert n not in v['executed']
  v['executed'][n]=status
 for phase,a,b,r,w in re.findall(r'BUFFER_OPTION (OCI|GUEST_WRITE)\|(\d+ \d+ \d+)\|(\d+ \d+ \d+)\|(\d+)\|(\d+)',log):
  v['observations'][phase]=[a,b,int(r),int(w)]
 assert set(v['expected'])==set(v['executed']) and set(v['observations'])=={'OCI','GUEST_WRITE'}
 want=['8192 524288 268435456']*2+[524288]*2
 assert v['observations']['GUEST_WRITE']==want and v['executed'][name+'/GUEST_WRITE']=='pass'
 seen=v['observations']['OCI']
 if seen==want:
  assert all(x=='pass' for x in v['executed'].values()) and v['bazel_exit']==0
  v['verdict']='OCI_APPLIED'
 else:
  assert seen[0]!=want[0] and seen[1]!=want[1] and seen[2]!=want[2] and seen[3]!=want[3]
  assert v['executed'][name+'/OCI']=='fail' and v['executed'][name]=='fail' and v['bazel_exit']==3
  v['verdict']='OCI_IGNORED_GUEST_WRITE_APPLIED'
except Exception as e:
 v['errors'].append(repr(e))
v['missing']=sorted(set(v['expected'])-v['executed'].keys())
(p/'verdict.json').write_text(json.dumps(v,indent=2)+'\n')
print(json.dumps(v))
sys.exit(v['verdict']=='INCOMPLETE')
