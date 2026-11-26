# Parsec KZ | JARVIS Experiment 001 | See COPYRIGHT.md
"""Optional local webcam hand control with explicit arming and an emergency stop.

The classifier and interpreter intentionally have no camera or desktop imports,
so they can be checked without opening hardware or moving the mouse.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import math
from pathlib import Path
import queue
import threading
import time


@dataclass(frozen=True)
class Gesture:
    name: str
    x: float = 0.5
    y: float = 0.5


def _xy(point):
    if hasattr(point, "x"):
        return float(point.x), float(point.y)
    return float(point[0]), float(point[1])


def _distance(a, b):
    ax, ay = _xy(a)
    bx, by = _xy(b)
    return math.hypot(ax - bx, ay - by)


def _straight(a, b, c):
    ax, ay = _xy(a)
    bx, by = _xy(b)
    cx, cy = _xy(c)
    ux, uy, vx, vy = ax - bx, ay - by, cx - bx, cy - by
    denominator = math.hypot(ux, uy) * math.hypot(vx, vy)
    return denominator > 1e-8 and (ux * vx + uy * vy) / denominator < -0.72


def classify_gesture(landmarks) -> Gesture:
    """Classify one hand in normalized coordinates; supports either hand.

    A raised index moves, a raised index+middle scrolls, thumb/index pinch
    clicks only when accepted by the temporal interpreter. Other poses pause.
    """
    if landmarks is None or len(landmarks) != 21:
        return Gesture("none")
    if any(not math.isfinite(value) for p in landmarks for value in _xy(p)):
        return Gesture("none")
    palm = max(_distance(landmarks[5], landmarks[17]), 0.025)
    extended = []
    for mcp, pip, tip in ((5, 6, 8), (9, 10, 12), (13, 14, 16), (17, 18, 20)):
        extended.append(
            _straight(landmarks[mcp], landmarks[pip], landmarks[tip])
            and _distance(landmarks[tip], landmarks[0])
            > _distance(landmarks[pip], landmarks[0]) * 1.12
        )
    index, middle, ring, little = extended
    x, y = _xy(landmarks[8])
    pinch = _distance(landmarks[4], landmarks[8]) / palm
    # The distance from the wrist rejects a closed fist as a pinch.
    if not (middle or ring or little) and pinch < 0.34 and _distance(landmarks[8], landmarks[0]) > palm * 1.05:
        return Gesture("pinch", x, y)
    if index and not (middle or ring or little):
        return Gesture("move", x, y)
    if index and middle and not (ring or little):
        return Gesture("scroll", *_xy(landmarks[0]))
    return Gesture("pause", x, y)


class GestureInterpreter:
    """Turn stable poses into bounded actions, without touching the desktop."""

    def __init__(self):
        self.reset()

    def reset(self, cursor=None):
        self.previous = "none"
        self.pose_since = 0.0
        self.cursor = cursor
        self.last_move = float("-inf")
        self.last_click = float("-inf")
        self.pinch_consumed = False
        self.scroll_y = None
        self.last_scroll = 0.0

    def update(self, gesture: Gesture, now: float, armed: bool = True) -> list[tuple]:
        if not armed:
            self.reset()
            return []
        if gesture.name != self.previous:
            self.previous = gesture.name
            self.pose_since = now
            self.scroll_y = None
            if gesture.name != "pinch":
                self.pinch_consumed = False
        if gesture.name == "move":
            if now - self.pose_since < 0.12:
                return []
            self.last_move = now
            x = max(0.0, min(1.0, (gesture.x - 0.12) / 0.76))
            y = max(0.0, min(1.0, (gesture.y - 0.12) / 0.76))
            if self.cursor is None:
                self.cursor = (x, y)
            else:
                cx, cy = self.cursor
                # Exponential smoothing suppresses landmark jitter.
                self.cursor = (cx + (x - cx) * 0.32, cy + (y - cy) * 0.32)
            return [("move", *self.cursor)]
        if gesture.name == "pinch":
            if (not self.pinch_consumed and now - self.pose_since >= 0.15
                    and now - self.last_move < 1.2 and now - self.last_click > 0.65):
                self.pinch_consumed = True
                self.last_click = now
                return [("click",)]
            return []
        if gesture.name == "scroll":
            if now - self.pose_since < 0.18:
                return []
            if self.scroll_y is None:
                self.scroll_y = gesture.y
                return []
            delta = self.scroll_y - gesture.y
            if abs(delta) >= 0.028 and now - self.last_scroll >= 0.09:
                amount = max(-4, min(4, int(delta / 0.028)))
                self.scroll_y += -amount * 0.028
                self.last_scroll = now
                return [("scroll", amount)]
        return []


HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
    (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
)
POSE_LABELS = {
    "none": "Рука не найдена — покажите одну руку",
    "move": "Указательный палец — движение курсора",
    "pinch": "Щипок — щелчок",
    "scroll": "Два пальца — прокрутка вверх/вниз",
    "pause": "Ладонь / кулак — пауза",
}


class HandControlWindow:
    def __init__(self, project_dir: Path):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.project_dir = Path(project_dir)
        self.root = tk.Tk()
        self.root.title("JARVIS · Управление жестами")
        self.root.geometry("1000x780")
        self.root.minsize(850, 700)
        self.root.configure(bg="#07111e")
        self.stop_event = threading.Event()
        self.armed = threading.Event()
        self.arm_at = float("inf")
        self.frame_queue = queue.Queue(maxsize=1)
        self.events = queue.Queue()
        self.worker = None
        self.closed = False
        self.photo = None
        self._last_pose = "none"
        self.camera_index = tk.StringVar(value="0")
        self.status = tk.StringVar(value="Камера выключена. Изображение обрабатывается только на ноутбуке.")
        self.pose = tk.StringVar(value="Сначала включите камеру, затем разрешите управление.")

        header = tk.Frame(self.root, bg="#07111e")
        header.pack(fill="x", padx=24, pady=(18, 8))
        tk.Label(header, text="J.A.R.V.I.S. / HAND CONTROL", font=("Segoe UI", 18, "bold"),
                 fg="#46e5ff", bg="#07111e").pack(side="left")
        tk.Button(header, text="Свернуть", command=self.root.iconify, bg="#142b40",
                  fg="#dfecfa", relief="flat", padx=12).pack(side="right")
        self.preview = tk.Label(self.root, text="КАМЕРА ВЫКЛЮЧЕНА\n\nВидео не записывается и не отправляется в сеть",
                                font=("Segoe UI", 14), fg="#5f7e95", bg="#0a1b2b")
        self.preview.pack(fill="both", expand=True, padx=24, pady=8)
        tk.Label(self.root, textvariable=self.pose, font=("Segoe UI", 12, "bold"),
                 fg="#46e5ff", bg="#07111e").pack(padx=24, anchor="w")
        tk.Label(self.root, textvariable=self.status, font=("Segoe UI", 10),
                 fg="#b7cada", bg="#07111e", wraplength=930, justify="left").pack(padx=24, pady=(4, 8), anchor="w")
        controls = tk.Frame(self.root, bg="#07111e")
        controls.pack(fill="x", padx=24, pady=6)
        tk.Label(controls, text="Камера:", fg="#b7cada", bg="#07111e").pack(side="left")
        tk.Entry(controls, textvariable=self.camera_index, width=3, bg="#142b40", fg="white",
                 insertbackground="white", relief="flat", font=("Segoe UI", 12)).pack(side="left", padx=8)
        self.start_button = tk.Button(controls, text="Включить камеру", command=self.start_camera,
                                      bg="#116b84", fg="white", relief="flat", padx=14, pady=9)
        self.start_button.pack(side="left", padx=4)
        self.arm_button = tk.Button(controls, text="Разрешить управление", command=self.toggle_arm,
                                    bg="#185348", fg="white", relief="flat", padx=14, pady=9, state="disabled")
        self.arm_button.pack(side="left", padx=4)
        tk.Button(controls, text="СТОП / Esc", command=self.stop_camera, bg="#853b47", fg="white",
                  relief="flat", padx=14, pady=9).pack(side="right")
        help_text = (
            "☝ Один указательный палец — курсор  ·  Щипок большим и указательным — щелчок\n"
            "✌ Указательный + средний — прокрутка  ·  Ладонь или кулак — временная пауза\n"
            "Щелчок работает после движения курсора. Держите руку прямо, в 40–90 см от камеры.\n"
            "Аварийный стоп: Esc в любом окне или обычной мышью в любой угол экрана."
        )
        tk.Label(self.root, text=help_text, font=("Segoe UI", 10), fg="#8ba6bb", bg="#07111e",
                 justify="left", anchor="w").pack(fill="x", padx=24, pady=(8, 18))
        self.root.bind("<Escape>", lambda _event: self.stop_camera())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(40, self._poll)

    def start_camera(self):
        from tkinter import messagebox
        if self.worker and self.worker.is_alive():
            return
        try:
            index = int(self.camera_index.get())
            if not 0 <= index <= 9:
                raise ValueError
        except ValueError:
            messagebox.showerror("Камера", "Введите номер камеры от 0 до 9.", parent=self.root)
            return
        self.stop_event.clear()
        self.armed.clear()
        self.arm_at = float("inf")
        self.arm_button.configure(state="disabled", text="Разрешить управление", bg="#185348")
        self.start_button.configure(state="disabled")
        self.status.set("Загружаю модель и открываю камеру…")
        self.worker = threading.Thread(target=self._camera_loop, args=(index,), daemon=True, name="jarvis-hand-camera")
        self.worker.start()

    def toggle_arm(self):
        if self.armed.is_set():
            self.armed.clear()
            self.arm_at = float("inf")
            self.arm_button.configure(text="Разрешить управление", bg="#185348")
            self.status.set("Только предпросмотр. Управление курсором выключено.")
        else:
            self.arm_at = time.monotonic() + 2.5
            self.armed.set()
            self.arm_button.configure(text="Отключить управление", bg="#8a6030")
            self.status.set("Управление включится через 2,5 секунды. Покажите указательный палец.")

    def stop_camera(self):
        self.armed.clear()
        self.arm_at = float("inf")
        self.stop_event.set()
        self.arm_button.configure(state="disabled", text="Разрешить управление", bg="#185348")
        self.status.set("Управление остановлено. Освобождаю камеру…" if self.worker and self.worker.is_alive() else "Камера выключена.")

    def _camera_loop(self, camera_index):
        capture = None
        detector = None
        try:
            try:
                import cv2
                import mediapipe as mp
                import pyautogui
                from PIL import Image
            except ImportError as exc:
                raise RuntimeError("Не хватает библиотек жестов. Запустите setup.bat. " + str(exc)) from exc
            model = self.project_dir / "models" / "hand_landmarker.task"
            if not model.is_file():
                raise RuntimeError("Не найдена модель models/hand_landmarker.task. Запустите setup.bat.")
            options = mp.tasks.vision.HandLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=str(model)),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_hands=1, min_hand_detection_confidence=0.65,
                min_hand_presence_confidence=0.65, min_tracking_confidence=0.65,
            )
            detector = mp.tasks.vision.HandLandmarker.create_from_options(options)
            # A normal webcam is sufficient; DirectShow avoids Windows startup delays.
            capture = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
            if not capture.isOpened():
                capture.release()
                capture = cv2.VideoCapture(camera_index)
            if not capture.isOpened():
                raise RuntimeError("Не удалось открыть камеру. Закройте другие приложения камеры и проверьте разрешения Windows.")
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            capture.set(cv2.CAP_PROP_FPS, 24)
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.02
            screen_w, screen_h = pyautogui.size()
            interpreter = GestureInterpreter()
            self.events.put(("ready", "Камера включена. Только предпросмотр — мышь ещё не управляется."))
            last_timestamp = -1
            failures = 0
            was_controlling = False
            while not self.stop_event.is_set():
                if self._escape_pressed():
                    self.events.put(("stop", "Аварийная остановка по Esc."))
                    break
                ok, frame = capture.read()
                if not ok:
                    failures += 1
                    if failures > 30:
                        raise RuntimeError("Камера перестала выдавать изображение. Проверьте подключение.")
                    time.sleep(0.05)
                    continue
                failures = 0
                frame = cv2.flip(frame, 1)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                timestamp = max(last_timestamp + 1, int(time.monotonic() * 1000))
                last_timestamp = timestamp
                result = detector.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp)
                landmarks = result.hand_landmarks[0] if result.hand_landmarks else None
                gesture = classify_gesture(landmarks)
                now = time.monotonic()
                controlling = self.armed.is_set() and now >= self.arm_at
                if controlling and not was_controlling:
                    px, py = pyautogui.position()
                    interpreter.reset((px / screen_w, py / screen_h))
                actions = interpreter.update(gesture, now, armed=controlling)
                was_controlling = controlling
                for action in actions:
                    # Recheck the emergency state immediately before every OS action.
                    if self.stop_event.is_set() or not self.armed.is_set() or self._escape_pressed():
                        self.stop_event.set()
                        break
                    if action[0] == "move":
                        pyautogui.moveTo(max(5, min(screen_w - 6, round(action[1] * screen_w))),
                                         max(5, min(screen_h - 6, round(action[2] * screen_h))), duration=0)
                    elif action[0] == "click":
                        pyautogui.click()
                    elif action[0] == "scroll":
                        pyautogui.scroll(action[1])
                h, w = rgb.shape[:2]
                if landmarks:
                    for a, b in HAND_CONNECTIONS:
                        pa = (int(landmarks[a].x * w), int(landmarks[a].y * h))
                        pb = (int(landmarks[b].x * w), int(landmarks[b].y * h))
                        cv2.line(rgb, pa, pb, (45, 235, 205), 2)
                    for p in landmarks:
                        cv2.circle(rgb, (int(p.x * w), int(p.y * h)), 4, (70, 220, 255), -1)
                if self.armed.is_set() and not controlling:
                    overlay = "START IN " + str(max(1, math.ceil(self.arm_at - now)))
                else:
                    overlay = "CONTROL ON | ESC = STOP" if controlling else "PREVIEW ONLY | CONTROL OFF"
                cv2.rectangle(rgb, (0, 0), (w, 34), (8, 25, 40), -1)
                cv2.putText(rgb, overlay, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.56,
                            (70, 235, 200) if controlling else (210, 220, 230), 1, cv2.LINE_AA)
                payload = (Image.fromarray(rgb), gesture.name, controlling, self.armed.is_set())
                try:
                    self.frame_queue.put_nowait(payload)
                except queue.Full:
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.frame_queue.put_nowait(payload)
                    except queue.Full:
                        pass
        except Exception as exc:
            if type(exc).__name__ == "FailSafeException":
                self.events.put(("error", "Аварийная остановка: мышь в углу экрана. Уберите мышь из угла и включите камеру снова."))
            else:
                self.events.put(("error", str(exc)))
        finally:
            self.armed.clear()
            self.stop_event.set()
            if capture is not None:
                capture.release()
            if detector is not None:
                try:
                    detector.close()
                except Exception:
                    pass
            self.events.put(("finished", "Камера выключена."))

    @staticmethod
    def _escape_pressed():
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000)
        except (AttributeError, OSError):
            return False

    def _poll(self):
        if self.closed:
            return
        from tkinter import messagebox
        while True:
            try:
                event, message = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "ready":
                if not self.stop_event.is_set():
                    self.arm_button.configure(state="normal")
                    self.status.set(message)
            elif event == "error":
                self.status.set(message)
                messagebox.showerror("JARVIS · Жесты", message, parent=self.root)
            elif event == "stop":
                self.status.set(message)
                self.stop_camera()
            elif event == "finished":
                self.start_button.configure(state="normal")
                self.arm_button.configure(state="disabled", text="Разрешить управление", bg="#185348")
                self.pose.set("Управление остановлено. Камера выключена.")
                self.preview.configure(image="", text="КАМЕРА ВЫКЛЮЧЕНА")
                self.photo = None
                while True:
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        break
        try:
            image, pose, controlling, armed = self.frame_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            if not self.stop_event.is_set():
                from PIL import Image, ImageTk
                image.thumbnail((max(320, self.preview.winfo_width() - 8), max(240, self.preview.winfo_height() - 8)), Image.Resampling.BILINEAR)
                self.photo = ImageTk.PhotoImage(image)
                self.preview.configure(image=self.photo, text="")
                self.pose.set(POSE_LABELS.get(pose, pose))
                if controlling:
                    self.status.set("УПРАВЛЕНИЕ ВКЛЮЧЕНО · основной экран · Esc — остановить камеру и курсор")
                elif not armed:
                    self.status.set("Только предпросмотр. Нажмите «Разрешить управление», когда будете готовы.")
        self.root.after(40, self._poll)

    def close(self):
        self.closed = True
        self.armed.clear()
        self.stop_event.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_gestures(project_dir: Path):
    HandControlWindow(project_dir).run()
