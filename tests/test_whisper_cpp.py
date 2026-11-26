from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import wave

from jarvis.whisper_cpp import WhisperCppModel, parse_whisper_json, resolve_paths


class WhisperCppTests(unittest.TestCase):
    def test_multilingual_json_preserves_unicode_and_time_offsets(self):
        segments, info = parse_whisper_json({
            'result': {'language': 'kk'},
            'transcription': [{'text': ' Сәлем, Джарвис! ', 'offsets': {'from': 100, 'to': 1800},
                               'tokens': [{'p': .8}, {'p': .9}]}],
        }, duration=2.0)
        self.assertEqual(info.language, 'kk')
        self.assertEqual(info.duration, 2)
        self.assertEqual(segments[0].text, 'Сәлем, Джарвис!')
        self.assertEqual(segments[0].start, .1)
        self.assertEqual(segments[0].end, 1.8)
        self.assertLess(segments[0].avg_logprob, 0)

    def test_missing_language_falls_back_and_noise_tokens_are_not_commands(self):
        segments, info = parse_whisper_json({'transcription': [{'text': '[BLANK_AUDIO]'},
                                                                      {'text': 'Hello'},
                                                                      {'text': '<|nospeech|>'}]}, 'en')
        self.assertEqual([segment.text for segment in segments], ['Hello'])
        self.assertEqual(info.language, 'en')

    def test_invalid_output_is_rejected(self):
        for payload in [None, [], {}, {'transcription': 'text'}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_whisper_json(payload)

    def test_portable_layout_including_release_subdirectory_is_discovered(self):
        scratch = Path(__file__).resolve().parent / '.engine-temp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='whisper-layout-', dir=scratch) as temporary:
            project = Path(temporary)
            executable = project / 'runtime' / 'whisper-cpp' / 'Release' / 'whisper-cli.exe'
            executable.parent.mkdir(parents=True)
            executable.touch()
            model = project / 'models' / 'whisper-cpp' / 'ggml-base.bin'
            model.parent.mkdir(parents=True)
            model.touch()
            self.assertEqual(resolve_paths(project), (project.resolve(), executable, model))
            self.assertEqual(resolve_paths(model.parent), (project.resolve(), executable, model))

    def test_adapter_writes_valid_pcm_and_cleans_all_recording_files(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('NumPy is not installed')
        scratch = Path(__file__).resolve().parent / '.engine-temp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='whisper-call-', dir=scratch) as temporary:
            project = Path(temporary)
            executable = project / 'runtime' / 'whisper-cpp' / 'whisper-cli.exe'
            executable.parent.mkdir(parents=True)
            executable.touch()
            model_path = project / 'models' / 'whisper-cpp' / 'ggml-base.bin'
            model_path.parent.mkdir(parents=True)
            model_path.touch()

            class FakeProcess:
                returncode = 0
                def communicate(self, timeout=None):
                    return b'', b''
                def poll(self):
                    return 0

            def fake_popen(arguments, **options):
                self.assertNotIn('shell', options)
                audio_file = arguments[arguments.index('--file') + 1]
                with wave.open(audio_file, 'rb') as recording:
                    self.assertEqual(recording.getframerate(), 16000)
                    self.assertEqual(recording.getsampwidth(), 2)
                    self.assertEqual(recording.getnchannels(), 1)
                    samples = np.frombuffer(recording.readframes(4), dtype='<i2')
                    self.assertEqual(samples.tolist(), [0, 32767, -32767, 16383])
                output_base = Path(arguments[arguments.index('--output-file') + 1])
                output_base.with_suffix('.json').write_text(json.dumps({'result': {'language': 'ru'},
                    'transcription': [{'text': 'Привет'}]}, ensure_ascii=False), encoding='utf-8')
                return FakeProcess()

            adapter = WhisperCppModel(project)
            with patch('jarvis.whisper_cpp.subprocess.Popen', side_effect=fake_popen):
                segments, info = adapter.transcribe(np.array([0, 2, -2, .5], dtype=np.float32))
            self.assertEqual(segments[0].text, 'Привет')
            self.assertEqual(info.language, 'ru')
            self.assertEqual(list((project / 'data' / 'temp').iterdir()), [])
            adapter.close()
            with self.assertRaises(RuntimeError):
                adapter.transcribe(np.zeros(1, dtype=np.float32))


if __name__ == '__main__':
    unittest.main()
