"""Run the real website Make job without the old cross-compiler packages."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone

SOURCE = '171137fdb3cfe0d17b4da8a9b2aebbdfff053c64'
TARGET = '//website:site_image'
TMP = Path(os.environ['RUNNER_TEMP'])
OUT = TMP / 'website-toolchain.json'
result = {'source': SOURCE, 'carrier': os.environ['GITHUB_SHA'], 'status': 'INCOMPLETE'}


def record():
    result['updated_at'] = datetime.now(timezone.utc).isoformat()
    OUT.write_text(json.dumps(result, indent=2) + '\n')


def executable_machine(path):
    with path.open('rb') as f:
        header = f.read(20)
    assert header[:6] == b'\x7fELF\x02\x01', path
    return int.from_bytes(header[18:20], 'little')


def main():
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == SOURCE
    assert (platform.system(), platform.machine()) == ('Linux', 'x86_64')
    assert 'VERSION_ID="22.04"' in Path('/etc/os-release').read_text()
    cross_tools = ('gcc', 'g++', 'as', 'ld', 'objcopy', 'nm')
    result['installed_gnu_aarch64'] = [t for t in cross_tools if shutil.which('aarch64-linux-gnu-' + t)]
    assert not result['installed_gnu_aarch64'], result['installed_gnu_aarch64']
    template = Path('.buildkite/pipeline.yaml').read_text().split('label: ":earth_americas: Website tests"\n', 1)[1].split('  #', 1)[0]
    assert 'DOCKER_BUILD: "false"' in template and 'arch: "amd64"' in template and 'queue: "default"' in template
    command = shlex.split(re.search(r'^    command: (.+)$', template, re.M)[1])
    assert command == ['make', 'build', 'TARGETS=' + TARGET], command
    rc = TMP / 'website-toolchain.bazelrc'
    rc.write_text(f'build --build_event_json_file={TMP}/site-build.bep\n'
                  f'cquery --build_event_json_file={TMP}/site-query.bep\n'
                  'build --build_event_publish_all_actions --jobs=4 --remote_cache= --remote_executor= --disk_cache=\n')
    version = subprocess.check_output(['bazel', '--version'], text=True).strip()
    assert version == 'bazel ' + Path('.bazelversion').read_text().strip(), version
    result.update(phase='make', bazel=version, command=command)
    record()
    # Bazelisk and the Make environment come from the shared production hook.
    runner = os.environ | {'MAKEFLAGS': 'STARTUP_OPTIONS=--bazelrc=' + str(rc), 'DOCKER_HOST': 'unix:///no-website-docker-daemon'}
    with (TMP / 'site-make.log').open('wb') as logfile:
        status = subprocess.run(command, stdout=logfile, stderr=subprocess.STDOUT, env=runner).returncode
    result.update(make_exit=status, make_finished=datetime.now(timezone.utc).isoformat())
    record()
    if status:
        # There are no credentials in this job. Publish fixed failure categories
        # rather than arbitrary repository errors and redirect URLs.
        log = (TMP / 'site-make.log').read_text(errors='replace')
        result['failure_kinds'] = [s for s in ('No space left on device', '429', '403', '404', 'timed out',
                                   'checksum', 'no such package', 'No matching toolchains', 'Permission denied', 'certificate',
                                   'not found', 'undefined reference', 'module resolution', 'out of memory') if s.lower() in log.lower()]
        result['known_dependencies'] = [k for k in ('jekyll_runtime', 'jekyll_gem_', 'hermetic_llvm', 'llvm-toolchain', 'rules_img', 'LLVMProject', 'toolchains_llvm', 'tar.bzl') if any(k in line for line in log.splitlines() if line.startswith(('ERROR:', 'WARNING: Download')))]
        result['failed_owners'] = sorted(set(re.findall(r'^ERROR: [^\n]*? (//[A-Za-z0-9_/:.+-]+) failed', log, re.M)))[:12]
        result['status'] = 'FAIL'
        return status
    counts = {}
    vdsos = {}
    build_target = False
    for line in (TMP / 'site-build.bep').open():
        event = json.loads(line)
        key = event['id']
        if key.get('targetCompleted', {}).get('label') == TARGET and event.get('completed', {}).get('success'):
            build_target = True
        if 'actionCompleted' not in key:
            continue
        action = event.get('action', {})
        if not action.get('success'):
            continue
        kind = action.get('type')
        if kind in {'JekyllSDK', 'JekyllSite', 'WebsiteSyscallJSON', 'WebsiteSyscallDocs'}:
            counts[kind] = counts.get(kind, 0) + 1
        primary = Path(key['actionCompleted']['primaryOutput'])
        if primary.as_posix().endswith('/vdso/vdso.so'):
            vdsos[str(primary)] = executable_machine(primary)
    result.update(actions=counts, vdso_elf_machines=vdsos, target_built=build_target)
    record()
    assert build_target
    assert set(counts) == {'JekyllSDK', 'JekyllSite', 'WebsiteSyscallJSON', 'WebsiteSyscallDocs'}, counts
    assert set(vdsos.values()) == {62, 183}, vdsos
    manifest = Path('bazel-bin/website/site')
    assert (manifest / 'index.html').is_file() and (manifest / 'blog/index.xml').is_file()
    result['site_files'] = sum(p.is_file() for p in manifest.rglob('*'))
    syscalls = json.loads(Path('bazel-bin/website/syscalls.json').read_bytes())
    tables = syscalls['linux']
    assert {'amd64', 'arm64'} <= tables.keys(), sorted(tables)
    for arch, number in {'amd64': '0', 'arm64': '63'}.items():
        assert tables[arch]['syscalls'][number]['name'] == 'read', arch
    result['linux_syscalls'] = {arch: len(tables[arch]['syscalls']) for arch in ('amd64', 'arm64')}
    result['syscall_json_sha256'] = hashlib.sha256(Path('bazel-bin/website/syscalls.json').read_bytes()).hexdigest()
    with (TMP / 'site-query.bep').open() as f:
        query_done = [e['finished'].get('overallSuccess', False) for e in map(json.loads, f) if 'finished' in e]
    assert query_done == [True], query_done
    result['status'] = 'PASS'
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    finally:
        record()
