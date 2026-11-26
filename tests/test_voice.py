"""Speech lifecycle and invocation tests; no microphone or network required."""
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from jarvis.voice import VoiceService, _queue_latest, split_wake_word


class WakeWordTests(unittest.TestCase):
    def test_supported_invocations(self):
        for word in ("Джарвис", "джервис", "jarvis"):
            self.assertEqual(split_wake_word(word + ", открой блокнот"), (True, "открой блокнот"))

    def test_invocation_without_command_arms_followup(self):
        self.assertEqual(split_wake_word("эй джарвис"), (True, ""))

    def test_not_a_word_fragment(self):
        self.assertEqual(split_wake_word("джарвисовый файл"), (False, "джарвисовый файл"))

    def test_audio_queue_drops_oldest(self):
        target = queue.Queue(maxsize=2)
        _queue_latest(target, b"old")
        _queue_latest(target, b"recent")
        _queue_latest(target, b"new")
        self.assertEqual(target.qsize(), 2)
        self.assertEqual(target.get_nowait(), b"recent")
        self.assertEqual(target.get_nowait(), b"new")


class VoiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parent / ".voice-temp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.project = Path(self.temp.name)
        self.statuses = []
        self.voice = VoiceService(self.project, lambda text: None, lambda state, message: self.statuses.append((state, message)))

    def tearDown(self):
        self.voice.close()
        self.temp.cleanup()

    def test_construction_does_not_start_workers(self):
        self.assertIsNone(self.voice._listen_thread)
        self.assertIsNone(self.voice._speech_thread)
        self.assertIsNone(self.voice._stream)

    def test_missing_model_returns_error_without_mic(self):
        with patch("jarvis.voice.importlib.util.find_spec", return_value=object()):
            self.voice.listen_once()
        self.assertIsNone(self.voice._listen_thread)
        self.assertEqual(self.statuses[-1][0], "error")

    def test_muted_on_tts_and_tail(self):
        self.voice._speaking.set()
        self.assertTrue(self.voice._muted())
        self.voice._speaking.clear()
        self.voice._mute_until = float("inf")
        self.assertTrue(self.voice._muted())

    def test_stale_listen_status_is_ignored(self):
        self.voice._session = 4
        self.voice._status("listening", "old", session=3)
        self.assertEqual(self.statuses, [])

    def test_disabled_output_does_not_start_tts(self):
        (self.project / "settings.json").write_text(json.dumps({"voice_output": False}), encoding="utf-8")
        self.voice.speak("Привет")
        self.assertIsNone(self.voice._speech_thread)

    def test_closed_service_does_not_start_workers(self):
        self.voice.close()
        self.voice.start()
        self.voice.speak("Привет")
        self.assertIsNone(self.voice._listen_thread)
        self.assertIsNone(self.voice._speech_thread)

    def test_close_cancels_owned_portable_model(self):
        calls = []
        self.voice._whisper_model = types.SimpleNamespace(close=lambda: calls.append("closed"))
        self.voice.close()
        self.assertEqual(calls, ["closed"])

    def _run_fake_recognition(self, phrases, wake_word=True, once=False, timeout=False):
        """Exercise the real listener without an OS stream or speech model."""
        received = []
        event = threading.Event()

        def on_text(text):
            received.append(text)
            event.set()

        class Recognizer:
            def __init__(self, model, rate):
                self.current = b""

            def AcceptWaveform(self, block):
                self.current = block
                return True

            def Result(self):
                return json.dumps({"text": self.current.decode("utf-8")})

            def Reset(self):
                pass

        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def start(self):
                for phrase in phrases:
                    self.callback(phrase.encode("utf-8"), 1, None, None)

            def close(self, **kwargs):
                pass

        modules = {
            "sounddevice": types.SimpleNamespace(
                query_devices=lambda **kwargs: {"default_samplerate": 16000}, RawInputStream=Stream,
            ),
            "vosk": types.SimpleNamespace(KaldiRecognizer=Recognizer),
        }
        self.voice.on_text = on_text
        self.voice._session = 1
        with patch.dict(sys.modules, modules), \
                patch.object(self.voice, "_settings", return_value={"speech_backend": "vosk"}), \
                patch.object(self.voice, "_load_model", return_value=object()):
            if timeout:
                with patch("jarvis.voice.time.monotonic", side_effect=[100.0, 116.0]):
                    self.voice._listen(event, 1, wake_word, once)
            else:
                self.voice._listen(event, 1, wake_word, once)
        return received

    def test_wake_mode_ignores_uninvoked_background_speech(self):
        received = self._run_fake_recognition(["открой браузер", "джарвис открой блокнот"])
        self.assertEqual(received, ["открой блокнот"])

    def test_wake_word_alone_accepts_following_command(self):
        received = self._run_fake_recognition(["джарвис", "открой блокнот"])
        self.assertEqual(received, ["открой блокнот"])

    def test_one_shot_needs_no_wake_word(self):
        received = self._run_fake_recognition(["открой блокнот"], wake_word=False, once=True)
        self.assertEqual(received, ["открой блокнот"])

    def test_one_shot_silence_times_out_and_closes_stream(self):
        received = self._run_fake_recognition([], wake_word=False, once=True, timeout=True)
        self.assertEqual(received, [])
        self.assertIsNone(self.voice._stream)
        self.assertIn("Речь не распознана", self.statuses[-1][1])


if __name__ == "__main__":
    unittest.main()
