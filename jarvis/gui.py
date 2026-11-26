"""Native, keyboard-accessible desktop interface for the local assistant."""
from __future__ import annotations

import json
import ctypes
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .engine import CommandEngine
from .voice import VoiceService


BG = "#070e1a"
PANEL = "#0c1829"
CARD = "#101f33"
LINE = "#20334b"
CYAN = "#51e2f5"
TEXT = "#e3edf8"
MUTED = "#92a7be"
GREEN = "#65dfb0"
AMBER = "#ffcb78"
RED = "#ff8e9b"


class JarvisApp:
    def __init__(self, project_dir: Path, settings: dict):
        self.project_dir = Path(project_dir)
        self.settings = settings
        # Keep Tk coordinates and screen capture coordinates in the same units.
        if sys.platform == "win32":
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        self.root = tk.Tk()
        self.root.title("J.A.R.V.I.S. — персональный помощник")
        self.root.configure(bg=BG)
        initial_width = max(900, min(1160, self.root.winfo_screenwidth() - 80))
        initial_height = max(650, min(800, self.root.winfo_screenheight() - 100))
        self.compact = initial_height < 740
        self.root.geometry(f"{initial_width}x{initial_height}")
        self.root.minsize(900, 650)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.events: queue.Queue = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="jarvis-ui")
        self.engine_lock = threading.Lock()
        self.engine = CommandEngine(self.project_dir, self.settings)
        self.closed = False
        self.busy = False
        self.listening = False
        self.voice_state = "idle"
        self.rotation = 0.0
        self.speaking = False
        self.gesture_process = None
        self.speech_enabled = tk.BooleanVar(value=bool(settings.get("voice_output", True)))
        self.status_text = tk.StringVar(value="ГОТОВ К РАБОТЕ")
        self.status_detail = tk.StringVar(value="Микрофон выключен · команды работают локально")
        self.mode_text = tk.StringVar(value="ЛОКАЛЬНЫЙ РЕЖИМ")
        self.command = tk.StringVar()
        self.voice = self._make_voice()
        self._configure_styles()
        self._build()
        self._message(
            "JARVIS",
            "Система готова. Открою программы, найду файлы, сделаю скриншот "
            "или помогу управлять ноутбуком. Кнопка «Жесты рук» открывает управление камерой. "
            "Напишите команду или нажмите «Микрофон». "
            "Для важных действий появится подтверждение.",
        )
        self._message("СИСТЕМА", "F8 — голосовая команда · Ctrl+L — поле ввода · Esc — выключить микрофон")
        self.root.bind("<Return>", self._enter)
        self.root.bind("<F8>", lambda _event: self._listen_once())
        self.root.bind("<Control-l>", lambda _event: self.entry.focus_set())
        self.root.bind("<Escape>", lambda _event: self._stop_voice())
        self.root.bind("<Configure>", self._resize)
        self.root.after(100, self._drain_events)
        self.root.after(50, self._animate)
        self.root.after(250, self._refresh_status)
        self.root.after(150, self._voice_availability)
        self.entry.focus_set()

    def _make_voice(self):
        return VoiceService(
            self.project_dir,
            on_text=lambda text: self.events.put(("heard", text)),
            on_status=lambda state, message: self.events.put(("voice", (state, message))),
            is_speaking=lambda: self.speaking,
        )

    def _configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=CARD, background=LINE, foreground=TEXT,
                        arrowcolor=CYAN, bordercolor=LINE, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", LINE)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

    def _label(self, parent, text="", *, size=10, color=TEXT, bold=False, bg=None, **kwargs):
        return tk.Label(parent, text=text, bg=bg or parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)

    def _button(self, parent, text, command, *, accent=False, small=False, **kwargs):
        button = tk.Button(
            parent, text=text, command=command, bg=CYAN if accent else CARD,
            fg=BG if accent else TEXT, activebackground="#83eef9" if accent else LINE,
            activeforeground=BG if accent else TEXT, bd=0, relief="flat",
            highlightthickness=0, cursor="hand2", padx=12, pady=9 if small else 11,
            font=("Segoe UI", 9 if small else 10, "bold" if accent else "normal"),
            disabledforeground="#536479", **kwargs,
        )
        return button

    def _build(self):
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        self.sidebar = tk.Frame(self.root, bg=PANEL, width=218, padx=17, pady=18)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.pack_propagate(False)
        self._label(self.sidebar, "J.A.R.V.I.S.", size=21, color=CYAN, bold=True).pack(anchor="w")
        self._label(self.sidebar, "ПЕРСОНАЛЬНЫЙ ПОМОЩНИК", size=8, color=MUTED).pack(anchor="w", pady=(3, 24))
        tk.Frame(self.sidebar, bg=LINE, height=1).pack(fill="x")
        self._label(self.sidebar, "УПРАВЛЕНИЕ", size=8, color=MUTED, bold=True).pack(anchor="w", pady=(22, 10))
        self.listen_button = self._button(self.sidebar, "◉  Микрофон  /  F8", self._listen_once, accent=True)
        self.listen_button.pack(fill="x", pady=(0, 8))
        self.wake_button = self._button(self.sidebar, "Слушать «Джарвис»", self._toggle_wake)
        self.wake_button.pack(fill="x", pady=(0, 8))
        self._button(self.sidebar, "■  Выключить микрофон", self._stop_voice, small=True).pack(fill="x")
        self.voice_note = self._label(self.sidebar, "Микрофон выключен", size=9, color=MUTED,
                                      wraplength=180, justify="left", height=2)
        self.voice_note.pack(anchor="w", pady=(8, 10))
        self._button(self.sidebar, "✋  Жесты рук", self._gestures, small=True).pack(fill="x", pady=(0, 6))
        self.quick_frame = tk.Frame(self.sidebar, bg=PANEL)
        if not self.compact:
            self.quick_frame.pack(fill="x")
        self._label(self.quick_frame, "БЫСТРЫЕ КОМАНДЫ", size=8, color=MUTED, bold=True).pack(anchor="w", pady=(6, 6))
        for label, command in (
            ("▧  Сделать скриншот", "сделай скриншот"),
            ("⌁  Состояние ноутбука", "состояние системы"),
        ):
            self._button(self.quick_frame, label, lambda t=command: self._submit(t), small=True,
                         anchor="w").pack(fill="x", pady=3)
        bottom = tk.Frame(self.sidebar, bg=PANEL)
        self.sidebar_bottom = bottom
        bottom.pack(side="bottom", fill="x")
        tk.Frame(bottom, bg=LINE, height=1).pack(fill="x", pady=(0, 13))
        tk.Checkbutton(bottom, text="Голосовые ответы", variable=self.speech_enabled,
                       command=self._toggle_speech, bg=PANEL, fg=TEXT, selectcolor=CARD,
                       activebackground=PANEL, activeforeground=CYAN,
                       font=("Segoe UI", 10), bd=0, highlightthickness=0).pack(anchor="w")
        self._button(bottom, "Настройки", self._settings, small=True).pack(fill="x", pady=(12, 7))
        self._button(bottom, "Список команд", self._help, small=True).pack(fill="x")
        self._label(bottom, "WINDOWS  /  LOCAL CONTROL", size=7, color=MUTED).pack(anchor="w", pady=(17, 0))

        main = tk.Frame(self.root, bg=BG, padx=23, pady=18)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        header = tk.Frame(main, bg=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._label(header, "Ваш личный центр управления", size=18, bold=True).pack(side="left")
        self.clock = self._label(header, "", size=10, color=MUTED)
        self.clock.pack(side="right")

        hero = tk.Frame(main, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        hero.grid(row=1, column=0, sticky="ew")
        hero.columnconfigure(0, weight=3)
        hero.columnconfigure(1, weight=2)
        self.reactor = tk.Canvas(hero, width=320, height=214, bg=PANEL, bd=0,
                                 highlightthickness=0)
        self.reactor.grid(row=0, column=0, sticky="nsew", padx=(4, 0))
        metrics = tk.Frame(hero, bg=PANEL, padx=17, pady=17)
        metrics.grid(row=0, column=1, sticky="nsew")
        self._label(metrics, "СИСТЕМНЫЙ МОНИТОР", size=8, color=MUTED, bold=True).pack(anchor="w")
        self.metric_labels = {}
        for key, caption in (("cpu", "Процессор"), ("memory", "Память"),
                             ("battery", "Батарея"), ("disk", "Свободно на диске")):
            line = tk.Frame(metrics, bg=PANEL)
            line.pack(fill="x", pady=(13 if key == "cpu" else 8, 0))
            self._label(line, caption, size=9, color=MUTED).pack(side="left")
            value = self._label(line, "—", size=10, color=CYAN, bold=True)
            value.pack(side="right", padx=(8, 0))
            self.metric_labels[key] = value
        self._label(metrics, size=8, color=GREEN, textvariable=self.mode_text).pack(anchor="w", pady=(15, 0))

        status = tk.Frame(main, bg=BG)
        status.grid(row=2, column=0, sticky="ew", pady=(11, 10))
        self.status_dot = self._label(status, "●", size=10, color=GREEN)
        self.status_dot.pack(side="left")
        self._label(status, size=9, color=TEXT, bold=True,
                    textvariable=self.status_text).pack(side="left", padx=(6, 0))
        self._label(status, "ИСТОРИЯ СЕАНСА", size=8, color=MUTED).pack(side="right")

        chat_frame = tk.Frame(main, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        chat_frame.grid(row=3, column=0, sticky="nsew")
        chat_frame.rowconfigure(0, weight=1)
        chat_frame.columnconfigure(0, weight=1)
        self.chat = tk.Text(chat_frame, bg=PANEL, fg=TEXT, relief="flat", bd=0,
                            padx=16, pady=12, wrap="word", font=("Segoe UI", 10),
                            state="disabled", cursor="arrow", highlightthickness=0,
                            spacing1=2, spacing3=4)
        self.chat.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(chat_frame, command=self.chat.yview, bg=LINE, troughcolor=PANEL,
                              activebackground=MUTED, width=10, bd=0)
        scroll.grid(row=0, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=scroll.set)
        self.chat.tag_configure("label", foreground=CYAN, font=("Consolas", 9, "bold"), spacing1=7)
        self.chat.tag_configure("user", foreground=TEXT, font=("Consolas", 9, "bold"), spacing1=7)
        self.chat.tag_configure("system", foreground=MUTED)
        self.chat.tag_configure("error", foreground=RED)
        self.chat.tag_configure("body", foreground=TEXT, lmargin1=0, lmargin2=0)

        footer = tk.Frame(main, bg=BG)
        footer.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        self._label(footer, "ЧЕМ ПОМОЧЬ?", size=8, color=MUTED, bold=True).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.entry = tk.Entry(footer, textvariable=self.command, bg=CARD, fg=TEXT,
                              insertbackground=CYAN, relief="flat", font=("Segoe UI", 12),
                              highlightbackground=LINE, highlightcolor=CYAN, highlightthickness=1,
                              bd=0)
        self.entry.grid(row=1, column=0, sticky="ew", ipady=12)
        self.send_button = self._button(footer, "Выполнить  →", self._submit, accent=True)
        self.send_button.grid(row=1, column=1, sticky="ns", padx=(9, 0))
        self._label(main, size=8, color=MUTED, textvariable=self.status_detail,
                    anchor="w", wraplength=760, justify="left").grid(row=5, column=0, sticky="ew", pady=(9, 0))

    def _enter(self, event):
        if event.widget is self.entry:
            self._submit()
            return "break"
        return None

    def _message(self, who, text, error=False):
        if self.closed:
            return
        self.chat.configure(state="normal")
        tag = "user" if who == "ВЫ" else "label"
        self.chat.insert("end", f"{who}  ·  {datetime.now():%H:%M}\n", tag)
        body_tag = "error" if error else "system" if who == "СИСТЕМА" else "body"
        self.chat.insert("end", str(text).strip() + "\n\n", body_tag)
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _submit(self, text=None):
        if self.closed:
            return
        if self.busy:
            self.status_detail.set("Завершаю текущую команду. Следующую можно отправить через мгновение.")
            return
        value = text if isinstance(text, str) else self.command.get()
        value = value.strip()
        if not value:
            self.entry.focus_set()
            return
        self.command.set("")
        self._message("ВЫ", value)
        self._set_busy(True)
        self._work(lambda: self.engine.run(value))

    def _set_busy(self, busy):
        self.busy = busy
        self.send_button.configure(state="disabled" if busy else "normal")
        self.status_text.set("ВЫПОЛНЯЮ КОМАНДУ" if busy else "ГОТОВ К РАБОТЕ")
        self.status_dot.configure(fg=CYAN if busy else GREEN)

    def _work(self, operation):
        def job():
            try:
                with self.engine_lock:
                    result = operation()
                self.events.put(("result", result))
            except Exception as exc:
                self.events.put(("result", {"kind": "error", "text": f"Не удалось выполнить команду: {exc}"}))
        self.pool.submit(job)

    def _handle_result(self, result):
        self._set_busy(False)
        if result.get("kind") == "confirm":
            self._set_busy(True)
            self.status_text.set("НУЖНО ПОДТВЕРЖДЕНИЕ")
            token = result.get("token", "")
            self._message("JARVIS", result.get("text", "Нужно подтверждение."))
            approved = messagebox.askyesno(
                "Подтверждение действия", result.get("details") or result.get("text", "Выполнить действие?"),
                parent=self.root, icon="warning", default="no",
            )
            if approved:
                self._set_busy(True)
                self._work(lambda: self.engine.confirm(token))
            else:
                self.engine.cancel(token)
                self._set_busy(False)
                self._message("СИСТЕМА", "Действие отменено.")
            return
        text = result.get("text", "Готово.")
        error = result.get("kind") == "error"
        self._message("JARVIS", text, error=error)
        self.status_detail.set("Действие завершено." if not error else "Подробности ошибки — в истории сеанса.")
        if self.speech_enabled.get() and text and not error:
            self.pool.submit(self.voice.speak, text)

    def _voice_availability(self):
        def job():
            try:
                self.events.put(("available", self.voice.available()))
            except Exception as exc:
                self.events.put(("available", (False, str(exc))))
        self.pool.submit(job)

    def _listen_once(self):
        if self.busy or self.closed:
            return
        self.listening = False
        self.wake_button.configure(text="Слушать «Джарвис»", fg=TEXT)
        self.status_text.set("ВКЛЮЧАЮ МИКРОФОН")
        self.pool.submit(self.voice.listen_once)

    def _toggle_wake(self):
        if self.closed:
            return
        if self.listening:
            self._stop_voice()
            return
        self.listening = True
        self.wake_button.configure(text="● Слушаю «Джарвис»", fg=CYAN)
        self._message("СИСТЕМА", "Голосовой режим включён. Скажите: «Джарвис, открой блокнот». Esc выключает микрофон.")
        self.pool.submit(self.voice.start, True)

    def _stop_voice(self):
        if self.closed:
            return
        self.listening = False
        self.wake_button.configure(text="Слушать «Джарвис»", fg=TEXT)
        self.pool.submit(self.voice.stop)
        if not self.busy:
            self.status_text.set("ГОТОВ К РАБОТЕ")
        self.status_detail.set("Микрофон выключен · введите команду ниже")
        self.voice_note.configure(text="Микрофон выключен", fg=MUTED)

    def _toggle_speech(self):
        self.settings["voice_output"] = self.speech_enabled.get()
        try:
            self._save_settings()
        except OSError as exc:
            self._message("СИСТЕМА", f"Не удалось сохранить настройку: {exc}", error=True)

    def _save_settings(self):
        destination = self.project_dir / "settings.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    def _drain_events(self):
        if self.closed:
            return
        try:
            for _ in range(40):
                event, value = self.events.get_nowait()
                if event == "result":
                    self._handle_result(value)
                elif event == "heard":
                    if self.busy:
                        self._message("СИСТЕМА", f"Распознано: {value}. Завершаю предыдущую команду; повторите её после ответа.")
                    else:
                        self._submit(value)
                elif event == "voice":
                    state, message = value
                    self.voice_state = state
                    self.speaking = state == "speaking"
                    self.voice_note.configure(text=message, fg=RED if state == "error" else CYAN if state == "listening" else MUTED)
                    if not self.busy:
                        self.status_text.set({"listening": "СЛУШАЮ", "processing": "РАСПОЗНАЮ РЕЧЬ", "speaking": "ГОЛОСОВОЙ ОТВЕТ", "error": "ПРОВЕРЬТЕ ГОЛОСОВОЙ РЕЖИМ"}.get(state, "ГОТОВ К РАБОТЕ"))
                    if state == "error":
                        if not message.startswith(("Сетевой голос", "Озвучивание")):
                            self.listening = False
                            self.wake_button.configure(text="Слушать «Джарвис»", fg=TEXT)
                        self._message("СИСТЕМА", message, error=True)
                    elif state == "idle" and not self.listening:
                        self.status_detail.set("Микрофон выключен · введите команду ниже")
                elif event == "available":
                    available, message = value
                    self.voice_note.configure(text="Микрофон выключен · голос готов" if available else "Голос не установлен.\nЗапустите setup.ps1.",
                                              fg=MUTED if available else AMBER)
                    if not available:
                        self._message("СИСТЕМА", message)
                elif event == "telemetry":
                    self._show_metrics(value)
        except queue.Empty:
            pass
        self.root.after(90, self._drain_events)

    def _refresh_status(self):
        if self.closed:
            return
        def job():
            try:
                self.events.put(("telemetry", self.engine.system_status()))
            except Exception:
                pass
        self.pool.submit(job)
        self.root.after(5000, self._refresh_status)

    def _show_metrics(self, info):
        def percent(value):
            return "—" if value is None else f"{value:.0f}%"
        self.metric_labels["cpu"].configure(text=percent(info.get("cpu_percent")))
        self.metric_labels["memory"].configure(text=percent(info.get("memory_percent")))
        battery = info.get("battery_percent")
        self.metric_labels["battery"].configure(text=percent(battery) + ("  ⚡" if info.get("power_plugged") and battery is not None else ""))
        free = info.get("disk_free_gb")
        self.metric_labels["disk"].configure(text="—" if free is None else f"{free:.1f} ГБ")
        if self.settings.get("local_ai_enabled", True):
            runtime = self.project_dir / "runtime" / "llama" / "llama-server.exe"
            models = list((self.project_dir / "models").glob("*.gguf"))
            self.mode_text.set("ЛОКАЛЬНЫЙ AI QWEN" if runtime.is_file() and models else "ЛОКАЛЬНЫЙ AI: НУЖНА УСТАНОВКА")
        elif self.settings.get("ollama_enabled"):
            self.mode_text.set("ЛОКАЛЬНЫЙ AI OLLAMA")
        else:
            self.mode_text.set("ЛОКАЛЬНОЕ УПРАВЛЕНИЕ")

    def _animate(self):
        if self.closed:
            return
        canvas = self.reactor
        width = max(canvas.winfo_width(), 240)
        height = max(canvas.winfo_height(), 200)
        cx, cy = width / 2, height / 2 - 4
        radius = min(height * .35, width * .30)
        canvas.delete("all")
        active = self.busy or self.voice_state in ("listening", "speaking", "processing")
        self.rotation = (self.rotation + (2.8 if active else .65)) % 360
        for idx in range(4):
            r = radius + 9 + idx * 4
            canvas.create_oval(cx-r, cy-r, cx+r, cy+r, outline="#142d42", width=1)
        for start in (0, 120, 240):
            canvas.create_arc(cx-radius, cy-radius, cx+radius, cy+radius,
                              start=start+self.rotation, extent=88, outline=CYAN,
                              width=3, style="arc")
        inner = radius * .78
        for start in (30, 150, 270):
            canvas.create_arc(cx-inner, cy-inner, cx+inner, cy+inner,
                              start=start-self.rotation*.7, extent=94, outline="#32768a",
                              width=5, style="arc")
        rr = radius * .57
        canvas.create_oval(cx-rr, cy-rr, cx+rr, cy+rr, outline="#285167", width=1, fill="#0e2031")
        for idx in range(17):
            x = cx + (idx-8)*4
            pulse = math.sin(idx*.67+self.rotation*.13)
            bar = (5 + abs(pulse)*20) if active else (3+abs(pulse)*5)
            canvas.create_line(x, cy-bar, x, cy+bar, fill=CYAN if active else "#3c8da1", width=2)
        for idx in range(36):
            angle = math.radians(idx*10)
            r = radius + 26
            canvas.create_line(cx+math.cos(angle)*r, cy+math.sin(angle)*r,
                               cx+math.cos(angle)*(r+3), cy+math.sin(angle)*(r+3), fill=LINE)
        canvas.create_text(cx, height-12, text="J.A.R.V.I.S.  /  SYSTEM ONLINE", fill=MUTED,
                           font=("Consolas", 8))
        self.clock.configure(text=datetime.now().strftime("%H:%M:%S"))
        self.root.after(65, self._animate)

    def _resize(self, event):
        if event.widget is not self.root:
            return
        compact = event.height < 740
        if compact == self.compact:
            return
        self.compact = compact
        if compact:
            self.quick_frame.pack_forget()
        else:
            self.quick_frame.pack(fill="x", before=self.sidebar_bottom)

    def _help(self):
        window = tk.Toplevel(self.root)
        window.title("Команды JARVIS")
        window.configure(bg=PANEL)
        window.geometry("680x580")
        window.transient(self.root)
        self._label(window, "Что можно сказать", size=19, bold=True).pack(anchor="w", padx=22, pady=(20, 12))
        text = tk.Text(window, bg=CARD, fg=TEXT, wrap="word", relief="flat", font=("Segoe UI", 11),
                       padx=17, pady=15, spacing3=10, highlightthickness=0)
        text.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        try:
            examples = self.engine.command_examples()
        except Exception:
            examples = ["открой блокнот", "сделай скриншот", "сделай громче", "который час"]
        text.insert("end", "\n".join(f"• {example}" for example in examples))
        text.insert("end", "\n\nГОЛОСОВОЙ РЕЖИМ\nF8 — одна команда. «Слушать Джарвис» — ожидание обращения.\nEsc — выключить микрофон.\n\nСВОБОДНЫЙ ДИАЛОГ\nВстроенная модель Qwen работает на ноутбуке без интернета. При первом вопросе загрузка может занять до минуты; ответы зависят от мощности ноутбука. Дополнительно можно подключить свой Ollama в настройках.\n\nЖЕСТЫ РУК\nКнопка «Жесты рук» открывает отдельное окно. Включите камеру в нём и следуйте подсказкам.\n\nПодтверждения принимаются только кнопкой в окне приложения.")
        text.configure(state="disabled")
        self._button(window, "Понятно", window.destroy, accent=True).pack(anchor="e", padx=22, pady=(0, 18))

    def _gestures(self):
        if self.gesture_process is not None and self.gesture_process.poll() is None:
            self._message("СИСТЕМА", "Окно жестов уже открыто. Переключитесь на него через панель задач.")
            return
        script = self.project_dir / "gesture_control.py"
        if not script.is_file():
            self._message("СИСТЕМА", "Модуль жестов не найден. Проверьте файл gesture_control.py в папке проекта.", error=True)
            return
        try:
            self.gesture_process = subprocess.Popen(
                [sys.executable, str(script)], cwd=str(self.project_dir),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._message("СИСТЕМА", "Открываю управление жестами. Камера включается только кнопкой в отдельном окне.")
        except OSError as exc:
            self._message("СИСТЕМА", f"Не удалось открыть управление жестами: {exc}", error=True)

    def _settings(self):
        if self.busy:
            self.status_detail.set("Открою настройки после завершения команды.")
            return
        self._stop_voice()
        window = tk.Toplevel(self.root)
        window.title("Настройки JARVIS")
        window.configure(bg=PANEL)
        window.geometry("650x660")
        window.minsize(620, 560)
        window.transient(self.root)
        window.grab_set()
        body = tk.Frame(window, bg=PANEL)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg=PANEL, bd=0, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)
        content = tk.Frame(canvas, bg=PANEL, padx=23, pady=20)
        item = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        content.columnconfigure(0, weight=1)
        self._label(content, "Настройки помощника", size=19, bold=True).grid(row=0, column=0, sticky="w", pady=(0, 14))

        def field(row, label, variable):
            self._label(content, label, size=10, color=MUTED).grid(row=row, column=0, sticky="w", pady=(8, 5))
            entry = tk.Entry(content, textvariable=variable, bg=CARD, fg=TEXT, insertbackground=CYAN,
                             bd=0, relief="flat", font=("Segoe UI", 11))
            entry.grid(row=row+1, column=0, sticky="ew", ipady=7)
            return entry

        voice_engine = tk.StringVar(value=self.settings.get("voice_engine", "edge"))
        self._label(content, "Голосовые ответы", color=MUTED).grid(row=1, column=0, sticky="w", pady=(0, 5))
        ttk.Combobox(content, textvariable=voice_engine, values=("windows", "edge"), state="readonly").grid(row=2, column=0, sticky="ew")
        self._label(content, "edge — голоса Microsoft на выбранном языке через интернет; windows — установленные голоса Windows офлайн.",
                    size=8, color=MUTED, wraplength=575, justify="left").grid(row=3, column=0, sticky="w", pady=(5, 6))
        speech_backend = tk.StringVar(value=self.settings.get("speech_backend", "whisper"))
        self._label(content, "Распознавание: whisper — много языков; vosk — русский, быстрее", color=MUTED,
                    size=9, wraplength=575, justify="left").grid(row=4, column=0, sticky="w", pady=(8, 5))
        ttk.Combobox(content, textvariable=speech_backend, values=("whisper", "vosk"), state="readonly").grid(row=5, column=0, sticky="ew")
        speech_language = tk.StringVar(value=self.settings.get("speech_language", "auto"))
        self._label(content, "Язык речи: auto — автоматически; ru, kk, en и другие коды", color=MUTED,
                    size=9, wraplength=575, justify="left").grid(row=6, column=0, sticky="w", pady=(8, 5))
        from .multilingual import LANGUAGES
        ttk.Combobox(content, textvariable=speech_language, values=LANGUAGES, state="readonly").grid(row=7, column=0, sticky="ew")
        device = tk.StringVar(value="" if self.settings.get("input_device") is None else str(self.settings["input_device"]))
        field(8, "Микрофон: индекс устройства (пусто — системный)", device)
        ollama = tk.BooleanVar(value=bool(self.settings.get("ollama_enabled", False)))
        tk.Checkbutton(content, text="Свободный диалог через локальный Ollama", variable=ollama,
                       bg=PANEL, fg=TEXT, selectcolor=CARD, activebackground=PANEL,
                       activeforeground=CYAN, highlightthickness=0, font=("Segoe UI", 10),
                       bd=0).grid(row=10, column=0, sticky="w", pady=(17, 4))
        model = tk.StringVar(value=self.settings.get("ollama_model", "qwen2.5:3b"))
        field(11, "Модель Ollama", model)
        url = tk.StringVar(value=self.settings.get("ollama_url", "http://127.0.0.1:11434"))
        field(13, "Адрес локального Ollama", url)
        roots = tk.StringVar(value="; ".join(self.settings.get("search_roots", [])))
        field(15, "Папки поиска файлов (разделитель ;)", roots)
        self._label(content, "Whisper и Vosk распознают речь на ноутбуке без отправки записей в интернет. Точность зависит от языка и качества микрофона.",
                    size=8, color=MUTED, wraplength=575, justify="left").grid(row=17, column=0, sticky="w", pady=(12, 10))
        local_ai = tk.BooleanVar(value=bool(self.settings.get("local_ai_enabled", True)))
        tk.Checkbutton(content, text="Локальный ИИ Qwen (без интернета)", variable=local_ai,
                       bg=PANEL, fg=TEXT, selectcolor=CARD, activebackground=PANEL,
                       activeforeground=CYAN, highlightthickness=0, font=("Segoe UI", 10),
                       bd=0).grid(row=18, column=0, sticky="w", pady=(9, 3))
        self._label(content, "Модель загружается при первом вопросе. Первый запуск — до минуты. Обычные команды выполняются сразу.",
                    size=8, color=MUTED, wraplength=575, justify="left").grid(row=19, column=0, sticky="w", pady=(3, 12))

        def save():
            raw_device = device.get().strip()
            try:
                mic = int(raw_device) if raw_device else None
                if mic is not None and mic < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Микрофон", "Введите неотрицательный индекс устройства или оставьте поле пустым.", parent=window)
                return
            if ollama.get() and (not model.get().strip() or not url.get().strip().startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:"))):
                messagebox.showerror("Ollama", "Укажите имя модели и локальный адрес вида http://127.0.0.1:11434.", parent=window)
                return
            before = dict(self.settings)
            self.settings.update(voice_engine=voice_engine.get(), input_device=mic,
                                 ollama_enabled=ollama.get(), ollama_model=model.get().strip(),
                                 ollama_url=url.get().strip().rstrip("/"),
                                 search_roots=[p.strip() for p in roots.get().split(";") if p.strip()],
                                 local_ai_enabled=local_ai.get(),
                                 speech_backend=speech_backend.get(), speech_language=speech_language.get(),
                                 voice_output=self.speech_enabled.get())
            try:
                self._save_settings()
            except OSError as exc:
                self.settings.clear()
                self.settings.update(before)
                messagebox.showerror("Настройки", f"Не удалось сохранить настройки: {exc}", parent=window)
                return
            self._stop_voice()
            window.destroy()
            self._message("СИСТЕМА", "Настройки сохранены. Микрофон выключен; включите его, когда будете готовы.")
            self._voice_availability()

        buttons = tk.Frame(window, bg=PANEL, padx=23, pady=14)
        buttons.pack(side="bottom", fill="x")
        self._button(buttons, "Отмена", window.destroy).pack(side="left", padx=(0, 8))
        self._button(buttons, "Сохранить", save, accent=True).pack(side="left")

    def _close(self):
        if self.closed:
            return
        self.closed = True
        def cleanup():
            try:
                self.voice.close()
            except Exception:
                pass
            try:
                close_engine = getattr(self.engine, "close", None)
                if close_engine is not None:
                    close_engine()
            except Exception:
                pass
            try:
                if self.gesture_process is not None and self.gesture_process.poll() is None:
                    self.gesture_process.terminate()
            except OSError:
                pass
        threading.Thread(target=cleanup, name="jarvis-cleanup", daemon=False).start()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def run(self):
        self.root.mainloop()
