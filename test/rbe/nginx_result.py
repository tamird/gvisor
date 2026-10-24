"""Nginx fixture expectations over the shared retained image verifier."""
import argparse
import hashlib
import json
from pathlib import Path
import re

import qualification_report as report
from image_result import (
    ImageBundle,
    project_retained_image,
    retained_test_report,
    retained_test_xml,
    verify_mixed_graph,
    verify_mixed_oci_images,
    verify_retained_runtime,
    verify_uncached_attempt,
)


def verify(prefix: Path, bundle: ImageBundle, receipt: dict, plan: dict, config: dict,
           metadata: Path, fixture: Path) -> dict:
    graph_only = config.get('graph_only', False)
    assert plan['expected_native_phases'] == (5 if graph_only else 16)
    package = '//images/benchmarks:'
    graph = verify_mixed_graph(bundle, package, 'nginx')
    if graph_only:
        assert receipt['executed_attempt_count'] == 0 and not receipt['build_outputs']
        return {}
    image = json.loads(bundle.members['nginx-contents.json'])
    outputs, audit = verify_mixed_oci_images(
        bundle, receipt, config, image, package, 'nginx', metadata, 2,
        Path(str(prefix) + '-mixed-events.jsonl'))
    for row in image['architectures']:
        cpu = 'k8' if row['architecture'] == 'amd64' else 'aarch64'
        expected = {'local/index.html', 'etc/nginx/nginx.conf', 'etc/nginx/nginx_gofer.conf'}
        for name in ('nginx.conf', 'nginx_gofer.conf'):
            source = report.file_record(fixture / name)
            item = row['files']['etc/nginx/' + name]
            assert (item['type'], item['mode'], item['uid'], item['gid'], item['sha256'], item['bytes']) == (
                '0', 0o644, 0, 0, source['sha256'], source['bytes'])
            assert {key: row['fixture_configs'][name][key] for key in ('sha256', 'bytes')} == source
        assert len(row['payloads']) == 6
        for size in (0, 1, 10, 100, 1024, 10240):
            path = 'local/latin' + str(size) + 'k.txt' if size else 'local/index.html'
            label = package + ('http_payload_' + str(size) if size else 'nginx_index')
            expected.add(path)
            digest = hashlib.sha256(bytes(size * 1024)).hexdigest()
            item = row['files'][path]
            assert (item['type'], item['mode'], item['uid'], item['gid'], item['bytes'], item['sha256']) == (
                '0', 0o644, 0, 0, size * 1024, digest)
            [payload] = [item for item in row['payloads'] if item['installed'] == path]
            [owner] = [item for item in graph['rows'] if item['label'].removeprefix('@@') == label and item['cpu'] == cpu and not item['is_exec']]
            assert payload['owner'] == owner and len(owner['files']) == 1
            assert payload['output']['path'] == graph['execution_root'] + '/' + owner['files'][0]
            assert (payload['output']['bytes'], payload['output']['sha256']) == (size * 1024, digest)
        assert {name for name, item in row['files'].items() if item['type'] != '5'} == expected
        assert all(item['mode'] == 0o755 for item in row['files'].values() if item['type'] == '5')
    runtime = json.loads(bundle.members['nginx-runtime-results.json'])
    setup = json.loads(bundle.members['nginx-runtime-preparation.json'])
    retained = verify_retained_runtime(
        bundle, config, runtime, [setup], image['architectures'][0], 'nginx',
        Path(str(prefix) + '-runtime-supplier-events.jsonl'))
    assert runtime['errors'] == [] and runtime['runtime_name'] == setup['runtime']
    assert setup['server_before']['Version'] == '24.0.9'
    assert setup['server_info']['ExperimentalBuild'] is True
    before = setup['server_info_before']
    if not before['ExperimentalBuild']:
        assert setup['daemon_action'] == 'Restart sole disposable daemon with declared fixture startup settings'
        assert setup['fixture_server_info']['ExperimentalBuild'] is True
        restored = setup['restored_server_info']
        for key in ('ID', 'DockerRootDir', 'Driver', 'ServerVersion', 'SecurityOptions', 'ExperimentalBuild', 'Runtimes'):
            assert restored[key] == before[key], (key, restored[key], before[key])
    else:
        assert setup['daemon_action'] == 'Reload existing daemon runtimes'
    installed = setup['config_after']['runtimes'][setup['runtime']]
    assert setup['server_info']['Runtimes'][setup['runtime']] == installed
    [runsc] = [item for item in setup['runtime_files'] if item['path'].endswith('/nginx-runtime/runsc')]
    assert installed['path'] == runsc['path']
    assert [arg for arg in installed['runtimeArgs'] if arg.startswith('--platform=')] == ['--platform=systrap']
    assert [arg for arg in installed['runtimeArgs'] if arg.startswith('--network=')] == ['--network=sandbox']
    expected_groups = {
        'nginx-benchmark-serving': ('//test/benchmarks/network:nginx_test',
            ['BenchmarkNginxDocSize/filesize.' + size + '/concurrency.1/filesystem.' + filesystem
             for size in ('1Kb', '1Mb', 'notfound') for filesystem in ('tmpfs', 'bind')],
            100, ['ns/op', 'average_latency.s', 'requests_per_second.QPS']),
        'nginx-benchmark-lifecycle': ('//test/benchmarks/base:startup_test',
            ['Benchmark' + op + 'Nginx' for op in ('Startup', 'Checkpoint', 'Restore')], 1, ['ns/op']),
        'nginx-benchmark-size': ('//test/benchmarks/base:size_test', ['BenchmarkSizeNginx'],
            1, ['ns/op', 'average_container_size.bytes']),
        'nginx-benchmark-usage': ('//test/benchmarks/base:usage_test', ['BenchmarkSizeNginx'],
            1, ['ns/op', 'average_container_memory_usage.bytes']),
    }
    assert [row['phase'] for row in runtime['consumers']] == list(expected_groups)
    assert receipt['executed_attempt_count'] == 4
    for row in runtime['consumers']:
        phase = row['phase']
        owner, names, iterations, units = expected_groups[phase]
        assert row['status'] == 'PASS' and row['target'] == owner
        assert (row['required_benchmarks'], row['iterations'], row['units']) == (names, iterations, units)
        attempt = verify_uncached_attempt(bundle, phase, owner)
        assert row['native_attempts'] == [attempt] and row['benchmarks'] == attempt['benchmarks']
        actual = {re.sub(r'-\d+$', '', item['name']): item for item in attempt['benchmarks']}
        assert len(actual) == len(attempt['benchmarks']) == len(names) and set(actual) == set(names)
        assert all(item['iterations'] == iterations and set(item['metrics']) == set(units) for item in actual.values())
        args = bundle.members['invocation-' + phase + '/arguments.txt'].decode().splitlines()
        assert [arg for arg in args if arg.startswith('--test_arg=-runtime=')] == ['--test_arg=-runtime=' + setup['runtime']]
        assert '--test_env=GO_TEST_WRAP_TESTV=0' in args and '--test_arg=-test.v=false' in args
        assert json.loads(bundle.members[phase + '-declared-benchmarks.json']) == {'owners': {owner: [
            {'name': name, 'count': 1, 'iterations': iterations, 'units': units} for name in names]}}
        retained_test_xml(bundle, phase, row['xml'])
        retained_test_report(bundle, phase, 'log', row['benchmark_log'])
        assert row['counts']['failed'] == row['counts']['skipped'] == 0
    helpers = json.loads(bundle.members['benchmark-helper-images.json'])
    assert helpers == runtime['benchmark_helpers'] and helpers['status'] == 'PASS' and helpers['source'] == bundle.source
    assert helpers['commands'] and all(command['native_exit'] == 0 for command in helpers['commands'])
    hey, util = helpers['hey'], helpers['util']
    assert hey['target'] == '//images/benchmarks:hey_archive'
    assert hey['archive']['bytes'] > 0 and re.fullmatch(r'[0-9a-f]{64}', hey['archive']['sha256'])
    assert (hey['config']['os'], hey['config']['architecture']) == ('linux', 'amd64')
    assert hey['files']['usr/bin/hey']['mode'] == 0o755 and hey['files']['usr/bin/hey']['bytes'] > 0
    assert re.fullmatch(r'[0-9a-f]{64}', hey['files']['usr/bin/hey']['sha256'])
    assert hey['image']['Id'] == 'sha256:' + hey['config_sha256']
    assert util['supplier'] == config['benchmark_utility']
    assert util['supplier']['immutable_image'].endswith('@' + util['supplier']['manifest_digest'])
    assert util['image']['Id'] == util['supplier']['image_config_digest']
    assert util['version'].startswith('GNU Wget ') and util['cleanup']['native_exit'] == 0
    assert set(runtime['benchmark_client_images']) == {'hey', 'util'}
    for name, helper in (('hey', hey), ('util', util)):
        assert helper['name'] == 'gvisor.dev/images/benchmarks/' + name + ':latest'
        final_image = runtime['benchmark_client_images'][name]
        assert final_image['Id'] == helper['image']['Id'] and helper['name'] in final_image['RepoTags']
        assert (final_image['Os'], final_image['Architecture']) == ('linux', 'amd64')
    assert [row['filesystem'] for row in runtime['responses']] == ['tmpfs', 'bind']
    for row in runtime['responses']:
        assert row['status'] == 'PASS' and row['version'] == 'nginx version: nginx/1.25.1'
        assert row['cleanup']['native_exit'] == 0
        assert [item['path'] for item in row['responses']] == ['latin1k.txt', 'latin1024k.txt', 'notfound']
        for item, size, status in zip(row['responses'], (1024, 1024 * 1024, None), (200, 200, 404), strict=True):
            assert item['status'] == status and item['headers'].get('content-encoding') in (None, 'identity')
            if size is None:
                assert 0 < item['bytes'] <= 1024 * 1024 and item['headers']['content-type'].split(';', 1)[0] == 'text/html'
            else:
                assert (item['bytes'], item['sha256']) == (size, hashlib.sha256(bytes(size)).hexdigest())
    return {'outputs': outputs, 'audit': audit, 'upstream_image': image, 'runtime': runtime, 'runtime_supplier': retained}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prefix', type=Path)
    parser.add_argument('metadata', type=Path)
    parser.add_argument('fixture', type=Path)
    parser.add_argument('--manual', type=int, default=0)
    args = parser.parse_args()
    refs = [Path(__file__), *[args.fixture / name for name in ('nginx.conf', 'nginx_gofer.conf')],
            *[args.metadata / (name + '.json') for name in ('index', 'amd64-manifest', 'amd64-config', 'arm64-manifest', 'arm64-config')]]
    return project_retained_image(
        args.prefix, args.manual,
        lambda prefix, bundle, receipt, plan, config: verify(prefix, bundle, receipt, plan, config, args.metadata, args.fixture),
        refs, 'Pinned official Nginx1.25.1 plus unchanged benchmark fixtures. Both image platforms; eleven native AMD64 functional benchmark rows with one separately authenticated retained runtime and immutable helpers. No ARM runtime, source-built Nginx or comparative performance claim.')


if __name__ == '__main__':
    raise SystemExit(main())
