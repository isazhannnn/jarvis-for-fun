"""Portable whisper.cpp adapter with a faster-whisper-compatible small API.

Only NumPy is needed in addition to the Python standard library. The official
CPU runtime lives under runtime/whisper-cpp and the multilingual GGML model
under models/whisper-cpp. Every recording is an ephemeral local PCM WAV.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import wave


@dataclass(frozen=True)
class Segment:
    text: str
    start: float = 0.0
    end: float = 0.0
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0


@dataclass(frozen=True)
class TranscriptionInfo:
    language: str
    duration: float = 0.0
    language_probability: float = 0.0


def parse_whisper_json(payload: dict, language: str | None = None,
                       duration: float = 0.0) -> tuple[list[Segment], TranscriptionInfo]:
    """Read the documented whisper-cli JSON schema, including older plain JSON."""
    if not isinstance(payload, dict) or not isinstance(payload.get('transcription'), list):
        raise ValueError('Whisper вернул некорректный JSON распознавания.')
    result = payload.get('result', {})
    detected = result.get('language') if isinstance(result, dict) else None
    detected = detected if isinstance(detected, str) and re.fullmatch(r'[a-z]{2,3}', detected) else (language or 'auto')
    segments = []
    for item in payload['transcription']:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            continue
        text = item['text'].strip()
        if not text or re.fullmatch(r'(?:\[[_A-Z ]+\]|<\|[^|]+\|>)+', text):
            continue
        offsets = item.get('offsets', {})
        if not isinstance(offsets, dict):
            offsets = {}
        def seconds(value):
            try:
                value = float(value) / 1000
                return max(0.0, value) if math.isfinite(value) else 0.0
            except (TypeError, ValueError):
                return 0.0
        probabilities = []
        tokens = item.get('tokens', [])
        for token in tokens if isinstance(tokens, list) else []:
            if isinstance(token, dict) and isinstance(token.get('p'), (int, float)):
                probability = float(token['p'])
                if math.isfinite(probability) and 0 < probability <= 1:
                    probabilities.append(math.log(max(probability, 1e-10)))
        avg_logprob = sum(probabilities) / len(probabilities) if probabilities else 0.0
        segments.append(Segment(text, seconds(offsets.get('from', 0)), seconds(offsets.get('to', 0)), avg_logprob))
    return segments, TranscriptionInfo(detected, duration)


def _project_directory(location: Path) -> Path:
    location = Path(location).expanduser().resolve()
    if location.suffix.lower() == '.bin':
        location = location.parent
    if location.parent.name == 'models':
        return location.parent.parent
    if location.name == 'models':
        return location.parent
    return location


def resolve_paths(location: Path) -> tuple[Path, Path, Path]:
    """Return project directory, executable and multilingual model, all local."""
    supplied = Path(location).expanduser().resolve()
    project = _project_directory(supplied)
    runtime = project / 'runtime' / 'whisper-cpp'
    executable_candidates = [runtime / 'whisper-cli.exe', runtime / 'Release' / 'whisper-cli.exe',
                             runtime / 'bin' / 'whisper-cli.exe']
    if runtime.is_dir():
        executable_candidates.extend(sorted(runtime.rglob('whisper-cli.exe')))
    executable = next((candidate for candidate in executable_candidates if candidate.is_file()), None)
    model_candidates = [project / 'models' / 'whisper-cpp' / 'ggml-base.bin',
                        project / 'models' / 'ggml-base.bin']
    if supplied.suffix.lower() == '.bin':
        model_candidates.insert(0, supplied)
    elif supplied.is_dir() and supplied != project:
        model_candidates.insert(0, supplied / 'ggml-base.bin')
    model = next((candidate for candidate in model_candidates if candidate.is_file()), None)
    if executable is None:
        raise RuntimeError('Не найден runtime/whisper-cpp/whisper-cli.exe. Запустите setup.ps1.')
    if model is None or '.en.' in model.name.casefold():
        raise RuntimeError('Не найдена многоязычная модель models/whisper-cpp/ggml-base.bin. Запустите setup.ps1.')
    return project, executable, model


def availability(project_dir: Path) -> tuple[bool, str]:
    try:
        import importlib.util
        if importlib.util.find_spec('numpy') is None:
            return False, 'Для многоязычной речи не установлен NumPy. Запустите setup.ps1.'
        resolve_paths(project_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        return False, str(exc)
    return True, 'Локальное многоязычное распознавание whisper.cpp готово. Микрофон выключен.'


class WhisperCppModel:
    """CPU transcriber; microphone capture stays in the caller's voice service."""

    def __init__(self, project_dir_or_model_dir, **options):
        self.project_dir, self.executable, self.model = resolve_paths(Path(project_dir_or_model_dir))
        try:
            self.threads = max(1, min(4, int(options.get('cpu_threads', (os.cpu_count() or 2) - 1))))
        except (TypeError, ValueError):
            self.threads = 2
        try:
            self.timeout = max(5.0, min(60.0, float(options.get('timeout', 60))))
        except (TypeError, ValueError):
            self.timeout = 60.0
        self._closed = threading.Event()
        self._inference_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._process = None

    def transcribe(self, audio, language=None, **ignored):
        import numpy as np
        if self._closed.is_set():
            raise RuntimeError('Распознавание остановлено.')
        requested = 'auto' if language is None else str(language).strip().lower()
        if not re.fullmatch(r'(?:auto|[a-z]{2,3})', requested):
            raise ValueError('Некорректный код языка распознавания.')
        samples = np.asarray(audio, dtype=np.float32)
        if samples.ndim != 1:
            raise ValueError('Whisper принимает один канал аудио с частотой 16 кГц.')
        if not np.isfinite(samples).all():
            raise ValueError('Аудио содержит некорректные значения.')
        duration = len(samples) / 16000
        if duration > 60:
            raise ValueError('Одна запись для распознавания не должна превышать 60 секунд.')
        if not len(samples):
            return [], TranscriptionInfo(requested, 0.0)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype('<i2')
        with self._inference_lock:
            if self._closed.is_set():
                raise RuntimeError('Распознавание остановлено.')
            temp_root = self.project_dir / 'data' / 'temp'
            temp_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='whisper-', dir=temp_root) as temporary:
                temp_dir = Path(temporary)
                audio_path = temp_dir / 'input.wav'
                output_base = temp_dir / 'result'
                with wave.open(str(audio_path), 'wb') as recording:
                    recording.setnchannels(1)
                    recording.setsampwidth(2)
                    recording.setframerate(16000)
                    recording.writeframes(pcm.tobytes())
                command = [str(self.executable), '--model', str(self.model),
                           '--file', str(audio_path), '--language', requested,
                           '--output-json-full', '--output-file', str(output_base),
                           '--no-gpu', '--threads', str(self.threads),
                           '--beam-size', '1', '--best-of', '1', '--temperature', '0',
                           '--no-fallback', '--max-context', '0', '--suppress-nst',
                           '--no-prints', '--no-timestamps']
                process = None
                try:
                    process = subprocess.Popen(
                        command, cwd=str(self.executable.parent), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                    )
                    with self._process_lock:
                        self._process = process
                    deadline = time.monotonic() + self.timeout
                    while True:
                        if self._closed.is_set():
                            raise RuntimeError('Распознавание остановлено.')
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError('Whisper не завершил распознавание за 60 секунд. Попробуйте более короткую фразу.')
                        try:
                            stdout, stderr = process.communicate(timeout=min(.2, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                    if self._closed.is_set():
                        raise RuntimeError('Распознавание остановлено.')
                    if process.returncode:
                        details = (stderr or stdout or b'').decode('utf-8', errors='replace')[-700:].strip()
                        raise RuntimeError('Ошибка локального Whisper: ' + (details or str(process.returncode)))
                    output_path = output_base.with_suffix('.json')
                    if not output_path.is_file():
                        raise RuntimeError('Whisper не создал результат. Проверьте язык и модель.')
                    try:
                        payload = json.loads(output_path.read_text(encoding='utf-8-sig'))
                    except (OSError, ValueError) as exc:
                        raise RuntimeError('Не удалось прочитать результат локального Whisper.') from exc
                    return parse_whisper_json(payload, requested, duration)
                finally:
                    if process is not None and process.poll() is None:
                        process.kill()
                        try:
                            process.communicate(timeout=3)
                        except (subprocess.TimeoutExpired, OSError):
                            pass
                    with self._process_lock:
                        if self._process is process:
                            self._process = None

    def close(self):
        self._closed.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
