"""Build and verify a deterministic local submission zip from an explicit file set."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT_FILES=('README.md','提交说明.md','requirements.txt','requirements-lock.txt',
            'requirements-inspection.txt','.gitignore')
ROOT_DIRS=('agent_qc','configs','scripts','tests','outputs','reports','data')
PREFIX='agent-qc-delivery/'


def sha(data):return hashlib.sha256(data).hexdigest()


def verify_archive(path):
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        if len(names)!=len(set(names)):raise ValueError('Duplicate archive entry')
        manifest=json.loads(z.read(PREFIX+'delivery_manifest.json'))
        expected={PREFIX+p for p in manifest['files']}
        if set(names)!=expected|{PREFIX+'delivery_manifest.json'}:
            raise ValueError('Archive inventory differs')
        for relative,meta in manifest['files'].items():
            data=z.read(PREFIX+relative)
            if len(data)!=meta['bytes'] or sha(data)!=meta['sha256']:
                raise ValueError('Archive hash differs: '+relative)
    return len(manifest['files'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='dist/agent-qc-delivery.zip')
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args();target=Path(args.output)
    if args.verify_only:
        print(json.dumps({'verified_files':verify_archive(target),'archive':str(target)}));return
    paths=[Path(p) for p in ROOT_FILES]
    for root in ROOT_DIRS:
        paths += [p for p in Path(root).rglob('*') if p.is_file() and not p.is_symlink()
                  and p.parts[:3]!=('reports','truncation_fix','before_review')
                  and not {'__pycache__','.venv'}.intersection(p.parts)
                  and p.name!='.DS_Store' and p.suffix not in ('.pyc','.part')]
    paths=sorted(set(paths))
    metadata=json.loads(Path('reports/交付复现信息.json').read_text())
    # Refuse to package a stale source inventory after scripts/tests/config edits.
    for name,h in metadata['code']['source_inventory'].items():
        if sha(Path(name).read_bytes())!=h:raise ValueError('Refresh delivery metadata after editing: '+name)
    inventory={p.as_posix():{'bytes':p.stat().st_size,'sha256':sha(p.read_bytes())} for p in paths}
    manifest={'format_version':1,'dataset_revision':metadata['dataset']['revision'],
        'pipeline_code_sha256':metadata['code']['pipeline_code_sha256'],
        'submission_source_sha256':metadata['code']['submission_source_sha256'],
        'status':metadata['submission_status'],'file_count':len(inventory),'files':inventory,
        'note':'清单不包含自身；压缩包外附整个zip的SHA256。时间戳固定以保证相同内容可重复打包。'}
    manifest_path=Path('delivery_manifest.json')
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in paths+[manifest_path]:
            info=zipfile.ZipInfo(PREFIX+p.as_posix(),date_time=(1980,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            z.writestr(info,p.read_bytes())
    count=verify_archive(target);digest=sha(target.read_bytes())
    target.with_suffix(target.suffix+'.sha256').write_text(digest+'  '+target.name+'\n')
    result={'archive':str(target),'bytes':target.stat().st_size,'sha256':digest,'verified_files':count,
            'status':metadata['submission_status']}
    (target.parent/'package_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
