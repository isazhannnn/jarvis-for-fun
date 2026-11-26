"""Install models and portable CPU inference runtime from their publishers."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent
HEADERS = {'User-Agent': 'JARVIS-Desktop-Setup/1.0'}


def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=45) as response:
        return json.load(response)


def download(url, destination, expected_sha=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 1000:
        if expected_sha is None or hashlib.file_digest(destination.open('rb'), 'sha256').hexdigest() == expected_sha:
            print(f'Already present: {destination.name}', flush=True)
            return
    temp = destination.with_suffix(destination.suffix + f'.part.{os.getpid()}')
    for attempt in range(3):
        try:
            sha = hashlib.sha256()
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as response, temp.open('wb') as output:
                length = int(response.headers.get('Content-Length', 0))
                size, last = 0, time.monotonic()
                while chunk := response.read1(64 * 1024):
                    output.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
                    if time.monotonic() - last > 4:
                        print(f'{destination.name}: {size / 2**20:.0f} MB / {length / 2**20:.0f} MB', flush=True)
                        last = time.monotonic()
            if length and size != length:
                raise ValueError(f'Incomplete download of {destination.name}: {size}/{length} bytes')
            if expected_sha and sha.hexdigest() != expected_sha:
                raise ValueError('SHA256 verification failed for ' + destination.name)
            temp.replace(destination)
            print(f'Downloaded {destination.name}: {size / 2**20:.1f} MB; SHA256 {sha.hexdigest()}', flush=True)
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)


def extract(archive, folder):
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            target = (folder / name).resolve()
            if not target.is_relative_to(folder.resolve()):
                raise ValueError('Unexpected path in archive')
        bundle.extractall(folder)


def install_models():
    from concurrent.futures import ThreadPoolExecutor
    model_dir = ROOT / 'models'
    def speech():
        if (model_dir / 'vosk-model-small-ru-0.22/am/final.mdl').exists():
            print('Speech model already installed', flush=True)
            return
        path = ROOT / 'downloads/vosk-model-small-ru-0.22.zip'
        from scripts.parallel_download import download as ranged_download
        ranged_download('https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip', path,
                        verify_zip=True, cache_dir=ROOT / 'downloads/parts')
        extract(path, model_dir)
    def hands():
        download('https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task', model_dir / 'hand_landmarker.task')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(speech), pool.submit(hands)]
        for future in futures:
            future.result()


def install_brain():
    model_name = 'qwen2.5-1.5b-instruct-q4_k_m.gguf'
    model = ROOT / 'models' / model_name
    from concurrent.futures import ThreadPoolExecutor
    def runtime():
        destination = ROOT / 'runtime/llama'
        if (destination / 'llama-server.exe').exists():
            print('llama.cpp runtime already installed', flush=True)
            return
        # This official release is verified on the target Windows installation.
        # Newer split-DLL builds are rejected by its existing signing policy.
        release = fetch_json('https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b5046')
        assets = [a for a in release['assets'] if a['name'].endswith('bin-win-avx2-x64.zip')]
        if not assets:
            raise RuntimeError('Compatible Windows CPU runtime unavailable')
        asset = assets[0]
        digest = (asset.get('digest') or '').removeprefix('sha256:') or None
        archive = ROOT / 'downloads' / asset['name']
        from scripts.parallel_download import download as ranged_download
        ranged_download(asset['browser_download_url'], archive, sha256=digest,
                        size=asset['size'], verify_zip=True, cache_dir=ROOT / 'downloads/parts')
        extract(archive, destination)
        (ROOT / 'runtime/source.json').write_text(json.dumps({'release': release['tag_name'], 'url': asset['browser_download_url'], 'digest': digest}, indent=2), encoding='utf-8')
    def weights():
        metadata = fetch_json('https://huggingface.co/api/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF?blobs=true')
        sibling = next(s for s in metadata['siblings'] if s['rfilename'] == model_name)
        sha = sibling.get('lfs', {}).get('sha256') or sibling.get('lfs', {}).get('oid')
        revision = metadata['sha']
        download(f'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/{revision}/{model_name}?download=true', model, sha)
        (ROOT / 'models/brain-source.json').write_text(json.dumps({'repository': 'Qwen/Qwen2.5-1.5B-Instruct-GGUF', 'revision': revision, 'filename': model_name, 'sha256': sha}, indent=2), encoding='utf-8')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runtime), pool.submit(weights)]
        for future in futures:
            future.result()


def install_multilingual():
    metadata = fetch_json('https://huggingface.co/api/models/Systran/faster-whisper-base?blobs=true')
    revision = metadata['sha']
    wanted = {'config.json', 'model.bin', 'tokenizer.json', 'vocabulary.json', 'vocabulary.txt', 'preprocessor_config.json'}
    for sibling in metadata['siblings']:
        name = sibling['rfilename']
        if name not in wanted:
            continue
        sha = sibling.get('lfs', {}).get('sha256') or sibling.get('lfs', {}).get('oid')
        download(f'https://huggingface.co/Systran/faster-whisper-base/resolve/{revision}/{name}?download=true', ROOT / 'models/faster-whisper-base' / name, sha)
    (ROOT / 'models/faster-whisper-base/source.json').write_text(json.dumps({'repository': 'Systran/faster-whisper-base', 'revision': revision}, indent=2), encoding='utf-8')
    install_portable_speech()


def install_portable_speech():
    from scripts.parallel_download import download as ranged_download
    runtime = ROOT / 'runtime/whisper-cpp'
    if not list(runtime.rglob('whisper-cli.exe')):
        release = fetch_json('https://api.github.com/repos/ggml-org/whisper.cpp/releases/tags/b5454')
        asset = next(a for a in release['assets'] if a['name'] == 'whisper-bin-x64.zip')
        digest = '6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb'
        archive = ROOT / 'downloads/whisper-bin-x64.zip'
        ranged_download(asset['browser_download_url'], archive, sha256=digest, size=asset['size'],
                        cache_dir=ROOT / 'downloads/parts')
        extract(archive, runtime)
    download('https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin',
             ROOT / 'models/whisper-cpp/ggml-base.bin',
             '60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--brain', action='store_true')
    parser.add_argument('--multilingual', action='store_true')
    args = parser.parse_args()
    install_models()
    if args.brain:
        install_brain()
    if args.multilingual:
        install_multilingual()
    print('Runtime setup complete.', flush=True)
