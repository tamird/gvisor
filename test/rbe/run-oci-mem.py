import json,os,re,subprocess,sys,tempfile
from pathlib import Path
binary=Path(sys.argv[1]);records=Path(sys.argv[2])
cgfs=Path('/sys/fs/cgroup')
cgname='gvisor-mem-audit-'+str(os.getpid())
grand=cgfs/cgname;grand.mkdir()
try:
    (grand/'memory.max').write_text(str(512<<20))
    (grand/'cgroup.subtree_control').write_text('+memory')
    middle=grand/'pod'; middle.mkdir(); (middle/'cgroup.subtree_control').write_text('+memory')
    for name,limit in [('unlimited',None),('larger',768<<20),('smaller',384<<20)]:
        leaf=middle/name;leaf.mkdir()
        if limit: (leaf/'memory.max').write_text(str(limit))
        with tempfile.TemporaryDirectory() as temp:
            td=Path(temp);bundle=td/'bundle';root=td/'state';bundle.mkdir();root.mkdir()
            cid='mem-'+name
            spec={'ociVersion':'1.0.2','process':{'cwd':'/','args':['/bin/cat','/proc/meminfo'],'user':{'uid':0,'gid':0}},'root':{'path':'/','readonly':True},'hostname':'memtest','linux':{'cgroupsPath':'/'+cgname+'/pod/'+name,'resources':{'memory':({} if limit is None else {'limit':limit})}},'mounts':[]}
            (bundle/'config.json').write_text(json.dumps(spec))
            args=[str(binary),'--root='+str(root),'--network=none','--platform=systrap','--overlay2=none']
            p=subprocess.run(args+['run','--bundle='+str(bundle),cid],capture_output=True,timeout=45)
            (records/(name+'.stderr')).write_bytes(p.stderr)
            (records/(name+'.stdout')).write_bytes(p.stdout)
            m=re.search(rb'^MemTotal:\s*(\d+) kB$',p.stdout,re.M)
            levels=[(g/'memory.max').read_text().strip() for g in [grand,middle,leaf]]
            (records/(name+'.json')).write_text(json.dumps({'status':p.returncode,'MemTotal':int(m.group(1))*1024 if m else None,'limits':levels}))
            subprocess.run(args+['delete','--force',cid],capture_output=True,timeout=10)
            if (root/'null-netns').exists():
                subprocess.run(['umount','-l',str(root/'null-netns')],capture_output=True)
        if leaf.exists():
            leaf.rmdir()
    middle.rmdir()
finally:
    for path in sorted(grand.rglob('*'), key=lambda p:-len(p.parts)):
        if path.is_dir():
            try:path.rmdir()
            except OSError:pass
    grand.rmdir()
