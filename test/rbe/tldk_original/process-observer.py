"""Observe only processes executing the original plugin Sentry on this runner."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

root = Path(os.environ["RUNNER_TEMP"])
receipt = root / "receipt" / "process-observer.json"
exe = (root / "reused/gvisor-bin/gvisor_sentry_plugin_stack").stat()
stop = root / "process-observer.stop"
rows = {}
earliest = int(time.time())
def dump():
    receipt.write_text(json.dumps(result, indent=2) + "\n")

def topology(base):
    return sorted(int(p.parts[-3][3:]) for p in (base / "sys/devices/system/cpu").glob("cpu[0-9]*/topology/core_id") if p.exists())
result = {"hostAffinity": sorted(os.sched_getaffinity(0)), "hostTopology": topology(Path("/")), "processes": rows}
dump()
# Record exit codes while an exiting thread still has an mm; Linux's scheduler
# exit tracepoint runs later, after its last reference to the mm was dropped.
# A task is selected on exec by the actual staged ELF inode. No command lines,
# stacks, argument addresses or syscall arguments from general tasks are read.
bt = r'''
BEGIN { printf("READY\n"); }
tracepoint:sched:sched_process_exec {
  if (((struct task_struct *)curtask)->mm->exe_file->f_inode->i_ino == INODE) {
    @ours[pid]=1; printf("EXEC %d\n", pid);
  }
}
tracepoint:raw_syscalls:sys_enter /@ours[pid]/ { @last[tid]=args->id; }
tracepoint:raw_syscalls:sys_exit /@ours[pid] && (args->ret == -1 || args->ret == -13 || args->ret == -38)/ {
  @denied[pid,args->id,args->ret]=count();
}
tracepoint:syscalls:sys_enter_access /@ours[pid]/{
  if (str(args->filename) == "/sys/devices/system/cpu/cpu0/topology/core_id") { @cpu0[tid]=1; }
}
tracepoint:syscalls:sys_exit_access /@cpu0[tid]/{
  printf("CPU0_ACCESS %d %d\n",pid,args->ret); delete(@cpu0[tid]);
}
tracepoint:signal:signal_deliver /@ours[pid] && (args->sig==4 || args->sig==6 || args->sig==7 || args->sig==9 || args->sig==11 || args->sig==31)/{
  printf("SIGNAL %d %d %d %d %d\n",pid,tid,args->sig,args->code,@last[tid]);
}
kprobe:do_group_exit /@ours[pid]/ { printf("GROUP_EXIT %d %d %d %d\n",pid,tid,arg0,@last[tid]); }
kprobe:do_exit /@ours[pid] && (pid==tid || (arg0 & 127)!=0)/{
  printf("EXIT %d %d %d %d\n",pid,tid,arg0,@last[tid]);
}
END { print(@denied); clear(@denied); clear(@ours); clear(@last); clear(@cpu0); }
'''.replace("INODE",str(exe.st_ino))
if any(line.rstrip().endswith(" force_sig_seccomp") for line in Path("/proc/kallsyms").open()):
    bt += 'kprobe:force_sig_seccomp /@ours[pid]/ { printf("SECCOMP %d %d %d %d %d\\n",pid,tid,arg0,arg1,arg2); }'
trace = None
bp = root/"tldk-specific.bt"
bp.write_text(bt)
bf = (root/"private-tldk-observer.log").open("w")
if shutil.which("bpftrace"):
    trace = subprocess.Popen(["bpftrace","-B","line","-q",str(bp)], stdout=bf, stderr=subprocess.STDOUT)
# The /proc observation still runs if the kernel does not supply tracepoints.
# The same probe is repeated only while one actual named guest test is active.
deadline = time.monotonic()+320
while time.monotonic()<deadline and not stop.exists():
    if trace is None or trace.poll() is not None or "READY" in (root/"private-tldk-observer.log").read_text():
        (root/"process-observer.ready").touch()
    for path in Path("/proc").iterdir():
        if not path.name.isdigit(): continue
        try:
            stat = (path/"exe").stat()
            if (stat.st_dev,stat.st_ino)!=(exe.st_dev,exe.st_ino): continue
            pid = path.name
            entry = rows.setdefault(pid,[])
            d = (path/"root").stat()
            identity=[d.st_dev,d.st_ino]
            allowed=re.search(r"^Cpus_allowed_list:\s*([0-9,-]+)",(path/"status").read_text(),re.M)
            obs={"root":identity,"allowed":allowed.group(1) if allowed else None, "sysExists":(path/"root/sys").exists(),"cpuIDs":topology(path/"root")}
            if not entry or entry[-1]!=obs:
                entry.append(obs);dump()
        except (OSError, ValueError): continue
    time.sleep(.016)
if trace is not None:
    if trace.poll() is None:
        trace.send_signal(signal.SIGINT)
        try: trace.wait(timeout=5)
        except subprocess.TimeoutExpired: trace.kill();trace.wait()
    result["tracerExit"]=trace.returncode
bf.close()
lines=(root/"private-tldk-observer.log").read_text(errors="replace").splitlines()
result["numericEvents"]=[s for s in lines if re.fullmatch(r"(?:EXEC|EXIT|GROUP_EXIT|SIGNAL|CPU0_ACCESS|SECCOMP)(?: -?\d+){1,5}|@denied\[(?:\d+, ?){2}-?\d+\]: \d+",s)][:500]
result["tracerReady"]="READY" in lines
result["tracerErrors"]=[s[:230] for s in lines if "ERROR:" in s][:4]
pids=set(rows)|{x.split()[1] for x in result["numericEvents"] if x.startswith("EXEC ")}
kernel=subprocess.run(["journalctl","-k","--since","@"+str(earliest),"--no-pager","-o","cat"],capture_output=True,text=True)
result["kernelEvents"]=[]
for line in kernel.stdout.splitlines():
    if not re.search(r"(?i)seccomp|type=1326|segfault|out of memory|killed process|general protection|trap ",line):continue
    fields=dict(re.findall(r"\b(pid|sig|syscall|code|arch|ip|error)=(0x[0-9a-f]+|[0-9a-f]+)",line))
    if fields.get("pid") in pids or any(re.search(r"(?:\[|[Pp]rocess )"+s+r"(?:\]|\b)",line) for s in pids):
        fields.pop("ip",None)
        fields["type"]=next((s for s in ("1326","seccomp","segfault","[Oo]ut of memory","[Kk]illed process","general protection","trap ") if re.search(s,line)),"other")
        result["kernelEvents"].append(fields)
dump()
