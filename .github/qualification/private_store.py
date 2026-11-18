"""Checkpoint-only private Docker store and namespace identity checks."""

import json
from pathlib import Path
import re
from typing import Literal, NotRequired, Protocol, TypedDict


Features = TypedDict('Features', {'containerd-snapshotter': Literal[False]})
PrivateConfig = TypedDict('PrivateConfig', {
    'experimental': Literal[True], 'data-root': str, 'exec-root': str,
    'containerd-namespace': str, 'containerd-plugins-namespace': str,
    'features': Features,
})


class DockerStoreInfo(TypedDict):
    DockerRootDir: str
    Driver: str
    DriverStatus: NotRequired[list[list[str]] | None]


class Inventory(TypedDict):
    root: str
    driver: str
    driverStatus: list[list[str]] | None
    images: list[str]
    volumes: list[str]


class Command(Protocol):
    def __call__(self, *args: str) -> str: ...


def configuration(work: Path, namespace: str) -> PrivateConfig:
    assert work.is_absolute() and work.resolve() == work
    assert re.fullmatch(r'checkpoint-cost-[0-9]+', namespace), namespace
    return {
        'experimental': True,
        'data-root': str(work / 'docker-data'),
        'exec-root': str(work / 'docker-exec'),
        'containerd-namespace': namespace,
        'containerd-plugins-namespace': namespace + '-plugins',
        'features': {'containerd-snapshotter': False},
    }


def verify_config(actual: PrivateConfig, private: PrivateConfig) -> None:
    assert isinstance(actual, dict)
    for key in ('data-root', 'exec-root', 'containerd-namespace', 'containerd-plugins-namespace'):
        assert actual.get(key) == private[key], (key, actual.get(key), private[key])
    assert isinstance(actual.get('features'), dict)
    assert actual['features'].get('containerd-snapshotter') is False
    assert actual.get('experimental') is True


def verify_info(actual: DockerStoreInfo, private: PrivateConfig) -> None:
    assert actual['DockerRootDir'] == private['data-root'], actual['DockerRootDir']
    assert isinstance(actual['Driver'], str) and actual['Driver'] != 'overlayfs'
    status = actual.get('DriverStatus')
    assert status is None or isinstance(status, list)
    assert not any('io.containerd.snapshotter.v1' in str(row) for row in status or []), status


def verify_unit(text: str) -> None:
    for key in ('config-file', 'data-root', 'exec-root', 'containerd-namespace', 'containerd-plugins-namespace', 'experimental', 'feature'):
        assert '--' + key not in text, (key, text)


def external_containerd(unit: str, original_config: dict[str, object]) -> str:
    flags = re.findall(r'--containerd(?:=|\s+)([^\s;]+)', unit)
    assert len(flags) <= 1, flags
    address = flags[0] if flags else original_config.get('containerd')
    # The actual namespace gate must use a recorded endpoint. Do not guess the
    # managed daemon's socket or mutate packages before discovering ambiguity.
    assert address == '/run/containerd/containerd.sock', address
    return address


def inventory(command: Command) -> Inventory:
    info = json.loads(command('docker', 'info', '--format', '{{json .}}'))
    assert isinstance(info, dict) and isinstance(info.get('DockerRootDir'), str) and isinstance(info.get('Driver'), str)
    status = info.get('DriverStatus')
    assert status is None or (isinstance(status, list) and all(isinstance(row, list) and all(isinstance(value, str) for value in row) for row in status))
    images = command('docker', 'image', 'ls', '--no-trunc', '--digests', '--format', '{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}')
    volumes = command('docker', 'volume', 'ls', '--format', '{{.Name}}\t{{.Driver}}')
    return {'root': info['DockerRootDir'], 'driver': info['Driver'], 'driverStatus': status,
            'images': sorted(set(images.splitlines())), 'volumes': sorted(set(volumes.splitlines()))}


def probe_owned(info: dict[str, object], marker: dict[str, str]) -> str:
    assert re.fullmatch(r'checkpoint-cost-store-[a-f0-9]{32}', marker['name'])
    assert re.fullmatch(r'sha256:[a-f0-9]{64}', marker['image']) and marker['runtime'] == 'runc'
    identifier = info['Id']
    assert isinstance(identifier, str) and re.fullmatch(r'[a-f0-9]{64}', identifier)
    assert info['Name'] == '/' + marker['name'] and info['Image'] == marker['image']
    host = info['HostConfig']
    assert isinstance(host, dict) and host.get('Runtime') == 'runc'
    assert 'id' not in marker or marker['id'] == identifier
    return identifier
