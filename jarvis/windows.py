"""Small explicit Windows operations. Never runs shell-generated commands."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import os
from pathlib import Path
import shutil
import subprocess
import time
import webbrowser


class WindowsController:
    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self._cpu_previous = None
        self._app_index = None

    @staticmethod
    def _windows():
        if os.name != 'nt':
            raise RuntimeError('Управление ноутбуком доступно в Windows.')

    def open_url(self, url):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname:
            raise ValueError('Разрешены только адреса сайтов http и https.')
        if not webbrowser.open(url):
            raise RuntimeError('Не удалось открыть браузер по умолчанию.')

    def _apps(self):
        if self._app_index is None:
            self._app_index = {}
            for root in [Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'Microsoft/Windows/Start Menu/Programs',
                         Path(os.environ.get('APPDATA', str(Path.home()))) / 'Microsoft/Windows/Start Menu/Programs']:
                if root.exists():
                    for item in root.rglob('*.lnk'):
                        self._app_index.setdefault(item.stem.casefold(), item)
        return self._app_index

    def open_app(self, name):
        self._windows()
        key = name.strip().casefold()
        builtins = {'блокнот': 'notepad.exe', 'notepad': 'notepad.exe',
                    'калькулятор': 'calc.exe', 'calculator': 'calc.exe',
                    'проводник': 'explorer.exe', 'explorer': 'explorer.exe',
                    'диспетчер задач': 'taskmgr.exe', 'paint': 'mspaint.exe',
                    'паинт': 'mspaint.exe', 'ножницы': 'SnippingTool.exe'}
        if key in ('браузер', 'интернет'):
            return self.open_url('https://www.google.com')
        sites = {'ютуб': 'https://www.youtube.com', 'youtube': 'https://www.youtube.com',
                 'гугл': 'https://www.google.com', 'google': 'https://www.google.com',
                 'яндекс': 'https://ya.ru', 'вконтакте': 'https://vk.com',
                 'вк': 'https://vk.com', 'ватсап': 'https://web.whatsapp.com'}
        if key in sites:
            return self.open_url(sites[key])
        if key in builtins:
            system_dir = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32'
            subprocess.Popen([str(system_dir / builtins[key])])
            return
        if key in ('настройки', 'параметры'):
            os.startfile('ms-settings:')
            return
        aliases = {'хром': 'google chrome', 'chrome': 'google chrome',
                   'эдж': 'microsoft edge', 'edge': 'microsoft edge',
                   'телеграм': 'telegram', 'телеграмм': 'telegram',
                   'дискорд': 'discord', 'стим': 'steam', 'спотифай': 'spotify',
                   'ворд': 'word', 'эксель': 'excel', 'вс код': 'visual studio code',
                   'код': 'visual studio code'}
        lookup = aliases.get(key, key)
        index = self._apps()
        candidate = index.get(lookup)
        if candidate is None:
            candidates = [(n, p) for n, p in index.items() if lookup in n and len(lookup) >= 3]
            if candidates:
                candidate = min(candidates, key=lambda p: len(p[0]))[1]
        if candidate:
            os.startfile(str(candidate))
            return
        raise ValueError(f'Программа «{name}» не найдена в меню Пуск. Попробуйте её название из меню Пуск.')

    def open_folder(self, name):
        self._windows()
        home = Path.home()
        aliases = {'загрузки': home / 'Downloads', 'документы': home / 'Documents',
                   'рабочий стол': home / 'Desktop', 'изображения': home / 'Pictures',
                   'музыка': home / 'Music', 'видео': home / 'Videos',
                   'проект': self.project_dir, 'джарвис': self.project_dir,
                   'заметки': self.project_dir / 'data/notes',
                   'скриншоты': self.project_dir / 'data/screenshots',
                   'диск д': Path('D:/'), 'диск d': Path('D:/'), 'диск с': Path('C:/')}
        path = aliases.get(name.casefold(), Path(os.path.expandvars(name.strip('"'))).expanduser())
        if path in [self.project_dir / 'data/notes', self.project_dir / 'data/screenshots']:
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError(f'Папка не найдена: {path}')
        os.startfile(str(path.resolve()))

    def _key(self, code):
        self._windows()
        user = ctypes.windll.user32
        user.keybd_event(code, 0, 0, 0)
        user.keybd_event(code, 0, 2, 0)

    def volume(self, action):
        if isinstance(action, int):
            level = max(0, min(100, action))
            # A volume key changes the Windows master endpoint by about two percent.
            for _ in range(50):
                self._key(0xAE)
            for _ in range(round(level / 2)):
                self._key(0xAF)
            return
        codes = {'up': 0xAF, 'down': 0xAE, 'mute': 0xAD}
        for _ in range(5 if action in ('up', 'down') else 1):
            self._key(codes[action])

    def media(self, action):
        self._key({'play': 0xB3, 'next': 0xB0, 'previous': 0xB1}[action])

    def show_desktop(self):
        self._windows()
        user = ctypes.windll.user32
        user.keybd_event(0x5B, 0, 0, 0)
        try:
            self._key(ord('D'))
        finally:
            user.keybd_event(0x5B, 0, 2, 0)

    def switch_window(self):
        self._windows()
        user = ctypes.windll.user32
        user.keybd_event(0x12, 0, 0, 0)
        try:
            self._key(0x09)
        finally:
            user.keybd_event(0x12, 0, 2, 0)

    def screenshot(self):
        from datetime import datetime
        from PIL import ImageGrab
        folder = self.project_dir / 'data/screenshots'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f') + '.png')
        ImageGrab.grab(all_screens=True).save(path)
        return path

    def power(self, action):
        self._windows()
        system_dir = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32'
        if action not in ('shutdown', 'restart', 'abort'):
            raise ValueError('Неизвестная команда питания.')
        args = ['/a'] if action == 'abort' else [('/s' if action == 'shutdown' else '/r'), '/t', '60']
        subprocess.run([str(system_dir / 'shutdown.exe'), *args], check=True,
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)

    def lock(self):
        self._windows()
        if not ctypes.windll.user32.LockWorkStation():
            raise RuntimeError('Windows не удалось заблокировать экран.')

    def list_windows(self):
        self._windows()
        user = ctypes.windll.user32
        # HWND is pointer-sized; default ctypes int conversion truncates it on x64.
        user.IsWindowVisible.argtypes = [wt.HWND]
        user.IsWindowVisible.restype = wt.BOOL
        user.GetWindowTextLengthW.argtypes = [wt.HWND]
        user.GetWindowTextLengthW.restype = ctypes.c_int
        user.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        user.GetWindowTextW.restype = ctypes.c_int
        user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        user.GetWindowThreadProcessId.restype = wt.DWORD
        result = []
        callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        user.EnumWindows.argtypes = [callback_type, wt.LPARAM]
        user.EnumWindows.restype = wt.BOOL
        def enum(hwnd, _):
            if user.IsWindowVisible(hwnd):
                length = user.GetWindowTextLengthW(hwnd)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user.GetWindowTextW(hwnd, buffer, length + 1)
                    pid = wt.DWORD()
                    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value != os.getpid():
                        result.append({'hwnd': int(hwnd), 'title': buffer.value})
            return True
        user.EnumWindows(callback_type(enum), 0)
        return result

    def focus_window(self, hwnd):
        self._windows()
        user = ctypes.windll.user32
        hwnd = int(hwnd)
        user.IsWindow.argtypes = [wt.HWND]
        user.IsWindow.restype = wt.BOOL
        user.IsIconic.argtypes = [wt.HWND]
        user.IsIconic.restype = wt.BOOL
        user.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        user.ShowWindow.restype = wt.BOOL
        user.SetForegroundWindow.argtypes = [wt.HWND]
        user.SetForegroundWindow.restype = wt.BOOL
        if not user.IsWindow(hwnd):
            raise ValueError('Окно уже закрыто.')
        if user.IsIconic(hwnd):
            user.ShowWindow(hwnd, 9)
        if not user.SetForegroundWindow(hwnd):
            raise RuntimeError('Windows не разрешила переключиться на это окно.')

    def type_text(self, text, hwnd):
        hwnd = int(hwnd)
        self.focus_window(hwnd)
        time.sleep(0.3)
        # Unicode SendInput supports Russian without changing clipboard or keyboard layout.
        ULONG_PTR = ctypes.c_size_t
        class KeyboardInput(ctypes.Structure):
            _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD), ('time', wt.DWORD), ('dwExtraInfo', ULONG_PTR)]
        class MouseInput(ctypes.Structure):
            _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD), ('dwFlags', wt.DWORD), ('time', wt.DWORD), ('dwExtraInfo', ULONG_PTR)]
        class InputUnion(ctypes.Union):
            _fields_ = [('ki', KeyboardInput), ('mi', MouseInput)]
        class Input(ctypes.Structure):
            _fields_ = [('type', wt.DWORD), ('u', InputUnion)]
        user = ctypes.windll.user32
        user.SendInput.argtypes = [wt.UINT, ctypes.POINTER(Input), ctypes.c_int]
        user.SendInput.restype = wt.UINT
        user.GetForegroundWindow.argtypes = []
        user.GetForegroundWindow.restype = wt.HWND
        for offset in range(0, len(text), 64):
            if user.GetForegroundWindow() != hwnd:
                raise RuntimeError('Ввод остановлен: активное окно изменилось.')
            inputs = []
            encoded = text[offset:offset + 64].encode('utf-16-le')
            for i in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[i:i + 2], 'little')
                for flags in (4, 6):
                    event = Input(type=1)
                    event.u.ki = KeyboardInput(0, unit, flags, 0, 0)
                    inputs.append(event)
            array = (Input * len(inputs))(*inputs)
            if user.SendInput(len(inputs), array, ctypes.sizeof(Input)) != len(inputs):
                raise RuntimeError('Приложение не приняло ввод. Возможно, оно запущено от администратора.')

    def recycle_file(self, path):
        self._windows()
        source_path = Path(path)
        if source_path.is_symlink() or source_path.is_junction():
            raise ValueError('Ссылки и точки подключения нельзя удалять этой командой.')
        path = source_path.resolve(strict=True)
        if not path.is_file():
            raise ValueError('Команда перемещает в корзину только один обычный файл, без папок.')
        if len(str(path)) >= 260 or str(path).startswith('\\\\?\\'):
            raise ValueError('Путь слишком длинный для безопасного перемещения в корзину Windows.')
        class FileOperation(ctypes.Structure):
            _fields_ = [('hwnd', wt.HWND), ('wFunc', wt.UINT), ('pFrom', wt.LPCWSTR),
                        ('pTo', wt.LPCWSTR), ('fFlags', wt.WORD),
                        ('fAnyOperationsAborted', wt.BOOL), ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wt.LPCWSTR)]
        source = ctypes.create_unicode_buffer(str(path) + '\0\0')
        op = FileOperation()
        op.wFunc = 3
        op.pFrom = ctypes.cast(source, wt.LPCWSTR)
        # Allow undo, avoid recursion and related HTML folders. If Windows cannot
        # recycle the file, it MUST show its permanent-deletion warning rather
        # than silently falling back to an irreversible delete.
        op.fFlags = 0x40 | 0x10 | 0x04 | 0x1000 | 0x2000 | 0x4000 | 0x0400
        operation = ctypes.windll.shell32.SHFileOperationW
        operation.argtypes = [ctypes.POINTER(FileOperation)]
        operation.restype = ctypes.c_int
        result = operation(ctypes.byref(op))
        if result or op.fAnyOperationsAborted:
            raise RuntimeError(f'Не удалось переместить файл в корзину (код {result}).')

    def system_status(self):
        status = {'cpu_percent': None, 'memory_percent': None, 'battery_percent': None,
                  'power_plugged': None, 'disk_free_gb': round(shutil.disk_usage(self.project_dir).free / 2**30, 1),
                  'platform': 'Windows' if os.name == 'nt' else os.name}
        if os.name != 'nt':
            return status
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('dwLength', wt.DWORD), ('dwMemoryLoad', wt.DWORD)] + [(n, ctypes.c_ulonglong) for n in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
        memory = MemoryStatus()
        memory.dwLength = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            status['memory_percent'] = int(memory.dwMemoryLoad)
        class PowerStatus(ctypes.Structure):
            _fields_ = [('ac', ctypes.c_ubyte), ('flag', ctypes.c_ubyte), ('percent', ctypes.c_ubyte), ('reserved', ctypes.c_ubyte), ('life', wt.DWORD), ('full', wt.DWORD)]
        power = PowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)):
            status['battery_percent'] = int(power.percent) if power.percent != 255 else None
            status['power_plugged'] = bool(power.ac) if power.ac != 255 else None
        idle, kernel, user_time = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user_time)):
            as_int = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
            now = (as_int(idle), as_int(kernel) + as_int(user_time))
            if self._cpu_previous:
                delta = now[1] - self._cpu_previous[1]
                if delta > 0:
                    status['cpu_percent'] = max(0, min(100, round(100 * (1 - (now[0] - self._cpu_previous[0]) / delta))))
            self._cpu_previous = now
        return status
