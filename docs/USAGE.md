# Operating guide / Parsec KZ

[← Overview](../README.md)

## Start

Run `setup.cmd` once, then `start.cmd`. Model downloads require a network connection. Use `diagnostics.cmd` to inspect readiness. Installation places runtime data in the project directory.

## Voice

Press the microphone button or F8 within the application for one command. Wake mode listens for “Джарвис” / “Jarvis”. Escape in the assistant window stops listening. Select a specific language when short phrases are misidentified. Vosk is the lightweight Russian option; Whisper provides multilingual recognition. Installed Windows voices are offline; edge synthesis requires a reachable external service.

## Gestures

Open the hand-control window, start the camera and inspect the preview. Arm control explicitly and wait for the countdown. Index finger: cursor. Thumb/index pinch after movement: one click. Two fingers: scroll. Palm/fist or no hand: pause. Escape stops camera processing globally while that mode runs.

## Local data

| Location | Contents |
| --- | --- |
| `settings.json` | Device and language preferences |
| `data/notes` | Text notes |
| `data/screenshots` | Explicitly requested captures |
| `data/recycle` | Recoverable moved files |
| `data/logs` | Runtime diagnostics |
| `models`, `runtime`, `.venv` | Downloaded models and dependencies |

These paths are excluded from Git. Restore with “Восстанови последний файл”; existing files are not overwritten. Shutdown and restart require confirmation and schedule a 60-second delay; “Отмени выключение” cancels it.

## Limitations

This assistant does not autonomously inspect screens or operate arbitrary interfaces. Recognition depends on speech, language, lighting and hardware. Local language-model answers may be incorrect. Review every proposed action before confirming.
