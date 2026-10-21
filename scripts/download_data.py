"""Download a pinned public dataset snapshot, preserving original files."""
import argparse
import concurrent.futures
import hashlib
import json
import os
import urllib.error
from pathlib import Path
import time
import urllib.request

REVISION = '216fb6e2c9449dd8eeab0a3b9233e89ea08d1f7a'
FILES = ['.gitattributes', 'AGENTS.md', 'LICENSE', 'README.md',
         'sample/CHECKSUMS.json', 'sample/labels/labels.jsonl.zst',
         'sample/messages/messages.jsonl.zst', 'sample/trajectories/trajectories.jsonl.zst',
         'sample/views/error_recovery.jsonl.zst', 'sample/views/sft_steps.jsonl.zst',
         'sample/views/tool_grammar.jsonl.zst']

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', default='data/gatcv6')
    p.add_argument('--proxy', help='Optional HTTP proxy URL')
    a = p.parse_args()
    root = Path(a.output)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {'https': a.proxy, 'http': a.proxy} if a.proxy else None))
    token_path = Path(os.environ.get('HF_TOKEN_PATH', str(Path(os.environ.get('HF_HOME', str(Path.home()/'.cache/huggingface'))) / 'token')))
    token = os.environ.get('HF_TOKEN') or os.environ.get('HUGGING_FACE_HUB_TOKEN')
    if not token and token_path.is_file():
        token = token_path.read_text().strip()
    if token:
        opener.addheaders = [('Authorization', 'Bearer ' + token)]
    start = time.monotonic()
    def fetch(name):
        target = root / 'raw' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f'https://huggingface.co/datasets/GOAT-AI/gatcv6/resolve/{REVISION}/{name}'
        for attempt in range(3):
            try:
                with opener.open(url, timeout=120) as response, target.with_suffix(target.suffix+'.part').open('wb') as out:
                    while chunk := response.read(1024*1024):
                        out.write(chunk)
                target.with_suffix(target.suffix+'.part').replace(target)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise RuntimeError('Access denied: request dataset access on Hugging Face, then run hf auth login. No data file was downloaded for ' + name) from None
                if attempt == 2: raise
            except Exception:
                if attempt == 2: raise
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        print(f'{name}: {target.stat().st_size} bytes', flush=True)
        return {'path': name, 'bytes': target.stat().st_size, 'sha256': digest}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        files = list(pool.map(fetch, FILES))
    manifest = {'dataset': 'GOAT-AI/gatcv6', 'revision': REVISION,
                'downloaded_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'elapsed_seconds': round(time.monotonic()-start, 3), 'files': files}
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    expected = json.loads((root / 'raw/sample/CHECKSUMS.json').read_text())
    by_path = {f['path']: f['sha256'] for f in files}
    verified = all(by_path['sample/' + name] == digest for name, digest in expected.items())
    status = {'status': 'complete' if verified else 'checksum_failed',
              'revision': REVISION, 'data_files_downloaded': len(expected),
              'published_checksums_match': verified}
    (root / 'download_status.json').write_text(json.dumps(status, indent=2)+'\n')
    if not verified: raise ValueError('Published checksum mismatch')
    print('Download complete; published checksums verified.', flush=True)

if __name__ == '__main__': main()
