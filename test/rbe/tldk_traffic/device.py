import json,os,stat,subprocess,sys
from pathlib import Path
d=Path(os.environ['RUNNER_TEMP'])/'traffic';d.mkdir(exist_ok=True)
r={'source':os.environ['PRODUCT'],'kernel':os.uname().release,'devices':{},'status':'INCOMPLETE','traffic_executed':False}
for path,mod,dev in [('/dev/vhost-net','vhost_net','vhost-net'),('/dev/net/tun','tun','tun')]:
 p=Path(path); commands=[]
 try:
  if not p.exists():
   s=subprocess.run(['modprobe',mod],capture_output=True,text=True);commands.append({'modprobe':s.returncode,'stderr':s.stderr[-400:]})
   if not p.exists() and not Path('/sys/class/misc/'+dev+'/dev').exists() and s.returncode:
    pkg='linux-modules-extra-'+os.uname().release
    if subprocess.run(['apt-cache','show',pkg],capture_output=True).returncode==0:
     s=subprocess.run(['apt-get','install','-y','--no-install-recommends',pkg],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True);commands.append({'install':pkg,'exit':s.returncode});subprocess.run(['modprobe',mod],check=False)
   if not p.exists():
    major,minor=map(int,Path('/sys/class/misc/'+dev+'/dev').read_text().split(':'));p.parent.mkdir(exist_ok=True);os.mknod(path,stat.S_IFCHR|0o660,os.makedev(major,minor))
  fd=os.open(path,os.O_RDWR|os.O_CLOEXEC);s=os.fstat(fd);os.close(fd);assert stat.S_ISCHR(s.st_mode)
  r['devices'][path]={'O_RDWR':'opened','dev':[os.major(s.st_rdev),os.minor(s.st_rdev)],'setup':commands}
 except Exception as ex:r['devices'][path]={'error':repr(ex),'setup':commands}
ok=all(row.get('O_RDWR')=='opened' for row in r['devices'].values())
r['status']='PASS' if ok else 'HOST_DEVICE_BLOCKED'
(d/'device.json').write_text(json.dumps(r,indent=2)+'\n');(d/'device.json').chmod(0o644)
print(json.dumps(r));sys.exit(0 if ok else 1)
