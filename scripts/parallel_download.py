"""Resume small parallel HTTP ranges and accept only the supplied SHA-256.

CLI: python parallel_download.py URL DEST --size BYTES --sha256 HEX
    [--workers 16] [--chunk-size 131072] [--cache-dir DOWNLOADS/parts]
For an official ZIP without a published checksum, explicitly use --verify-zip
without --sha256; this checks exact size and archive CRC and reports the
observed digest without presenting it as a publisher-authenticated checksum.

No archives are extracted and no downloaded program is executed here.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile


HEADERS = {'User-Agent': 'JARVIS-Setup/1.0', 'Accept-Encoding': 'identity'}


class RangeUnsupported(RuntimeError):
    pass


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _source(url, expected_size):
    """Resolve the potentially signed CDN URL once, then probe a one-byte range."""
    resolved, head_size = url, None
    try:
        with urlopen(Request(url, headers=HEADERS, method='HEAD'), timeout=30) as response:
            resolved = response.geturl()
            if response.headers.get('Content-Length'):
                head_size = int(response.headers['Content-Length'])
    except (HTTPError, URLError, OSError, ValueError):
        pass
    try:
        with urlopen(Request(resolved, headers={**HEADERS, 'Range': 'bytes=0-0'}), timeout=30) as response:
            resolved = response.geturl()
            status = response.getcode()
            content_range = response.headers.get('Content-Range', '')
            match = re.fullmatch(r'bytes 0-0/(\d+)', content_range.strip())
            if status == 206 and match:
                discovered = int(match[1])
                if len(response.read1(2)) != 1:
                    raise RuntimeError('Range probe returned an unexpected body size')
                supports_range = True
            elif status == 200:
                discovered = int(response.headers.get('Content-Length') or head_size or expected_size or 0)
                supports_range = False
            else:
                raise RuntimeError(f'Unexpected HTTP range probe: {status} {content_range!r}')
    except HTTPError as exc:
        # Some signed CDNs bind the URL method. Retry the original URL once.
        if resolved != url and exc.code in (400, 401, 403):
            with urlopen(Request(url, headers={**HEADERS, 'Range': 'bytes=0-0'}), timeout=30) as response:
                resolved = response.geturl()
                match = re.fullmatch(r'bytes 0-0/(\d+)', response.headers.get('Content-Range', '').strip())
                supports_range = response.getcode() == 206 and match is not None
                discovered = int(match[1]) if supports_range else int(response.headers.get('Content-Length') or expected_size or 0)
        else:
            raise
    total = int(expected_size or discovered or head_size or 0)
    if total <= 0:
        raise RuntimeError('Source size is unknown; provide --size')
    if discovered and discovered != total:
        raise RuntimeError(f'Source size mismatch: expected {total}, server reports {discovered}')
    return resolved, total, supports_range


def _read_into(response, stream, expected, deadline, stopped):
    count = 0
    while count < expected:
        if stopped.is_set():
            raise RuntimeError('Download cancelled after another chunk failed')
        if time.monotonic() > deadline:
            raise TimeoutError('Chunk exceeded its 90-second total deadline')
        block = response.read1(min(65536, expected - count))
        if not block:
            raise RuntimeError(f'Incomplete response: {count}/{expected} bytes')
        stream.write(block)
        count += len(block)
    if response.read1(1):
        raise RuntimeError('Server returned more bytes than requested')
    return count


def _part(url, total, start, end, path, stopped):
    expected = end - start + 1
    if path.is_file() and path.stat().st_size == expected:
        return expected, True
    partial = path.with_suffix('.partial')
    last_error = None
    deadline = time.monotonic() + 90
    for attempt in range(3):
        if stopped.is_set():
            raise RuntimeError('Download cancelled')
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Chunk exceeded its 90-second total deadline')
            with urlopen(Request(url, headers={**HEADERS, 'Range': f'bytes={start}-{end}'}), timeout=min(30, remaining)) as response:
                if response.getcode() == 200:
                    raise RangeUnsupported('Server ignored Range; refusing multiple full-file responses')
                expected_header = f'bytes {start}-{end}/{total}'
                if response.getcode() != 206 or response.headers.get('Content-Range', '').strip() != expected_header:
                    raise RuntimeError('Incorrect Content-Range: ' + repr(response.headers.get('Content-Range')))
                content_length = response.headers.get('Content-Length')
                if content_length is not None and int(content_length) != expected:
                    raise RuntimeError('Incorrect range Content-Length')
                with partial.open('wb') as output:
                    _read_into(response, output, expected, deadline, stopped)
            if partial.stat().st_size != expected:
                raise RuntimeError('Part size verification failed')
            os.replace(partial, path)
            return expected, False
        except RangeUnsupported:
            raise
        except (OSError, ValueError, RuntimeError, URLError) as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                break
            if attempt < 2:
                stopped.wait(.4 * (attempt + 1))
    raise RuntimeError(f'Chunk {start}-{end} failed after 3 attempts: {last_error}')


def _validate(path, total, sha256, verify_zip):
    if path.stat().st_size != total:
        raise RuntimeError('Downloaded size verification failed')
    observed = file_sha256(path)
    if sha256 is not None and observed != sha256:
        raise RuntimeError('SHA-256 verification failed')
    if verify_zip:
        try:
            with zipfile.ZipFile(path) as archive:
                damaged = archive.testzip()
                if damaged is not None:
                    raise RuntimeError('ZIP CRC check failed: ' + damaged)
        except zipfile.BadZipFile as exc:
            raise RuntimeError('Downloaded file is not a valid ZIP') from exc
    return observed


def _single(url, destination, total, sha256, verify_zip=False):
    print('Server ignores Range; using one verified download.', flush=True)
    assembling = destination.with_name(destination.name + '.assembling')
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=30) as response, assembling.open('wb') as output:
                _read_into(response, output, total, time.monotonic() + 1800, threading.Event())
            observed = _validate(assembling, total, sha256, verify_zip)
            os.replace(assembling, destination)
            print(('Verified publisher SHA-256: ' if sha256 else 'Verified ZIP CRC/size; observed SHA-256: ') + observed, flush=True)
            return destination
        except (OSError, RuntimeError, URLError):
            if attempt == 2:
                raise
    raise RuntimeError('Download failed')


def download(url, destination, *, sha256=None, size=None, workers=16, chunk_size=131072, cache_dir=None, verify_zip=False):
    sha256 = str(sha256).casefold() if sha256 is not None else None
    if sha256 is None and not verify_zip:
        raise ValueError('An official SHA-256 is required unless --verify-zip is explicitly enabled')
    if sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise ValueError('An official 64-character SHA-256 is required')
    if not str(url).startswith('https://'):
        raise ValueError('Only HTTPS download URLs are accepted')
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sha256 is not None and destination.is_file() and (not size or destination.stat().st_size == int(size)) and file_sha256(destination) == sha256:
        if verify_zip:
            _validate(destination, destination.stat().st_size, sha256, True)
        print('Already verified: ' + str(destination), flush=True)
        return destination
    started = time.monotonic()
    resolved, total, supported = _source(url, size)
    if sha256 is None and destination.is_file() and destination.stat().st_size == total:
        observed = _validate(destination, total, None, True)
        print('Existing ZIP CRC/size verified; observed SHA-256: ' + observed, flush=True)
        return destination
    workers = max(1, min(32, int(workers)))
    chunk_size = max(16384, min(1048576, int(chunk_size)))
    print(f'Download {total / 1048576:.2f} MiB; {workers} workers; {chunk_size // 1024} KiB chunks', flush=True)
    if not supported:
        return _single(resolved, destination, total, sha256, verify_zip)
    cache_root = Path(cache_dir) if cache_dir is not None else destination.parent / 'parts'
    cache_key = sha256 or hashlib.sha256((url + ':' + str(total)).encode('utf-8')).hexdigest()
    parts_dir = cache_root.expanduser().resolve() / cache_key
    parts_dir.mkdir(parents=True, exist_ok=True)
    specifications = [(start, min(start + chunk_size, total) - 1,
                       parts_dir / f'{start:012d}-{min(start + chunk_size, total) - 1:012d}.part')
                      for start in range(0, total, chunk_size)]
    completed = 0
    downloaded = 0
    resumed = 0
    stopped = threading.Event()
    range_fallback = False
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='http-range')
    futures = []
    try:
        futures = [pool.submit(_part, resolved, total, start, end, path, stopped)
                   for start, end, path in specifications]
        for future in as_completed(futures):
            try:
                count, cached = future.result()
            except RangeUnsupported:
                range_fallback = True
                stopped.set()
                break
            completed += 1
            downloaded += 0 if cached else count
            resumed += count if cached else 0
            if completed % 8 == 0 or completed == len(specifications):
                elapsed = max(.01, time.monotonic() - started)
                percentage = 100 * (downloaded + resumed) / total
                print(f'{percentage:5.1f}% {completed}/{len(specifications)} parts | {downloaded / elapsed / 1048576:.2f} MiB/s | {elapsed:.1f}s', flush=True)
    except BaseException:
        stopped.set()
        raise
    finally:
        if stopped.is_set():
            for future in futures:
                future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    if range_fallback:
        return _single(resolved, destination, total, sha256, verify_zip)
    assembling = destination.with_name(destination.name + '.assembling')
    digest = hashlib.sha256()
    with assembling.open('wb') as output:
        for start, end, part in specifications:
            if part.stat().st_size != end - start + 1:
                raise RuntimeError('Cached part size changed before assembly')
            with part.open('rb') as stream:
                for block in iter(lambda: stream.read(1048576), b''):
                    digest.update(block)
                    output.write(block)
    if assembling.stat().st_size != total or (sha256 is not None and digest.hexdigest() != sha256):
        raise RuntimeError('SHA-256 mismatch; destination was not replaced. Check the official source/hash or clear this asset part cache.')
    observed = _validate(assembling, total, sha256, verify_zip) if verify_zip else digest.hexdigest()
    os.replace(assembling, destination)
    elapsed = time.monotonic() - started
    verification = 'Verified publisher SHA-256' if sha256 else 'Verified ZIP CRC/size; observed SHA-256 ' + observed
    print(f'{verification}; saved {destination} | {elapsed:.1f}s total', flush=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('destination')
    parser.add_argument('--size', type=int)
    parser.add_argument('--sha256')
    parser.add_argument('--verify-zip', action='store_true')
    parser.add_argument('--workers', type=int, default=16)
    parser.add_argument('--chunk-size', type=int, default=131072)
    parser.add_argument('--cache-dir')
    arguments = parser.parse_args()
    download(arguments.url, arguments.destination, sha256=arguments.sha256, size=arguments.size,
             workers=arguments.workers, chunk_size=arguments.chunk_size, cache_dir=arguments.cache_dir,
             verify_zip=arguments.verify_zip)


if __name__ == '__main__':
    main()
