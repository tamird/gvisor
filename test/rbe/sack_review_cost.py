import json, pathlib, re, statistics, sys, xml.etree.ElementTree as X
from datetime import datetime, timezone
out=pathlib.Path(sys.argv[1]); product, stock, candidate=sys.argv[2:]
def read(p): return (out/p).read_text() if (out/p).is_file() else ''
errors=[]; missing=[]; records={}; costs={}; counts={}; timings={}; measurements={}
for target,required in {'tcp_rack_test':{'TestSackedOutAcrossRecovery','TestSackedOutAfterPMTU','TestSackedOutDisjointBlocks','TestSackedOutPartialGSOTrim','TestSackedOutRetransmissionTiming'},'tcp_test':{'TestSACKBridgesRetransmittedQueueFragment','TestWireCreditSegmentSize'}}.items():
 log=read(target+'.log')
 run=set(re.findall(r'^=== RUN\s+(\S+)$',log,re.M))
 passed=set(re.findall(r'^\s*--- PASS: (\S+) \(',log,re.M))
 failed=re.findall(r'^\s*--- FAIL: (\S+)',log,re.M)
 skipped=re.findall(r'^\s*--- SKIP: (\S+)',log,re.M)
 parsed=[]
 if (out/(target+'.xml')).is_file():
  parsed=[dict(name=n.get('name'),status=n.get('status'),failure=n.find('failure') is not None or n.find('error') is not None,skip=n.find('skipped') is not None) for n in X.parse(out/(target+'.xml')).findall('.//testcase')]
 if not parsed: missing.append(target+' XML')
 if not run or not required<=passed or failed or not passed|set(skipped)>=run:errors.append(target+' failed or incomplete')
 records[target]={'total':len(run),'passed':len(passed),'failed':failed,'skips':skipped,'required':sorted(required),'missing':sorted(required-passed),'xml_cases':len(parsed),'xml_failed':[x['name'] for x in parsed if x['failure']]}
 if records[target]['xml_failed']:errors.append(target+' failed in XML')
 if target=='tcp_rack_test':
  for key,rx in [('hole','SECOND_HOLE .*'),('pmtu','MSS=\\d+ sacked=-?\\d+'),('disjoint','DISJOINT .*'),('gso','GSO aggregate=.*')]:
   measurements[key]=re.findall(rx,log)
  if not re.search(r'SECOND_HOLE immediate=true seq=packet10 count=3 reownd=0s',log):errors.append('missing real second-hole behavior')
for key in ['suite','analysis']:
 ec=read(key+'-exit').strip()
 if ec!='0':errors.append(key+' status='+ec)
for arm, sha in [('stock',stock),('candidate',candidate)]:
 sizes=[]; data={}; raw=[]
 for r in range(1,9):
  rec=[line.split('\t') for line in read('cost-exits.tsv').splitlines() if line.split('\t')[:2]==[arm,str(r)]]
  if rec!=[[arm,str(r),sha,'0']]:missing.append(f'{arm}:{r} successful exact source')
  log=read(f'cost-{arm}-{r}-test.log')
  sizes.extend(int(x) for x in re.findall(r'SACK_SEGMENT_BYTES=(\d+)',log))
  for name,n,b,a in re.findall(r'^BenchmarkRACKSparseCoverage/(\S+)\s+\d+\s+([\d.]+) ns/op\s+(\d+) B/op\s+(\d+) allocs/op',log,re.M):
   data.setdefault(name,[]).append([float(n),int(b),int(a)])
  raw.append(log)
 expected={a+'/'+b for a in ['ordinary-128','tail-4096','partial-GSO-4096'] for b in ['new','duplicate']}
 if set(data)!=expected or any(len(v)!=8 for v in data.values()):errors.append(arm+' missing benchmark pairs')
 if len(sizes)!=8 or len(set(sizes))!=1:errors.append(arm+' segment size missing')
 counts[arm]=sizes;costs[arm]=data;timings[arm]={k:statistics.median(x[0] for x in v) for k,v in data.items()}
v={'status':'fail' if errors or missing else 'pass','product':product,'stock_fixture':stock,'candidate_fixture':candidate,'executed':records,'analysis_owners':['tcp_nogo','tcp_test_nogo'],'suite_completed':read('suite-end').strip(),'verified_at':datetime.now(timezone.utc).isoformat(),'segment_bytes':counts,'wire':measurements,'paired_cost':costs,'median_ns':timings,'failures':errors,'missing':missing}
(out/'verdict.json').write_text(json.dumps(v,indent=2));print(json.dumps({k:v[k] for k in ['status','failures','missing','median_ns','segment_bytes','wire']}))
sys.exit(v['status']!='pass')
