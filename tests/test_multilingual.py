"""Multilingual model routing, audio lifecycle, and voice choice without devices."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.multilingual import availability, edge_voice_for_text, reply_language, resample_to_16k
from jarvis.voice import VoiceService, split_wake_word

TEST_TEMP = Path(__file__).resolve().parent / ".multilingual-temp"
TEST_TEMP.mkdir(exist_ok=True)


class AudioConversionTests(unittest.TestCase):
    def test_downsample_keeps_duration_and_mono_float32(self):
        audio = np.sin(np.arange(48000) * 2 * np.pi * 440 / 48000).astype(np.float32)
        converted = resample_to_16k(audio, 48000)
        self.assertEqual(converted.shape, (16000,))
        self.assertEqual(converted.dtype, np.float32)
        self.assertAlmostEqual(float(converted.max()), 1.0, places=3)

    def test_native_rate_and_empty_audio(self):
        audio = np.array([.1, -.1], dtype=np.float32)
        np.testing.assert_array_equal(resample_to_16k(audio, 16000), audio)
        self.assertEqual(len(resample_to_16k([], 48000)), 0)

    def test_invalid_rate_fails(self):
        with self.assertRaises(ValueError):
            resample_to_16k([0], 0)


class LanguageVoiceTests(unittest.TestCase):
    def test_wake_word_is_recognized_across_scripts(self):
        for phrase, expected in (("Jarvis, open notepad", "open notepad"),
                                 ("贾维斯打开记事本", "打开记事本"),
                                 ("자비스 메모장 열기", "메모장 열기"),
                                 ("جارفيس افتح المتصفح", "افتح المتصفح")):
            self.assertEqual(split_wake_word(phrase), (True, expected))

    def test_unicode_script_detection_and_fixed_language(self):
        for text, expected in (("Сәлем, досым", "kk"), ("こんにちは", "ja"),
                               ("你好", "zh"), ("안녕하세요", "ko"),
                               ("مرحبا", "ar"), ("नमस्ते", "hi")):
            self.assertEqual(reply_language(text, {}), expected)
        self.assertEqual(reply_language("Привет", {"speech_language": "en"}), "en")
        self.assertEqual(reply_language("Джарвис готов к работе. Открой блокнот.", {}), "ru")

    def test_voice_catalogue_selects_target_language_and_caches(self):
        calls = []
        async def list_voices():
            calls.append(True)
            return [
                {"ShortName": "ru-RU-DmitryNeural", "Locale": "ru-RU", "Gender": "Male"},
                {"ShortName": "en-US-JennyNeural", "Locale": "en-US", "Gender": "Female"},
                {"ShortName": "en-US-GuyNeural", "Locale": "en-US", "Gender": "Male"},
                {"ShortName": "kk-KZ-DauletNeural", "Locale": "kk-KZ", "Gender": "Male"},
            ]
        async def immediate_timeout(awaitable, timeout):
            return await awaitable
        def complete(coroutine):
            # Every awaited operation is already mocked. Driving it directly
            # avoids creating Windows socket pairs in restricted test sandboxes.
            try:
                coroutine.send(None)
            except StopIteration as result:
                return result.value
            finally:
                coroutine.close()
            self.fail("Mocked voice selection unexpectedly waited for external I/O")
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory, \
                patch.dict(sys.modules, {"edge_tts": types.SimpleNamespace(list_voices=list_voices)}), \
                patch("jarvis.multilingual._VOICE_CACHE", None), \
                patch("jarvis.multilingual.asyncio.wait_for", immediate_timeout):
            root = Path(directory)
            chosen = complete(edge_voice_for_text("Hello", {"speech_language": "en"}, "ru", root))
            self.assertEqual(chosen, "en-US-GuyNeural")
            chosen = complete(edge_voice_for_text("Сәлем", {"speech_language": "kk"}, None, root))
            self.assertEqual(chosen, "kk-KZ-DauletNeural")
            self.assertEqual(len(calls), 1)


class WhisperLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP)
        self.project = Path(self.temp.name)
        self.received = []
        self.statuses = []
        self.voice = VoiceService(self.project, self.received.append,
                                  lambda state, message: self.statuses.append((state, message)))

    def tearDown(self):
        self.voice.close()
        self.temp.cleanup()

    def test_whisper_is_default_and_missing_model_does_not_open_microphone(self):
        with patch("jarvis.multilingual.importlib.util.find_spec", return_value=object()):
            self.voice.listen_once()
        self.assertIsNone(self.voice._listen_thread)
        self.assertIsNone(self.voice._stream)
        self.assertEqual(self.statuses[-1][0], "error")
        self.assertIn("Whisper", self.statuses[-1][1])

    def _recognize(self, text="Open notepad", stale=False, portable=False):
        captured = []
        streams = []
        voice = self.voice
        class Model:
            def transcribe(self, audio, **kwargs):
                captured.append((audio, kwargs))
                if stale:
                    voice._session += 1
                segment = types.SimpleNamespace(text=text, no_speech_prob=.01, avg_logprob=-.1)
                return iter([segment]), types.SimpleNamespace(language="en")
        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                self.closed = False
                streams.append(self)
            def start(self):
                # More than .25 s of speech followed by .8 s silence.
                for amplitude, blocks in ((.1, 12), (0, 18)):
                    data = np.full(800, amplitude * 32767, dtype=np.int16).tobytes()
                    for _ in range(blocks):
                        self.callback(data, 800, None, None)
            def close(self, **kwargs):
                self.closed = True
            def abort(self, **kwargs):
                pass
        modules = {
            "sounddevice": types.SimpleNamespace(query_devices=lambda **kwargs: {"default_samplerate": 16000},
                                                  RawInputStream=Stream),
            "faster_whisper": types.SimpleNamespace(WhisperModel=lambda *a, **kw: Model()),
        }
        self.voice._session = 1
        event = threading.Event()
        with patch.dict(sys.modules, modules), patch.object(self.voice, "_settings", return_value={}), \
                patch("jarvis.multilingual._faster_whisper_ready", return_value=not portable), \
                patch("jarvis.whisper_cpp.WhisperCppModel", side_effect=lambda *a, **kw: Model()) as portable_factory:
            self.voice._listen(event, 1, wake_word=False, once=True)
        if portable:
            portable_factory.assert_called_once_with(self.project)
        return captured, streams

    def test_one_shot_auto_detects_language_and_closes_stream(self):
        captured, streams = self._recognize()
        self.assertEqual(self.received, ["Open notepad"])
        self.assertIsNone(captured[0][1]["language"])
        self.assertEqual(captured[0][0].dtype, np.float32)
        self.assertEqual(self.voice._last_language, "en")
        self.assertTrue(streams[0].closed)
        self.assertIsNone(self.voice._stream)
        self.assertFalse(self.voice._whisper_lock.locked())

    def test_portable_backend_dispatches_without_faster_whisper_assets(self):
        captured, streams = self._recognize(text="Сәлем досым", portable=True)
        self.assertEqual(self.received, ["Сәлем досым"])
        self.assertEqual(len(captured), 1)
        self.assertTrue(streams[0].closed)
        self.assertFalse(self.voice._whisper_lock.locked())

    def test_stopped_or_replaced_session_never_dispatches_inference_result(self):
        _, streams = self._recognize(stale=True)
        self.assertEqual(self.received, [])
        self.assertTrue(streams[0].closed)
        self.assertFalse(self.voice._whisper_lock.locked())

    def test_model_is_reused_across_microphone_sessions(self):
        self._recognize()
        loaded_model = self.voice._whisper_model
        self._recognize()
        self.assertIs(self.voice._whisper_model, loaded_model)
        self.assertEqual(len(self.received), 2)


if __name__ == "__main__":
    unittest.main()
