"""Explicit desktop commands and a separate conversational local model."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import math
import operator
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.parse import quote_plus

from .windows import WindowsController


def result(text, kind='answer', **extra):
    return {'text': str(text), 'kind': kind, **extra}


def calculate(expression: str):
    """Evaluate only bounded arithmetic, without eval, names or function calls."""
    if len(expression) > 160:
        raise ValueError('Слишком длинный пример.')
    expression = expression.replace('×', '*').replace('÷', '/').replace('^', '**').replace(',', '.')
    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
                  ast.Pow: operator.pow}
    def walk(node, depth=0):
        if depth > 18:
            raise ValueError('Слишком сложный пример.')
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = walk(node.operand, depth + 1) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            left, right = walk(node.left, depth + 1), walk(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and (abs(right) > 12 or abs(left) > 1e10):
                raise ValueError('Слишком большая степень.')
            value = operations[type(node.op)](left, right)
        else:
            raise ValueError('Можно считать числа, скобки и действия + − * / % ^.')
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError('Результат слишком большой.')
        return value
    return walk(ast.parse(expression, mode='eval').body)


@dataclass
class PendingAction:
    expires: float
    callback: object
    action: str


class CommandEngine:
    def __init__(self, project_dir: Path, settings: dict):
        self.project_dir = Path(project_dir)
        self.settings = settings
        self.windows = WindowsController(self.project_dir)
        self.pending = {}
        self._brain = None

    def command_examples(self):
        return [
            'Открой блокнот', 'Открой калькулятор', 'Открой Telegram',
            'Открой YouTube', 'Открой папку загрузки', 'Открой сайт example.com',
            'Найди в интернете погоду в городе', 'Найди файл диплом',
            'Сделай скриншот', 'Громче', 'Тише', 'Громкость 40',
            'Выключи звук', 'Пауза', 'Следующий трек', 'Покажи рабочий стол',
            'Переключи окно', 'Список окон', 'Активируй окно Блокнот',
            'Напечатай в окно Блокнот: Привет, мир!',
            'Создай заметку: Купить продукты', 'Покажи заметки',
            'Состояние системы', 'Сколько времени', 'Посчитай (12 + 8) * 5',
            'Удали файл D:\\пример.txt', 'Восстанови последний файл',
            'Заблокируй экран', 'Выключи компьютер', 'Перезагрузи компьютер',
            'Отмени выключение', 'Open calculator', 'Take a screenshot',
            'Калькуляторды аш', 'Explain what a processor does',
        ]

    def system_status(self):
        return self.windows.system_status()

    def close(self):
        self.pending.clear()
        if self._brain is not None:
            self._brain.close()

    def _audit(self, action, outcome):
        try:
            directory = self.project_dir / 'data/logs'
            directory.mkdir(parents=True, exist_ok=True)
            # Store operation names only, never microphone audio or typed file contents.
            with (directory / 'actions.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps({'time': datetime.now().isoformat(timespec='seconds'),
                                         'action': action, 'outcome': outcome}) + '\n')
        except OSError:
            logging.exception('Cannot write operation audit')

    def _confirm(self, details, callback, action):
        token = secrets.token_urlsafe(24)
        self.pending[token] = PendingAction(time.monotonic() + 90, callback, action)
        return result('Нужно подтверждение действия.', 'confirm', token=token, details=details)

    def cancel(self, token):
        self.pending.pop(token, None)

    def confirm(self, token):
        pending = self.pending.pop(token, None)
        if pending is None or pending.expires < time.monotonic():
            return result('Подтверждение истекло или уже использовано. Повторите команду.', 'error')
        try:
            answer = pending.callback()
            self._audit(pending.action, 'confirmed')
            return answer
        except Exception as exc:
            self._audit(pending.action, 'failed')
            return result(str(exc), 'error')

    def run(self, text):
        self.pending.clear()
        text = str(text).strip()
        if not text:
            return result('Введите команду или нажмите микрофон.', 'error')
        if len(text) > 5000:
            return result('Команда слишком длинная. Максимум — 5000 символов.', 'error')
        original = text
        text = re.sub(r'^(?:джарвис|джервис|jarvis)[\s,.:!—-]*', '', text, flags=re.I).strip()
        key = text.casefold().replace('ё', 'е').strip(' .!?')
        try:
            response = self._dispatch(text, key)
            if response is None:
                if self.settings.get('local_ai_enabled', True) or self.settings.get('ollama_enabled', False):
                    if self._brain is None:
                        from .brain import LocalBrain
                        self._brain = LocalBrain(self.project_dir, self.settings)
                    plan = self._brain.plan(original)
                    if plan:
                        planned = self._planned_action(plan)
                        if planned is not None:
                            return planned
                    return result(self._brain.reply(original))
                return result('Команда не найдена. Откройте «Список команд» или включите локальный ИИ в настройках.', 'error')
            return response
        except (ValueError, OSError, RuntimeError, SyntaxError, ZeroDivisionError) as exc:
            return result(str(exc), 'error')
        except Exception:
            logging.exception('Command failed')
            return result('Не удалось выполнить команду. Подробности в data/logs/jarvis.log.', 'error')

    def _dispatch(self, text, key):
        # Exact English/Kazakh commands are useful without waiting for the language model.
        translations = {
            'take a screenshot': 'сделай скриншот', 'screenshot': 'скриншот',
            'volume up': 'громче', 'volume down': 'тише', 'mute': 'выключи звук',
            'show desktop': 'покажи рабочий стол', 'switch window': 'переключи окно',
            'system status': 'состояние системы', 'what time is it': 'сколько времени',
            'lock screen': 'заблокируй экран', 'shutdown computer': 'выключи компьютер',
            'restart computer': 'перезагрузи компьютер', 'cancel shutdown': 'отмени выключение',
            'калькуляторды аш': 'открой калькулятор', 'блокнотты аш': 'открой блокнот',
            'скриншот жаса': 'сделай скриншот', 'дыбысты көтер': 'громче',
            'дыбысты азайт': 'тише', 'дыбысты өшір': 'выключи звук',
            'сағат неше': 'сколько времени', 'экранды бұғатта': 'заблокируй экран',
        }
        if key in translations:
            text = key = translations[key]
        if key in ('помощь', 'команды', 'что ты умеешь', 'help'):
            return result('Примеры команд:\n' + '\n'.join('• ' + x for x in self.command_examples()))
        if key in ('привет', 'здравствуй', 'здравствуйте', 'hello', 'hi', 'сәлем'):
            return result('Я на связи. Готов к командам. Можно говорить, печатать или включить жесты рук.')
        if key in ('спасибо', 'thanks', 'рахмет'):
            return result('Всегда к вашим услугам.')
        if key in ('сколько времени', 'который час', 'время'):
            return result('Сейчас ' + datetime.now().strftime('%H:%M'))
        if key in ('какая дата', 'какое сегодня число', 'дата'):
            return result('Сегодня ' + datetime.now().strftime('%d.%m.%Y'))
        if key in ('состояние системы', 'состояние ноутбука', 'покажи систему', 'батарея'):
            status = self.system_status()
            return result(f"Память: {status['memory_percent']}%. Батарея: {status['battery_percent'] if status['battery_percent'] is not None else 'нет данных'}%. Свободно на диске проекта: {status['disk_free_gb']} ГБ.")
        match = re.fullmatch(r'(?:посчитай|вычисли|calculate)\s+(.+)', text, re.I | re.S)
        if match:
            return result(str(calculate(match[1])))
        if key in ('сделай скриншот', 'скриншот', 'снимок экрана', 'сфотографируй экран'):
            path = self.windows.screenshot()
            self._audit('screenshot', 'done')
            return result('Скриншот сохранён:\n' + str(path), path=str(path))
        if key in ('громче', 'увеличь громкость', 'сделай громче'):
            self.windows.volume('up')
            return result('Увеличил громкость.')
        if key in ('тише', 'уменьши громкость', 'сделай тише'):
            self.windows.volume('down')
            return result('Уменьшил громкость.')
        if key in ('выключи звук', 'без звука', 'включи звук', 'переключи звук'):
            self.windows.volume('mute')
            return result('Переключил состояние звука Windows.')
        match = re.fullmatch(r'(?:громкость|volume)(?:\s+на)?\s+(\d{1,3})\s*%?', key)
        if match:
            value = int(match[1])
            if value > 100:
                raise ValueError('Громкость задаётся от 0 до 100.')
            self.windows.volume(value)
            return result(f'Громкость — примерно {value}%.')
        media = {'пауза': 'play', 'продолжи': 'play', 'включи музыку': 'play',
                 'следующий трек': 'next', 'предыдущий трек': 'previous', 'pause': 'play', 'next track': 'next'}
        if key in media:
            self.windows.media(media[key])
            return result('Отправил команду активному проигрывателю.')
        if key in ('покажи рабочий стол', 'сверни окна', 'сверни все окна'):
            self.windows.show_desktop()
            return result('Переключил рабочий стол.')
        if key in ('переключи окно', 'следующее окно'):
            self.windows.switch_window()
            return result('Переключил окно.')
        if key in ('список окон', 'покажи окна', 'list windows'):
            windows = self.windows.list_windows()
            return result('Открытые окна:\n' + '\n'.join('• ' + w['title'] for w in windows[:30]))
        match = re.fullmatch(r'(?:активируй окно|переключись на окно|focus window)\s+(.+)', text, re.I)
        if match:
            window = self._find_window(match[1])
            self.windows.focus_window(window['hwnd'])
            return result('Активировано: ' + window['title'])
        match = re.fullmatch(r'(?:напечатай в окно|type in window)\s+(.+?):\s*(.+)', text, re.I | re.S)
        if match:
            window = self._find_window(match[1])
            content = match[2]
            hwnd = window['hwnd']
            def type_confirmed():
                self.windows.type_text(content, hwnd)
                return result('Текст введён в окно: ' + window['title'])
            return self._confirm('Напечатать в окно «' + window['title'] + '»:\n\n' + content[:1500] + '\n\nНажатия Enter и отправки сообщений не будет.', type_confirmed, 'type_text')
        if key in ('выключи компьютер', 'выключи ноутбук', 'заверши работу'):
            return self._confirm('Выключить ноутбук через 60 секунд? Сохраните открытые документы. Для отмены скажите «Отмени выключение».', lambda: self._power('shutdown'), 'shutdown')
        if key in ('перезагрузи компьютер', 'перезагрузи ноутбук', 'перезагрузка'):
            return self._confirm('Перезагрузить ноутбук через 60 секунд? Сохраните открытые документы. Для отмены скажите «Отмени выключение».', lambda: self._power('restart'), 'restart')
        if key in ('отмени выключение', 'отмена выключения', 'отмени перезагрузку'):
            self.windows.power('abort')
            return result('Запланированное выключение или перезагрузка отменены.')
        if key in ('заблокируй экран', 'заблокируй компьютер', 'заблокируй ноутбук'):
            return self._confirm('Заблокировать экран Windows? Для входа понадобится ваш пароль или PIN.', self._lock, 'lock')
        match = re.fullmatch(r'(?:создай заметку|запиши заметку|заметка|create note|write note)\s*:?\s+(.+)', text, re.I | re.S)
        if match:
            return self._note(match[1])
        if key in ('покажи заметки', 'открой заметки', 'мои заметки', 'show notes'):
            self.windows.open_folder('заметки')
            return result('Открыл папку заметок.')
        match = re.fullmatch(r'(?:удали файл|delete file)\s+(.+)', text, re.I | re.S)
        if match:
            return self._trash_plan(match[1])
        if key in ('восстанови последний файл', 'restore last file'):
            return self._restore_plan()
        match = re.fullmatch(r'(?:найди файл|найди файлы|find file|find files)\s+(.+)', text, re.I)
        if match:
            return self._search_files(match[1])
        match = re.fullmatch(r'(?:найди в интернете|поищи в интернете|найди|search(?: for)?)\s+(.+)', text, re.I)
        if match:
            self.windows.open_url('https://www.google.com/search?q=' + quote_plus(match[1]))
            return result('Открыл поиск в браузере: ' + match[1])
        match = re.fullmatch(r'(?:открой папку|open folder)\s+(.+)', text, re.I)
        if match:
            self.windows.open_folder(match[1])
            return result('Открыл папку: ' + match[1])
        match = re.fullmatch(r'(?:открой сайт|open website|open site)\s+(.+)', text, re.I)
        if match:
            url = match[1].strip()
            if '://' not in url:
                url = 'https://' + url
            self.windows.open_url(url)
            return result('Открыл сайт: ' + url)
        match = re.fullmatch(r'(?:открой|запусти|open|launch)\s+(.+)', text, re.I)
        if match:
            target = match[1].strip()
            folders = {'загрузки', 'документы', 'рабочий стол', 'изображения', 'музыка', 'видео', 'проект', 'скриншоты', 'диск д', 'диск d', 'диск с'}
            if target.casefold() in folders:
                self.windows.open_folder(target)
            else:
                english = {'calculator': 'калькулятор', 'notepad': 'блокнот', 'browser': 'браузер', 'settings': 'настройки', 'file explorer': 'проводник'}
                self.windows.open_app(english.get(target.casefold(), target))
            self._audit('open', 'done')
            return result('Открыл: ' + target)
        return None

    def _find_window(self, name):
        windows = [w for w in self.windows.list_windows() if name.casefold() in w['title'].casefold()]
        if not windows:
            raise ValueError('Окно не найдено. Сначала откройте приложение и запросите «Список окон».')
        if len(windows) > 1:
            raise ValueError('Нашлось несколько окон. Уточните название:\n' + '\n'.join(w['title'] for w in windows))
        return windows[0]

    def _planned_action(self, plan):
        """Model output selects a constrained operation; it never becomes code."""
        from .brain import PLAN_APPS, PLAN_FOLDERS
        if not isinstance(plan, dict) or set(plan) != {'action', 'argument'}:
            return None
        action, argument = plan['action'], plan['argument']
        if not isinstance(action, str) or not isinstance(argument, str):
            return None
        if action == 'time' and not argument:
            return result('Сейчас ' + datetime.now().strftime('%H:%M'))
        if action == 'system_status' and not argument:
            return self._dispatch('состояние системы', 'состояние системы')
        descriptions = {
            'open_app': 'Открыть приложение: ', 'open_folder': 'Открыть папку: ',
            'search_web': 'Открыть поиск в браузере: ', 'screenshot': 'Сделать скриншот',
            'volume_up': 'Увеличить громкость', 'volume_down': 'Уменьшить громкость',
            'mute': 'Переключить состояние звука', 'show_desktop': 'Показать рабочий стол',
            'media_play': 'Переключить воспроизведение / паузу', 'media_next': 'Следующий трек',
        }
        if action not in descriptions:
            return None
        if action == 'open_app' and argument not in PLAN_APPS:
            return None
        if action == 'open_folder' and argument not in PLAN_FOLDERS:
            return None
        if action == 'search_web' and (not argument or len(argument) > 250):
            return None
        if action not in {'open_app', 'open_folder', 'search_web'} and argument:
            return None
        def perform():
            if action == 'open_app':
                self.windows.open_app(argument)
            elif action == 'open_folder':
                self.windows.open_folder(argument)
            elif action == 'search_web':
                self.windows.open_url('https://www.google.com/search?q=' + quote_plus(argument))
            elif action == 'screenshot':
                path = self.windows.screenshot()
                return result('Скриншот сохранён:\n' + str(path), path=str(path))
            elif action in ('volume_up', 'volume_down', 'mute'):
                self.windows.volume({'volume_up': 'up', 'volume_down': 'down', 'mute': 'mute'}[action])
            elif action == 'show_desktop':
                self.windows.show_desktop()
            elif action in ('media_play', 'media_next'):
                self.windows.media('play' if action == 'media_play' else 'next')
            return result('Выполнено: ' + descriptions[action] + argument)
        return self._confirm('Локальный ИИ распознал запрос как действие:\n\n' + descriptions[action] + argument + '\n\nВыполнить?', perform, 'ai_' + action)

    def _power(self, action):
        self.windows.power(action)
        return result('Ноутбук ' + ('выключится' if action == 'shutdown' else 'перезагрузится') + ' через 60 секунд. Для отмены: «Отмени выключение».')

    def _lock(self):
        self.windows.lock()
        return result('Экран заблокирован.')

    def _note(self, text):
        folder = self.project_dir / 'data/notes'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f') + '.txt')
        path.write_text(text, encoding='utf-8')
        self._audit('note', 'created')
        return result('Заметка сохранена:\n' + str(path), path=str(path))

    def _search_files(self, name):
        roots = [Path(p).expanduser() for p in self.settings.get('search_roots', []) if isinstance(p, str)]
        matches, seen, scanned = [], set(), 0
        started = time.monotonic()
        skip = {'.git', '.venv', 'node_modules', '__pycache__', 'appdata', 'windows', '$recycle.bin', 'system volume information', 'models', 'runtime', 'downloads'}
        stopped = False
        for root in roots:
            if not root.is_dir():
                continue
            for directory, subdirs, files in os.walk(root, followlinks=False):
                subdirs[:] = [d for d in subdirs if d.casefold() not in skip and not (Path(directory) / d).is_symlink()]
                for filename in files:
                    scanned += 1
                    if name.casefold() in filename.casefold():
                        path = str(Path(directory) / filename)
                        if path.casefold() not in seen:
                            matches.append(path)
                            seen.add(path.casefold())
                    if len(matches) >= 30 or scanned >= 50000 or time.monotonic() - started > 5:
                        stopped = True
                        break
                if stopped:
                    break
            if stopped:
                break
        if matches:
            return result('Нашёл файлы:\n' + '\n'.join(matches) + ('\nПоиск ограничен 30 результатами, 50 000 файлами или 5 секундами.' if stopped else ''))
        return result('Файлы не найдены в выбранных папках. Папки поиска можно изменить в settings.json. Содержимое файлов не читается.')

    def _file_to_trash(self, raw):
        path = Path(os.path.expandvars(raw.strip().strip('"'))).expanduser()
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('Укажите полный путь к обычному локальному файлу. Ссылки и папки не перемещаю.')
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError('Перемещать можно только отдельные файлы.')
        protected = [Path(os.environ.get('SystemRoot', 'C:/Windows')),
                     Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')),
                     Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')),
                     self.project_dir]
        for root in protected:
            if path.is_relative_to(root.resolve()):
                raise ValueError('Системные файлы и сам проект Джарвиса защищены от перемещения.')
        if path.name.casefold() in {'pagefile.sys', 'hiberfil.sys', 'swapfile.sys', 'bootmgr', 'ntldr'}:
            raise ValueError('Этот файл защищён.')
        return path

    def _trash_plan(self, raw):
        path = self._file_to_trash(raw)
        fingerprint = (path.stat().st_size, path.stat().st_mtime_ns)
        def move():
            current = self._file_to_trash(str(path))
            if (current.stat().st_size, current.stat().st_mtime_ns) != fingerprint:
                raise ValueError('Файл изменился после запроса. Повторите команду.')
            folder = self.project_dir / 'data/recycle' / secrets.token_hex(8)
            (folder / 'files').mkdir(parents=True)
            destination = folder / 'files' / current.name
            metadata = folder / 'restore.json'
            metadata.write_text(json.dumps({'original': str(current), 'filename': current.name, 'time': time.time()}, ensure_ascii=False), encoding='utf-8')
            shutil.move(str(current), str(destination))
            return result('Файл перенесён в корзину Джарвиса:\n' + str(destination) + '\nДля возврата: «Восстанови последний файл».')
        return self._confirm('Перенести файл в корзину Джарвиса (data/recycle)?\n\n' + str(path) + '\n\nМожно вернуть командой «Восстанови последний файл».', move, 'recycle')

    def _restore_plan(self):
        folder = self.project_dir / 'data/recycle'
        candidates = sorted(folder.glob('*/restore.json'), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
        for metadata in candidates:
            data = json.loads(metadata.read_text(encoding='utf-8'))
            filename = data['filename']
            if Path(filename).name != filename or filename in ('.', '..'):
                continue
            source = metadata.parent / 'files' / filename
            if not source.is_file():
                continue
            target = Path(data['original'])
            if not target.is_absolute() or target.parent.is_symlink():
                continue
            def restore(source=source, target=target, metadata=metadata):
                if target.exists():
                    raise ValueError('По исходному пути уже есть файл. Его не перезаписываю.')
                if not target.parent.is_dir():
                    raise ValueError('Исходная папка больше не существует.')
                shutil.move(str(source), str(target))
                metadata.unlink()
                return result('Файл восстановлен:\n' + str(target))
            return self._confirm('Вернуть файл из корзины Джарвиса?\n\n' + str(target), restore, 'restore')
        return result('В корзине Джарвиса нет файлов для восстановления.')
