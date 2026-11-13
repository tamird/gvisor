"""Measure MySQL's log assertion and TCP readiness on one fresh systrap server."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import xml.etree.ElementTree as ET

import previous

OUT = Path(os.environ['RUNNER_TEMP']) / 'mysql-54914'
TOOLS = Path(os.environ['RUNNER_TEMP']) / 'podman-tools'
ARCHIVE = 'https://buildkite.com/organizations/gvisor/pipelines/pipeline/builds/54914/jobs/01a1d5a5-b785-46ed-94e9-c1f8a7ed998d/artifacts/01a1d5b3-99cf-40f0-80a7-5358f39f9181'
DIGEST = 'adfdb0dffb78e460e4b2d3bd345f1d863e33f1b3aba0a81ab5f6f0e097150b41'
LABEL = '//test/image:image_test'
IMG = 'gvisor.dev/images/basic/mysql'
RUNTIME = 'runsc-mysql-systrap'

def text(*cmd: str) -> str:
    return subprocess.check_output(cmd, text=True).strip()

def install_runtime() -> None:
    path = TOOLS / 'ci.tar.gz'
    with urllib.request.urlopen(ARCHIVE, timeout=120) as src, path.open('wb') as dst:
        shutil.copyfileobj(src, dst)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == DIGEST
    evidence = {}
    members = ['runsc', 'gvisor-bin/gvisor_sentry', 'gvisor-bin/runsc-netgofer', 'gvisor-bin/checkpointgofer', 'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/gvisor-proc-umounter', 'gvisor-bin/runsc-fd-parking', 'gvisor-bin/runsc-metric-server']
    with tarfile.open(path) as archive:
        for member in members:
            entry = archive.getmember('buildkite_runtime/' + member)
            assert entry.isfile(), entry.name
            data = archive.extractfile(entry).read()
            assert data.startswith(b'\x7fELF'), entry.name
            file = TOOLS / 'release' / member
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            file.chmod(0o755)
            evidence[member] = hashlib.sha256(data).hexdigest()
    runtime = str(TOOLS / 'release/runsc')
    subprocess.run(['sudo', runtime,'install','--runtime='+RUNTIME,'--','--platform=systrap'],check=True)
    subprocess.run(['sudo','systemctl','restart','docker'],check=True)
    for image in ['gcr.io/gvisor-presubmit/basic/mysql_x86_64:latest', 'mysql/mysql-server:8.0.19']:
        if subprocess.run(['docker','pull',image]).returncode == 0:
            break
    else:
        raise RuntimeError('Neither the canonical CI image nor its single upstream FROM image was accessible')
    subprocess.run(['docker','tag',image,IMG],check=True)
    config = json.loads(text('docker','image','inspect',IMG))[0]
    assert '/healthcheck.sh' in ' '.join(config['Config']['Healthcheck']['Test']), config['Config']['Healthcheck']
    server_version = text('docker','run','--rm','--runtime='+RUNTIME,'--entrypoint','mysqld',IMG,'--version')
    assert '8.0.19' in server_version, server_version
    facts = {'kernel':text('uname','-r'), 'docker':text('docker','version','--format','{{.Server.Version}}'),
             'cgroup':text('docker','info','--format','{{.CgroupVersion}}'), 'runtime':RUNTIME,
             'release_archive_sha256':DIGEST,'elf_sha256':evidence,'image':image,
             'id':config['Id'],'digests':config['RepoDigests'],'mysql':server_version}
    (OUT/'inputs.json').write_text(json.dumps(facts,indent=2))

def instrument() -> None:
    file = Path('test/image/image_test.go')
    source = file.read_text().replace('"os"','"os"\n\t"os/exec"',1)
    start = source.index('func TestMysql(')
    stop = source.index('func TestTomcat(',start)
    fn=source[start:stop]
    old='\t// Wait until it\'s up and running.'
    probe='''    address, err := server.FindIP(ctx, false)
    if err != nil { t.Fatal(err) }
    // Keep the test's existing wait, deadline and SQL workload in this container.
    // Observe authenticated TCP from a separate already-running systrap client.
    observed := make(chan bool, 1)
    deadline := time.Now().Add(100*time.Second)
    go func(){
        var attempts int
        for time.Now().Before(deadline) {
            attempts++
            output, err := exec.Command("docker","exec","mysql-tcp-observer",
                "mysql","--protocol=tcp","--connect-timeout=1","-uroot","-h"+address.String(),
                "--batch","--skip-column-names","-e","SELECT 1").CombinedOutput()
            if err == nil && strings.TrimSpace(string(output)) == "1" {
                t.Logf("MYSQL_TIMING tcp_select=%s attempts=%d",time.Now().UTC().Format(time.RFC3339Nano),attempts)
                observed <- true
                return
            }
            time.Sleep(500*time.Millisecond)
        }
        observed <- false
    }()
    defer func() {
        state, err := exec.Command("docker","inspect","--format","{{.State.StartedAt}} {{json .State.Health}}",server.ID()).Output()
        t.Logf("MYSQL_TIMING start_and_health=%s err=%v", state, err)
        logs, err := exec.Command("docker","logs","--timestamps",server.ID()).CombinedOutput()
        t.Logf("MYSQL_TIMING logs_err=%v",err)
        for _, line := range strings.Split(string(logs),"\\n") {
            if strings.Contains(line,"Entrypoint") || strings.Contains(line,"ready for connections") ||
                strings.Contains(line,"Shutdown") || strings.Contains(line,"zoneinfo") {
                t.Logf("MYSQL_PHASE %s", line)
            }
        }
    }()
'''
    assert fn.count(old)==1 and fn.count('t.Fatalf("WaitForOutput() timeout: %v", err)')==2
    fn=fn.replace(old,probe+old).replace('t.Fatalf("WaitForOutput() timeout: %v", err)',
             't.Logf("MYSQL_TIMING log_timeout=%s",time.Now().UTC().Format(time.RFC3339Nano))\n\t\tt.Logf("MYSQL_TIMING later_tcp=%t",<-observed)\n\t\tt.Fatalf("WaitForOutput() timeout: %v", err)',1).replace('// Generate the client', 't.Logf("MYSQL_TIMING log_ready=%s",time.Now().UTC().Format(time.RFC3339Nano))\n\tif !<-observed { t.Error("independent TCP observer did not complete SELECT 1") }\n\t// Generate the client')
    file.write_text(source[:start]+fn+source[stop:])
    (OUT/'observer.diff').write_text(text('git','diff','--',str(file))+'\n')

def main() -> None:
    OUT.mkdir(exist_ok=True)
    previous.OUT = OUT
    install_runtime()
    instrument()
    bazel = [str(TOOLS/'bazel'),'--nosystem_rc','--nohome_rc']
    opts=['--config=x86_64','-c','opt','--jobs=4','--lockfile_mode=off','--remote_executor=','--remote_cache=','--bes_backend=','--spawn_strategy=processwrapper-sandbox,local','--strategy=GoStdlib=processwrapper-sandbox','--strategy=GoStdlibList=processwrapper-sandbox']
    for attempt in range(24):
        dist=['--distdir='+str(x) for x in sorted(previous.DIRS)]
        r=subprocess.run([*bazel,'build',*opts,*dist,LABEL],capture_output=True,text=True)
        print(r.stderr[-1800:],flush=True)
        if r.returncode==0: break
        if not previous.recover(r.stderr):
            raise RuntimeError(r.stderr[-2500:])
    else: raise RuntimeError('Unresolved declared Bazel dependencies')
    subprocess.run(['docker','run','-d','--name','mysql-tcp-observer','--runtime='+RUNTIME,'-e','MYSQL_PWD=foobar123','--entrypoint','/bin/sleep',IMG,'240'],check=True)
    try:
        result=subprocess.run([*bazel,'test',*opts,*dist,'--nocache_test_results','--test_env=PATH=/usr/local/bin:/usr/bin:/bin','--test_env=DOCKER_CLI_PATH=/usr/bin/docker','--test_arg=--runtime='+RUNTIME,'--test_filter=^TestMysql$','--test_arg=-test.v','--test_output=all',LABEL],text=True,capture_output=True,timeout=300)
        (OUT/'bazel.log').write_text(result.stdout+result.stderr)
        record=Path('bazel-testlogs/test/image/image_test')
        for name in ['test.xml','test.log']:
            if (record/name).exists(): shutil.copyfile(record/name, OUT/name)
        log=(OUT/'test.log').read_text()
        assert '=== RUN   TestMysql' in log and 'MYSQL_TIMING' in log, log[-1800:]
        assert len(ET.parse(OUT/'test.xml').findall('.//testcase'))>=1
        print('\n'.join(s for s in log.splitlines() if 'MYSQL_' in s),flush=True)
        if result.returncode: raise SystemExit(result.returncode)
        assert '--- PASS: TestMysql' in log and 'MYSQL_TIMING tcp_select=' in log and 'MYSQL_TIMING log_ready=' in log
    finally: subprocess.run(['docker','rm','-f','mysql-tcp-observer'])

if __name__ == '__main__':
    main()
