# Parsec KZ | JARVIS Experiment 001 | See COPYRIGHT.md
"""Offline Russian speech recognition and optional spoken replies.

Vosk and sounddevice are imported lazily. Importing this module never opens a
microphone, loads a speech model, or contacts an online service.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import importlib.util
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid


log = logging.getLogger(__name__)
WAKE_PATTERN = re.compile(
    r"(?:(?:^|[\s,.:!?—-])(?:джарвис|джервис|джарвес|джарвіс|жарвис|jarvis|"
    r"جارفيس|جارڤيس|جارویس|जार्विस)(?=$|[\s,.:!?—-])|"
    r"(?:贾维斯|賈維斯|ジャービス|ジャーヴィス|자비스))", re.I,
)


def split_wake_word(text: str) -> tuple[bool, str]:
    """Return whether the invocation occurs, and the command following it."""
    text = str(text).strip()
    match = WAKE_PATTERN.search(text)
    if not match:
        return False, text
    return True, text[match.end():].strip(" ,.:!?—-\t\r\n")


def _queue_latest(target: queue.Queue, value) -> None:
    """Keep the callback nonblocking even when recognition is slower than input."""
    try:
        target.put_nowait(value)
    except queue.Full:
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(value)
        except queue.Full:
            pass


class VoiceService:
    def __init__(self, project_dir: Path, on_text, on_status, is_speaking=None):
        self.project_dir = Path(project_dir)
        self.model_path = self.project_dir / "models" / "vosk-model-small-ru-0.22"
        self.on_text = on_text
        self.on_status = on_status
        self.external_is_speaking = is_speaking
        self._closed = threading.Event()
        self._speaking = threading.Event()
        self._mute_until = 0.0
        self._speech_epoch = 0
        self._listen_lock = threading.RLock()
        self._model_lock = threading.Lock()
        self._model = None
        self._whisper_lock = threading.Lock()
        self._whisper_model = None
        self._last_language = None
        self._listen_thread = None
        self._stop_event = threading.Event()
        self._session = 0
        self._stream = None
        self._speech_queue = queue.Queue(maxsize=3)
        self._speech_thread = None
        self._speech_lock = threading.Lock()
        self._tts_process = None
        self._mci_alias = None

    def _settings(self) -> dict:
        try:
            value = json.loads((self.project_dir / "settings.json").read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _status(self, state: str, message: str, session: int | None = None) -> None:
        if self._closed.is_set() or (session is not None and session != self._session):
            return
        try:
            self.on_status(state, message)
        except Exception:
            log.exception("Voice status callback failed")

    def available(self) -> tuple[bool, str]:
        if self._settings().get("speech_backend", "whisper") == "whisper":
            from .multilingual import availability
            return availability(self.project_dir)
        missing = []
        for name in ("vosk", "sounddevice"):
            try:
                if importlib.util.find_spec(name) is None:
                    missing.append(name)
            except (ImportError, ValueError):
                missing.append(name)
        if missing:
            return False, "Не установлены голосовые модули: " + ", ".join(missing) + ". Запустите setup.ps1."
        if not (self.model_path / "conf" / "model.conf").is_file():
            return False, "Не найдена русская модель речи. Запустите setup.ps1 для её загрузки."
        return True, "Русская речь распознаётся локально; микрофон включается по кнопке."

    def start(self, wake_word: bool = True) -> None:
        self._begin(bool(wake_word), once=False)

    def listen_once(self) -> None:
        self._begin(wake_word=False, once=True)

    def _begin(self, wake_word: bool, once: bool) -> None:
        if self._closed.is_set():
            return
        ready, message = self.available()
        if not ready:
            self._status("error", message)
            return
        self.stop()
        with self._listen_lock:
            self._session += 1
            session = self._session
            event = threading.Event()
            self._stop_event = event
            self._listen_thread = threading.Thread(
                target=self._listen, args=(event, session, wake_word, once),
                name="jarvis-microphone", daemon=True,
            )
            self._listen_thread.start()

    def stop(self) -> None:
        with self._listen_lock:
            self._session += 1
            self._stop_event.set()
            stream = self._stream
            self._stream = None
            worker = self._listen_thread
        if stream is not None:
            try:
                stream.abort(ignore_errors=True)
            except Exception:
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=0.3)
        self._status("idle", "Микрофон выключен")

    def _muted(self) -> bool:
        if self._speaking.is_set() or time.monotonic() < self._mute_until:
            return True
        try:
            return bool(self.external_is_speaking and self.external_is_speaking())
        except Exception:
            return False

    def _load_model(self):
        with self._model_lock:
            if self._model is None:
                import vosk
                vosk.SetLogLevel(-1)
                self._model = vosk.Model(str(self.model_path))
            return self._model

    def _listen(self, event: threading.Event, session: int, wake_word: bool, once: bool) -> None:
        if self._settings().get("speech_backend", "whisper") == "whisper":
            from .multilingual import listen_whisper
            listen_whisper(self, event, session, wake_word, once)
            return
        stream = None
        try:
            import sounddevice as sd
            import vosk

            self._status("processing", "Загружаю русскую модель речи…", session)
            model = self._load_model()
            if event.is_set() or self._closed.is_set():
                return
            device = self._settings().get("input_device")
            if device is not None:
                device = int(device)
            device_info = sd.query_devices(device=device, kind="input")
            sample_rate = int(device_info["default_samplerate"])
            if sample_rate <= 0:
                raise RuntimeError("Микрофон не сообщил частоту записи")
            recognizer = vosk.KaldiRecognizer(model, sample_rate)
            audio_queue = queue.Queue(maxsize=12)

            def callback(indata, frames, timing, status):
                if event.is_set() or self._closed.is_set() or self._muted():
                    return
                _queue_latest(audio_queue, bytes(indata))

            stream = sd.RawInputStream(
                samplerate=sample_rate, blocksize=max(800, sample_rate // 5),
                device=device, dtype="int16", channels=1, callback=callback,
            )
            with self._listen_lock:
                if event.is_set() or session != self._session:
                    return
                self._stream = stream
            stream.start()
            prompt = "Скажите команду, у вас 15 секунд" if once else (
                "Слушаю. Начните фразу со слова «Джарвис»" if wake_word else "Слушаю команды"
            )
            self._status("listening", prompt, session)
            deadline = time.monotonic() + 15.0
            armed_until = 0.0
            speech_epoch = self._speech_epoch
            while not event.is_set() and not self._closed.is_set():
                if once and time.monotonic() >= deadline:
                    self._status("idle", "Речь не распознана. Нажмите микрофон и повторите команду.", session)
                    break
                try:
                    block = audio_queue.get(timeout=0.15)
                except queue.Empty:
                    continue
                if self._muted() or speech_epoch != self._speech_epoch:
                    recognizer.Reset()
                    speech_epoch = self._speech_epoch
                    while True:
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            break
                    continue
                if not recognizer.AcceptWaveform(block):
                    continue
                text = json.loads(recognizer.Result()).get("text", "").strip()
                if not text:
                    continue
                if wake_word:
                    invoked, command = split_wake_word(text)
                    if invoked:
                        if not command:
                            armed_until = time.monotonic() + 8.0
                            self._status("listening", "Я здесь. Скажите команду.", session)
                            continue
                        text = command
                        armed_until = 0.0
                    elif time.monotonic() < armed_until:
                        armed_until = 0.0
                    else:
                        continue
                else:
                    invoked, stripped = split_wake_word(text)
                    if invoked:
                        text = stripped
                        if not text:
                            continue
                if event.is_set() or session != self._session:
                    break
                self._status("processing", "Распознано: " + text, session)
                try:
                    self.on_text(text)
                except Exception:
                    log.exception("Recognized text callback failed")
                if once:
                    self._status("idle", "Команда распознана. Микрофон выключен.", session)
                    break
                self._status("listening", prompt, session)
        except Exception as exc:
            if not event.is_set() and not self._closed.is_set():
                log.exception("Speech recognition failed")
                self._status(
                    "error", "Не удалось включить голос: " + str(exc)[:200]
                    + ". Проверьте микрофон в настройках Windows и выбранное устройство.", session,
                )
        finally:
            if stream is not None:
                try:
                    stream.close(ignore_errors=True)
                except Exception:
                    pass
            with self._listen_lock:
                if session == self._session:
                    self._stream = None

    def speak(self, text: str) -> None:
        if self._closed.is_set() or not self._settings().get("voice_output", True):
            return
        text = re.sub(r"```.*?```", "", str(text), flags=re.S)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"[*#_`]+", "", text).strip()
        if not text:
            return
        text = text[:650]
        _queue_latest(self._speech_queue, text)
        with self._speech_lock:
            if self._speech_thread is None or not self._speech_thread.is_alive():
                self._speech_thread = threading.Thread(target=self._speech_worker, name="jarvis-speaker", daemon=True)
                self._speech_thread.start()

    def _speech_worker(self) -> None:
        while not self._closed.is_set():
            try:
                text = self._speech_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            if self._closed.is_set():
                break
            settings = self._settings()
            if not settings.get("voice_output", True):
                continue
            self._speaking.set()
            self._speech_epoch += 1
            self._status("speaking", "Озвучиваю ответ…")
            try:
                if settings.get("voice_engine", "windows") == "edge":
                    try:
                        self._speak_edge(text, settings)
                    except Exception:
                        if not self._closed.is_set():
                            log.warning("Online voice unavailable; falling back to Windows", exc_info=True)
                            self._status("error", "Сетевой голос недоступен. Использую установленный голос Windows.")
                            self._speak_windows(text)
                else:
                    self._speak_windows(text)
            except Exception as exc:
                if not self._closed.is_set():
                    log.exception("Text to speech failed")
                    self._status("error", "Озвучивание недоступно: " + str(exc)[:160])
            finally:
                self._mute_until = time.monotonic() + 0.8
                self._speech_epoch += 1
                self._speaking.clear()
                if self._stream is not None:
                    self._status("listening", "Слушаю. Дождитесь окончания ответа перед новой командой.")
                else:
                    self._status("idle", "Готов к командам")

    def _speak_windows(self, text: str, output_path: Path | None = None) -> None:
        """Speak with an installed OneCore/SAPI voice, or render a local WAV.

        OneCore tokens are opened directly. No voice registration, registry
        copying, or system-wide configuration changes are needed.
        """
        if os.name != "nt":
            raise RuntimeError("Системная озвучка доступна только в Windows")
        # Text is transferred as base64 data, never interpolated as PowerShell.
        encoded_text = base64.b64encode(text.encode("utf-8")).decode("ascii")
        from .multilingual import reply_language
        language = reply_language(text, self._settings(), self._last_language)
        if not re.fullmatch(r"[a-z]{2,3}", language):
            language = "en"
        if output_path is not None:
            output_path = Path(output_path).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
        encoded_output = base64.b64encode(str(output_path or "").encode("utf-8")).decode("ascii")
        script = r"""
$ErrorActionPreference = 'Stop'
$targetLanguage = '__LANG__'
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__TEXT__'))
$outputPath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__OUTPUT__'))
$speaker = $null
$fileStream = $null
$dotnetSpeaker = $null
$choices = @()
try {
    $speaker = New-Object -ComObject SAPI.SpVoice
    $registryRoots = @(
        'HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens',
        'HKCU:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens',
        'HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens',
        'HKCU:\SOFTWARE\Microsoft\Speech\Voices\Tokens'
    )
    foreach ($registryRoot in $registryRoots) {
        foreach ($key in Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue) {
            $candidate = $null
            try {
                $candidate = New-Object -ComObject SAPI.SpObjectToken
                $candidate.SetId($key.Name)
                $matchesLanguage = $false
                foreach ($languageHex in ($candidate.GetAttribute('Language') -split ';')) {
                    $culture = [Globalization.CultureInfo]::GetCultureInfo([Convert]::ToInt32($languageHex, 16))
                    if ($culture.TwoLetterISOLanguageName -eq $targetLanguage) { $matchesLanguage = $true }
                }
                if ($matchesLanguage) {
                    $description = $candidate.GetDescription()
                    $rank = if ($candidate.GetAttribute('Gender') -eq 'Male') { 0 } else { 1 }
                    if ($targetLanguage -eq 'ru' -and $description -like '*Pavel*') { $rank = -1 }
                    $choices += [pscustomobject]@{Token=$candidate; Rank=$rank; Name=$description}
                    $candidate = $null
                }
            } catch {
                # An unrelated or unavailable installed token is skipped.
            } finally {
                if ($null -ne $candidate) { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($candidate) }
            }
        }
    }
    $selected = $false
    foreach ($choice in ($choices | Sort-Object Rank, Name)) {
        try { $speaker.Voice = $choice.Token; $selected = $true; break } catch {}
    }
    if (-not $selected) { throw "No installed voice for language $targetLanguage" }
    $speaker.Rate = 1
    if ($outputPath) {
        $fileStream = New-Object -ComObject SAPI.SpFileStream
        $fileStream.Format.Type = 18
        $fileStream.Open($outputPath, 3, $false)
        $speaker.AudioOutputStream = $fileStream
    }
    $null = $speaker.Speak($text)
} catch {
    if ($null -ne $fileStream) { try { $fileStream.Close() } catch {}; $fileStream = $null }
    # Older systems may only expose voices through System.Speech.
    Add-Type -AssemblyName System.Speech
    $dotnetSpeaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $dotnetVoice = $dotnetSpeaker.GetInstalledVoices() |
        Where-Object {$_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq $targetLanguage} |
        Sort-Object @{Expression={if ($_.VoiceInfo.Gender -eq 'Male') {0} else {1}}} |
        Select-Object -First 1
    if (-not $dotnetVoice) { throw "No installed Windows voice for language $targetLanguage" }
    $dotnetSpeaker.SelectVoice($dotnetVoice.VoiceInfo.Name)
    $dotnetSpeaker.Rate = 1
    if ($outputPath) { $dotnetSpeaker.SetOutputToWaveFile($outputPath) }
    $dotnetSpeaker.Speak($text)
} finally {
    if ($null -ne $fileStream) { try { $fileStream.Close() } catch {} }
    if ($null -ne $dotnetSpeaker) { $dotnetSpeaker.Dispose() }
    foreach ($choice in $choices) {
        try { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($choice.Token) } catch {}
    }
    if ($null -ne $speaker) { try { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($speaker) } catch {} }
}
""".replace("__LANG__", language).replace("__TEXT__", encoded_text).replace("__OUTPUT__", encoded_output)
        command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup,
        )
        self._tts_process = process
        try:
            deadline = time.monotonic() + 70.0
            while process.poll() is None:
                if self._closed.wait(0.1) or time.monotonic() > deadline:
                    process.terminate()
                    process.wait(timeout=3)
                    return
            if process.returncode:
                raise RuntimeError(f"Windows не смог озвучить ответ на языке {language}. Установите соответствующий голос Windows или выберите edge.")
        finally:
            self._tts_process = None

    def _speak_edge(self, text: str, settings: dict) -> None:
        if os.name != "nt":
            raise RuntimeError("Воспроизведение голоса поддерживается только в Windows")
        import edge_tts

        audio_dir = self.project_dir / "data" / "voice_cache"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / ("voice_" + uuid.uuid4().hex + ".mp3")
        async def save():
            from .multilingual import edge_voice_for_text
            voice_name = await edge_voice_for_text(text, settings, self._last_language, self.project_dir)
            await asyncio.wait_for(edge_tts.Communicate(text, voice=voice_name, rate="+5%").save(str(audio_path)), timeout=16)

        alias = "jarvis_" + uuid.uuid4().hex
        try:
            asyncio.run(save())
            if self._closed.is_set():
                return
            winmm = ctypes.windll.winmm
            winmm.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            winmm.mciSendStringW.restype = ctypes.c_uint
            error = winmm.mciSendStringW(f'open "{audio_path}" type mpegvideo alias {alias}', None, 0, None)
            if error:
                raise RuntimeError("Windows не смог открыть звуковой ответ")
            self._mci_alias = alias
            if winmm.mciSendStringW(f"play {alias}", None, 0, None):
                raise RuntimeError("Windows не смог воспроизвести звуковой ответ")
            deadline = time.monotonic() + 70
            while not self._closed.wait(0.1) and time.monotonic() < deadline:
                status = ctypes.create_unicode_buffer(32)
                if winmm.mciSendStringW(f"status {alias} mode", status, len(status), None):
                    break
                if status.value != "playing":
                    break
        finally:
            try:
                ctypes.windll.winmm.mciSendStringW(f"close {alias}", None, 0, None)
            except Exception:
                pass
            self._mci_alias = None
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                pass

    def close(self) -> None:
        self._closed.set()
        self.stop()
        close_model = getattr(self._whisper_model, "close", None)
        if callable(close_model):
            try:
                close_model()
            except Exception:
                log.warning("Unable to stop portable speech model", exc_info=True)
        while True:
            try:
                self._speech_queue.get_nowait()
            except queue.Empty:
                break
        process = self._tts_process
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass
        alias = self._mci_alias
        if alias and os.name == "nt":
            try:
                ctypes.windll.winmm.mciSendStringW(f"stop {alias}", None, 0, None)
            except Exception:
                pass
