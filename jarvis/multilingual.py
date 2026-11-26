"""On-device multilingual microphone recognition using a local Whisper model.

The module itself imports only the standard library. Model and audio packages
are loaded only after the user explicitly enables the microphone.
"""
from __future__ import annotations

from collections import deque
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time


log = logging.getLogger(__name__)
_VOICE_CACHE = None
MODEL_DIRECTORY = "faster-whisper-base"
LANGUAGES = (
    "auto", "ru", "kk", "en", "uk", "uz", "tr", "de", "fr", "es", "it", "pt", "ar", "zh",
    "ja", "ko", "hi", "pl", "nl", "sv", "da", "no", "fi", "cs", "sk", "hu", "ro", "bg",
    "el", "he", "fa", "ur", "id", "ms", "vi", "th", "ta", "te", "bn", "mr", "gu", "pa",
    "ne", "si", "my", "km", "lo", "ka", "hy", "az", "be", "et", "lv", "lt", "sl", "hr",
    "sr", "bs", "mk", "sq", "is", "fo", "cy", "mt", "eu", "ca", "gl", "af", "sw",
    "am", "ha", "yo", "so", "sn", "mg", "ps", "sd", "tg", "tk", "mn", "tt", "ba",
    "sa", "bo", "yi", "la", "oc", "br", "lb", "nn", "ht", "mi", "haw", "su", "jw", "tl",
    "as", "ln", "ml", "kn",
)


def reply_language(text: str, settings: dict, last_language: str | None = None) -> str:
    """Choose an output language without transmitting text for detection."""
    requested = str(settings.get("speech_language", "auto")).lower().split("-")[0]
    if requested != "auto" and requested in LANGUAGES:
        return requested
    lowered = text.lower()
    if re.search("[әғқңұһ]", lowered):
        return "kk"
    if re.search("[їєґ]", lowered):
        return "uk"
    if re.search("[ぁ-ヿ]", lowered):
        return "ja"
    scripts = (
        ("ko", "[가-힣]"), ("zh", "[一-鿿]"), ("he", "[א-ת]"),
        ("el", "[Ͱ-Ͽ]"), ("hi", "[ऀ-ॿ]"), ("bn", "[ঀ-৿]"),
        ("ta", "[஀-௿]"), ("te", "[ఀ-౿]"), ("kn", "[ಀ-೿]"),
        ("ml", "[ഀ-ൿ]"), ("gu", "[઀-૿]"), ("pa", "[਀-੿]"),
        ("th", "[ก-๿]"), ("lo", "[຀-໿]"), ("my", "[က-႟]"),
        ("km", "[ក-៿]"), ("ka", "[ა-ჿ]"), ("hy", "[Ա-֏]"),
        ("am", "[ሀ-፿]"),
    )
    for language, pattern in scripts:
        if re.search(pattern, text):
            return language
    if re.search("[\u0600-\u06ff]", text):
        if re.search("[ٹڈڑںےھ]", text):
            return "ur"
        return "fa" if re.search("[پچژگ]", text) else "ar"
    if re.search("[А-Яа-яЁё]", text):
        if "ў" in lowered:
            return "be"
        if re.search("[ђћљњџ]", lowered):
            return "sr"
        if re.search("[ѓќѕ]", lowered):
            return "mk"
        if "і" in lowered:
            return "kk" if last_language == "kk" else "uk"
        # Statistical guesses on short Cyrillic commands frequently label
        # Russian as Bulgarian. Prefer the recognised input language or Russian.
        return last_language if last_language in ("ru", "kk", "uk", "be", "bg", "sr", "mk", "mn") else "ru"
    if len(re.sub(r"\W", "", text)) >= 12:
        try:
            from langdetect import DetectorFactory, detect
            DetectorFactory.seed = 0
            detected = detect(text).lower().split("-")[0]
            if detected in LANGUAGES:
                return detected
        except (ImportError, ValueError, RuntimeError):
            pass
        except Exception:
            # Language guessing is optional; lack of certainty must not break TTS.
            pass
    if last_language in LANGUAGES and last_language != "auto":
        return last_language
    return "en" if re.search("[A-Za-z]", text) else "ru"


async def edge_voice_for_text(text: str, settings: dict, last_language: str | None,
                              project_dir: Path) -> str:
    """Select an available Microsoft voice, caching its locale catalogue locally."""
    import edge_tts
    global _VOICE_CACHE
    override = str(settings.get("edge_voice", "auto"))
    if override != "auto" and re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)+Neural", override):
        return override
    language = reply_language(text, settings, last_language)
    language = {"jw": "jv", "tl": "fil", "no": "nb"}.get(language, language)
    cache_path = Path(project_dir) / "data" / "edge_voices.json"
    cached = _VOICE_CACHE
    if cached is None:
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("voices"), list):
                cached = loaded
        except (OSError, ValueError):
            pass
    if cached is None or time.time() - float(cached.get("saved_at", 0)) > 86400:
        try:
            voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=10)
            cached = {"saved_at": time.time(), "voices": voices}
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                cache_path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        except Exception:
            if cached is None:
                # Known defaults avoid forcing a Russian voice for other languages
                # when the catalogue endpoint alone is unavailable.
                known = {
                    "ru": "ru-RU-DmitryNeural", "kk": "kk-KZ-DauletNeural",
                    "en": "en-US-GuyNeural", "uk": "uk-UA-OstapNeural",
                    "de": "de-DE-ConradNeural", "fr": "fr-FR-HenriNeural",
                    "es": "es-ES-AlvaroNeural", "it": "it-IT-DiegoNeural",
                    "pt": "pt-BR-AntonioNeural", "tr": "tr-TR-AhmetNeural",
                    "ar": "ar-SA-HamedNeural", "zh": "zh-CN-YunxiNeural",
                    "ja": "ja-JP-KeitaNeural", "ko": "ko-KR-InJoonNeural",
                    "hi": "hi-IN-MadhurNeural", "uz": "uz-UZ-SardorNeural",
                }
                if language in known:
                    return known[language]
                raise RuntimeError(f"Не удалось выбрать сетевой голос для языка {language}")
    _VOICE_CACHE = cached
    preferred = {"en": "en-US", "es": "es-ES", "pt": "pt-BR", "ar": "ar-SA", "zh": "zh-CN"}.get(language)
    candidates = [voice for voice in cached["voices"] if isinstance(voice, dict)
                  and str(voice.get("Locale", "")).lower().startswith(language + "-")
                  and isinstance(voice.get("ShortName"), str)]
    if not candidates:
        raise RuntimeError(f"В каталоге Microsoft нет голоса для языка {language}")
    candidates.sort(key=lambda item: (bool(preferred and item.get("Locale") != preferred),
                                      item.get("Gender") != "Male", item["ShortName"]))
    return candidates[0]["ShortName"]


def _faster_whisper_ready(project_dir: Path) -> bool:
    try:
        if importlib.util.find_spec("faster_whisper") is None:
            return False
    except (ImportError, ValueError):
        return False
    model_path = Path(project_dir) / "models" / MODEL_DIRECTORY
    return all((model_path / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json"))


def load_whisper_model(project_dir: Path):
    """Prefer a ready faster-whisper install, then the wheel-free CPU runtime."""
    if _faster_whisper_ready(project_dir):
        try:
            from faster_whisper import WhisperModel
            return WhisperModel(
                str(Path(project_dir) / "models" / MODEL_DIRECTORY), device="cpu", compute_type="int8", local_files_only=True,
                cpu_threads=max(1, min(4, (os.cpu_count() or 2) - 1)), num_workers=1,
            )
        except (ImportError, OSError, RuntimeError):
            from .whisper_cpp import availability as portable_availability
            if not portable_availability(project_dir)[0]:
                raise
    from .whisper_cpp import WhisperCppModel
    return WhisperCppModel(project_dir)


def availability(project_dir: Path) -> tuple[bool, str]:
    missing = []
    for name in ("sounddevice", "numpy"):
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    if missing:
        return False, "Не установлены модули многоязычной речи: " + ", ".join(missing) + ". Запустите setup.ps1."
    if _faster_whisper_ready(project_dir):
        return True, "Whisper распознаёт поддерживаемые языки на ноутбуке. Микрофон выключен."
    from .whisper_cpp import availability as portable_availability
    portable_ready, details = portable_availability(project_dir)
    if portable_ready:
        return True, "Локальное многоязычное распознавание Whisper · portable CPU готово. Микрофон выключен."
    return False, "Многоязычная речь Whisper недоступна. " + details


def resample_to_16k(audio, source_rate: int):
    """Convert mono float32 audio without requiring SciPy or a network codec."""
    import numpy as np
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if source_rate <= 0:
        raise ValueError("Некорректная частота микрофона")
    if source_rate == 16000 or not len(audio):
        return audio
    count = max(1, round(len(audio) * 16000 / source_rate))
    old_positions = np.arange(len(audio), dtype=np.float64)
    new_positions = np.arange(count, dtype=np.float64) * source_rate / 16000
    return np.interp(new_positions, old_positions, audio).astype(np.float32)


def _current(service, event, session):
    return not event.is_set() and not service._closed.is_set() and session == service._session


def _drain(target):
    while True:
        try:
            target.get_nowait()
        except queue.Empty:
            return


def listen_whisper(service, event: threading.Event, session: int, wake_word: bool, once: bool) -> None:
    """Implement VoiceService listening with the same callbacks as its Vosk path.

    A service-wide lock remains held for the whole listening session. Stopping a
    session invalidates its results immediately; a new session waits for an
    in-flight inference before reusing the same model or opening a microphone.
    """
    from .voice import _queue_latest, split_wake_word

    lock = service._whisper_lock
    acquired = False
    stream = None
    capture_enabled = threading.Event()
    try:
        while _current(service, event, session):
            if lock.acquire(timeout=.1):
                acquired = True
                break
            service._status("processing", "Завершаю предыдущую обработку речи…", session)
        if not acquired or not _current(service, event, session):
            return

        import numpy as np
        import sounddevice as sd

        if service._whisper_model is None:
            service._status("processing", "Загружаю локальную многоязычную модель Whisper…", session)
            service._whisper_model = load_whisper_model(service.project_dir)
            if service._whisper_model.__class__.__name__ == "WhisperCppModel":
                service._status("processing", "Локальный Whisper · portable CPU готов к распознаванию.", session)
        if not _current(service, event, session):
            return
        settings = service._settings()
        device = settings.get("input_device")
        device = int(device) if device is not None else None
        device_info = sd.query_devices(device=device, kind="input")
        sample_rate = int(device_info["default_samplerate"])
        if sample_rate <= 0:
            raise RuntimeError("Микрофон не сообщил частоту записи")
        language = str(settings.get("speech_language", "auto")).strip().lower()
        language = None if language == "auto" else language
        block_size = max(256, round(sample_rate * .05))
        audio_queue = queue.Queue(maxsize=80)

        def callback(indata, frames, timing, status):
            if _current(service, event, session) and capture_enabled.is_set() and not service._muted():
                _queue_latest(audio_queue, bytes(indata))

        stream = sd.RawInputStream(
            samplerate=sample_rate, blocksize=block_size, device=device,
            dtype="int16", channels=1, callback=callback,
        )
        with service._listen_lock:
            if not _current(service, event, session):
                return
            service._stream = stream
        capture_enabled.set()
        stream.start()
        prompt = "Говорите на выбранном языке: до 15 секунд" if once else (
            "Слушаю на поддерживаемых языках. Начните с «Джарвис» / Jarvis" if wake_word else "Слушаю команды"
        )
        service._status("listening", prompt, session)
        deadline = time.monotonic() + 15
        armed_until = 0.0
        speech_epoch = service._speech_epoch
        pre_roll = deque(maxlen=max(1, round(sample_rate * .25 / block_size)))
        recorded = []
        voiced_duration = 0.0
        utterance_duration = 0.0
        silence_duration = 0.0
        noise_floor = .002
        try:
            minimum_rms = float(settings.get("microphone_threshold", .008))
        except (ValueError, TypeError):
            minimum_rms = .008
        minimum_rms = min(.08, max(.002, minimum_rms))

        while _current(service, event, session):
            if service._muted() or speech_epoch != service._speech_epoch:
                recorded.clear()
                pre_roll.clear()
                _drain(audio_queue)
                speech_epoch = service._speech_epoch
                voiced_duration = utterance_duration = silence_duration = 0.0
                if once:
                    deadline = time.monotonic() + 15
                event.wait(.05)
                continue
            if once and time.monotonic() >= deadline and not recorded:
                service._status("idle", "Речь не распознана. Нажмите микрофон и повторите команду.", session)
                return
            try:
                raw = audio_queue.get(timeout=.15)
            except queue.Empty:
                continue
            block = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            duration = len(block) / sample_rate
            rms = float(np.sqrt(np.mean(block * block))) if len(block) else 0.0
            threshold = max(minimum_rms, min(.05, noise_floor * 3.0))
            voiced = rms >= threshold
            if not recorded and not voiced:
                noise_floor = .97 * noise_floor + .03 * min(rms, minimum_rms)
                pre_roll.append(block)
                continue
            if voiced and not recorded:
                recorded.extend(pre_roll)
                pre_roll.clear()
            recorded.append(block)
            utterance_duration += duration
            if voiced:
                voiced_duration += duration
                silence_duration = 0.0
            else:
                silence_duration += duration
            if silence_duration < .8 and utterance_duration < 12:
                continue

            capture_enabled.clear()
            frames = recorded
            recorded = []
            accepted_audio = voiced_duration >= .25
            voiced_duration = utterance_duration = silence_duration = 0.0
            if not accepted_audio:
                _drain(audio_queue)
                capture_enabled.set()
                continue
            service._status("processing", "Распознаю речь локально…", session)
            audio = resample_to_16k(np.concatenate(frames), sample_rate)
            transcription_epoch = service._speech_epoch
            segments, info = service._whisper_model.transcribe(
                audio, language=language, beam_size=1, best_of=1, temperature=0,
                condition_on_previous_text=False, vad_filter=False,
                no_speech_threshold=.6, log_prob_threshold=-1.0,
            )
            parts = []
            for segment in segments:
                if not _current(service, event, session):
                    return
                if getattr(segment, "no_speech_prob", 0) > .8 and getattr(segment, "avg_logprob", 0) < -1:
                    continue
                parts.append(segment.text.strip())
            text = " ".join(parts).strip()
            service._last_language = getattr(info, "language", None)
            _drain(audio_queue)
            if not _current(service, event, session):
                return
            if service._muted() or transcription_epoch != service._speech_epoch:
                capture_enabled.set()
                continue
            capture_enabled.set()
            if not text:
                service._status("listening", "Не расслышал. Повторите фразу ближе к микрофону.", session)
                continue
            invoked, command = split_wake_word(text)
            if wake_word:
                if invoked:
                    if not command:
                        armed_until = time.monotonic() + 8
                        service._status("listening", "Я здесь. Скажите команду.", session)
                        continue
                    text = command
                    armed_until = 0.0
                elif time.monotonic() < armed_until:
                    armed_until = 0.0
                else:
                    service._status("listening", prompt, session)
                    continue
            elif invoked:
                text = command
                if not text:
                    continue
            if not _current(service, event, session):
                return
            service._status("processing", "Распознано: " + text, session)
            try:
                service.on_text(text)
            except Exception:
                log.exception("Multilingual recognized text callback failed")
            if once:
                service._status("idle", "Команда распознана. Микрофон выключен.", session)
                return
            service._status("listening", prompt, session)
    except Exception as exc:
        if _current(service, event, session):
            log.exception("Multilingual microphone recognition failed")
            service._status("error", "Многоязычная речь недоступна: " + str(exc)[:200]
                            + ". Проверьте модель, язык и микрофон в настройках.", session)
    finally:
        capture_enabled.clear()
        if stream is not None:
            try:
                stream.close(ignore_errors=True)
            except Exception:
                pass
        with service._listen_lock:
            if session == service._session:
                service._stream = None
        if acquired:
            lock.release()
