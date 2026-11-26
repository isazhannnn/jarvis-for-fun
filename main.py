from __future__ import annotations
import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def setup_logging():
    directory = ROOT / 'data/logs'
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / 'jarvis.log', maxBytes=2_000_000, backupCount=2, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')


def main():
    parser = argparse.ArgumentParser(description='JARVIS desktop assistant')
    parser.add_argument('--check', action='store_true', help='Read-only diagnostics; no camera or microphone capture')
    parser.add_argument('--command', help='Run an explicit text command without the UI (confirmations are not auto-approved)')
    args = parser.parse_args()
    setup_logging()
    from jarvis.config import load_settings, save_settings
    settings = load_settings(ROOT)
    if not (ROOT / 'settings.json').exists():
        save_settings(ROOT, settings)
    if args.check:
        from importlib.util import find_spec
        from jarvis.windows import WindowsController
        from jarvis.voice import VoiceService
        voice = VoiceService(ROOT, lambda _: None, lambda *_: None)
        diagnostics = {
            'python': sys.version, 'project': str(ROOT),
            'dependencies': {n: find_spec(n) is not None for n in ['tkinter', 'vosk', 'sounddevice', 'PIL', 'cv2', 'mediapipe', 'pyautogui', 'edge_tts', 'faster_whisper']},
            'voice': voice.available(),
            'hand_model': (ROOT / 'models/hand_landmarker.task').is_file(),
            'ai_runtime': (ROOT / 'runtime/llama/llama-server.exe').is_file(),
            'ai_model': (ROOT / 'models/qwen2.5-1.5b-instruct-q4_k_m.gguf').is_file(),
            'system': WindowsController(ROOT).system_status(),
        }
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        voice.close()
        return
    if args.command:
        from jarvis.engine import CommandEngine
        engine = CommandEngine(ROOT, settings)
        try:
            print(json.dumps(engine.run(args.command), ensure_ascii=False, indent=2))
        finally:
            engine.close()
        return
    if os.name == 'nt':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    from jarvis.gui import JarvisApp
    JarvisApp(ROOT, settings).run()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception('Application startup failed')
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, 'Не удалось запустить Джарвиса. Откройте data\\logs\\jarvis.log или запустите «Диагностика.cmd».', 'JARVIS', 0x10)
        raise
