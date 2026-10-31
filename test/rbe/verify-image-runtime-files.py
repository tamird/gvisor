"""Read one produced OCI manifest, Docker archive, and original pinned file digests."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
record_file, key, manifest_paths, archive_paths, destination = sys.argv[1:]
original=json.loads(Path(record_file).read_text())[key]
out=Path(destination)
def unique(paths,ending):
    matches=[Path(p) for p in Path(paths).read_text().splitlines() if p.endswith(ending)]
    assert len(matches)==1,(paths,ending,matches)
    return matches[0]
manifest=json.loads(unique(manifest_paths,'_manifest.json').read_text())
config=json.loads(unique(manifest_paths,'_config.json').read_text())
archive=unique(archive_paths,'.tar')
layers=manifest['layers']
assert [(l['digest'],l['size']) for l in layers[:-1]]==[(l['digest'],l['size']) for l in original['base_layers']]
assert config['architecture']==original['architecture'] and config['os']=='linux'
cfg=config['config']
for k,v in original['runtime_config'].items():
    assert (cfg.get(k) or None)==(v or None),(k,cfg.get(k),v)
with tarfile.open(archive) as full:
    docker,=json.load(full.extractfile('manifest.json'))
    assert len(docker['Layers'])==len(layers)
    stored=full.extractfile(docker['Layers'][-1]).read()
    with tarfile.open(fileobj=io.BytesIO(stored),mode='r:*') as tar:
        files={}
        for e in tar.getmembers():
            path=e.name.removeprefix('./').rstrip('/')
            assert '.runfiles' not in path and not path.endswith('.repo_mapping'),path
            if e.isdir():continue
            assert e.isfile(),(path,e.type)
            assert e.mode==(0o644 if path.endswith('/COPYING') else 0o755),(path,oct(e.mode))
            files[path]=hashlib.sha256(tar.extractfile(e).read()).hexdigest()
        assert files==original['files'],(key,files,original['files'])
current=sum(x['size'] for x in layers);old=sum(x['size'] for x in original['base_layers'])+original['old_layer_bytes']
record={'source':original['corrected'],'case':key,'before':original['old_layer_sha256'],'after':layers[-1]['digest'],'base_layers':layers[:-1],'files':files,'old':old,'new':current,'shipped_original_image':original['shipped_bytes'],'diff_bytes':current-old}
out.write_text(json.dumps(record,indent=2)+'\n')
print(key,'verified',len(files),'runtime files, layer bytes',original['old_layer_bytes'],'->',layers[-1]['size'],'whole image',old,'->',current)
