import os
import ctypes
import json
from pathlib import Path
import platform
import re
import struct
import subprocess

assert platform.machine() == 'x86_64'
tmp = Path(os.environ['RUNNER_TEMP'])
out = tmp / 'results'
image, = re.findall(r'^\s*gsutilImage\s*= "([^"]+)"',Path('test/kubernetes/benchmarks/gsutil.go').read_text(),re.M)
subprocess.run(['docker','pull',image],check=True)
libc = ctypes.CDLL(None,use_errno=True)
inputs = json.loads(Path('test/rbe/gsutil-pod-commands.json').read_text())
records = {}
for mode, argv in sorted(inputs.items()):
    directory = tmp / ('trackers-'+mode)
    directory.mkdir()
    os.chmod(directory,0o777)
    wd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    assert wd != -1, ctypes.get_errno()
    assert libc.inotify_add_watch(wd,os.fsencode(directory),0x00000100 | 0x00000080 | 0x00000008) != -1, ctypes.get_errno()
    cfg = tmp / (mode+'.boto')
    cfg.write_text('[GSUtil]\nresumable_tracker_dir=/trackers\nsliced_object_download_threshold=1M\nsliced_object_download_component_size=16M\n')
    cmd = ['docker','run','--runtime=gsutil-systrap','--rm','--entrypoint='+argv[0],'-v',str(cfg)+':/probe.boto:ro','-v',str(directory)+':/trackers','-e','BOTO_CONFIG=/probe.boto',image,argv[1]]
    script=argv[2].replace('gs://gvisor-benchmark-testdata/bigrandomfile','gs://gvisor-benchmark-testdata/littlerandomfile')
    script += " && stat -c 'probe-bytes=%s' /downloads/randombigfile && gsutil hash -c /downloads/randombigfile"
    p = subprocess.run(cmd+[script],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=400)
    log=p.stdout.decode(errors='replace')
    names=set()
    while True:
        try: buf=os.read(wd,65536)
        except BlockingIOError: break
        pos=0
        while pos<len(buf):
            _,mask,_,n=struct.unpack_from('iIII',buf,pos); pos+=16
            assert not mask & 0x4000, 'inotify queue overflow'
            names.add(buf[pos:pos+n].split(b'\x00',1)[0].decode()); pos+=n
    os.close(wd)
    ids=sorted({int(m.group(1)) for name in names if (m:=re.search(r'__(\d+)\.etag$',name))})
    records[mode]=dict(rc=p.returncode,files=sorted(names),component_ids=ids,bytes=52428800 if 'probe-bytes=52428800' in log else 0,crc='1BRP5g==' if '1BRP5g==' in log else '',success=bool(re.search(r'^::::TIMING::::\d+::::\d+\s*$',log,re.M)) and '::::TIMING::::FAILURE' not in log)
    print(mode,records[mode],flush=True)
    (out/'component-trackers.json').write_text(json.dumps(records,indent=2)+'\n')
assert all(r['rc']==0 and r['success'] and r['crc'] and r['bytes'] for r in records.values())
assert not records['single']['component_ids'] and len(records['sliced']['component_ids'])>=2, records
