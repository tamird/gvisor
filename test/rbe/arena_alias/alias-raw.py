"""Record only deliberately supplied tunables after glibc has parsed them."""
import json,os,pathlib,subprocess,time
d=pathlib.Path(os.environ['RUNNER_TEMP'])/'receipt';d.mkdir(exist_ok=True)
prefix='GLIBC_TUNABLES=glibc.pthread.rseq=0'
r={'glibc':os.confstr('CS_GNU_LIBC_VERSION'),'modes':{}}
for mode,env in [('combined',{'GLIBC_TUNABLES':'glibc.pthread.rseq=0:glibc.malloc.arena_max=32'}),('separate',{'GLIBC_TUNABLES':'glibc.pthread.rseq=0','MALLOC_ARENA_MAX':'32'})]:
 p=subprocess.Popen(['/usr/bin/sleep','30'],env=env)
 try:
  q=pathlib.Path('/proc')/str(p.pid)
  for _ in range(300):
   if 'nanosleep' in (q/'wchan').read_text():break
   time.sleep(.01)
  assert 'nanosleep' in (q/'wchan').read_text()
  bits=(q/'environ').read_bytes().decode().strip('\0').split('\0')
 finally:p.terminate();p.wait()
 r['modes'][mode]=bits
 (d/'glibc-original-bytes.json').write_text(json.dumps(r,indent=2)+'\n')
assert r['modes']=={'combined':[prefix,'glibc.malloc.arena_max=32'],'separate':[prefix,'MALLOC_ARENA_MAX=32']},r
r['status']='PASS';(d/'glibc-original-bytes.json').write_text(json.dumps(r,indent=2)+'\n')
