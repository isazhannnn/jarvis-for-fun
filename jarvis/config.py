from __future__ import annotations

import json
from pathlib import Path


def defaults(project_dir: Path) -> dict:
    home = Path.home()
    return {
        'voice_output': True, 'voice_engine': 'windows', 'wake_word': True,
        'input_device': None, 'camera_index': 0,
        'speech_backend': 'whisper', 'speech_language': 'auto', 'edge_voice': 'auto',
        'local_ai_enabled': True, 'local_ai_port': 18765,
        'ollama_enabled': False, 'ollama_model': 'qwen2.5:3b',
        'ollama_url': 'http://127.0.0.1:11434',
        'search_roots': [str(home / 'Documents'), str(home / 'Desktop'),
                         str(home / 'Downloads'), str(project_dir)],
    }


def load_settings(project_dir: Path) -> dict:
    settings = defaults(project_dir)
    path = project_dir / 'settings.json'
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if isinstance(data, dict):
                settings.update(data)
        except (ValueError, OSError):
            pass
    return settings


def save_settings(project_dir: Path, settings: dict) -> None:
    path = project_dir / 'settings.json'
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
