"""Launch the optional JARVIS camera/gesture control window."""
from pathlib import Path


def main():
    import ctypes
    import os
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
    from jarvis.gestures import run_gestures
    run_gestures(Path(__file__).resolve().parent)


if __name__ == "__main__":
    main()
