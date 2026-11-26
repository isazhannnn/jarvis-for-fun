# Architecture / Parsec KZ

[← Overview](../README.md)

## Source map

```text
jarvis/
├── gui.py           Desktop HUD, workers and event queue
├── engine.py        Explicit commands and confirmation lifecycle
├── brain.py         Local model process, history and validated plans
├── windows.py       Win32 operations and application discovery
├── voice.py         Listening sessions, wake word and speech output
├── multilingual.py  Whisper capture, language and backend selection
├── whisper_cpp.py   Portable speech subprocess adapter
├── gestures.py      Hand poses, smoothing and armed pointer control
└── config.py        Local settings
scripts/             Verified resumable asset downloads
tests/               Unit and lifecycle tests with fake devices
assets/              Portfolio artwork
docs/                Architecture and operating guide
```

## Trust boundaries

| Input | Validation | Effect |
| --- | --- | --- |
| Explicit command | Router patterns and operation constraints | Specific Windows operation |
| Model response | Finite action schema and argument allowlists | Proposal requiring confirmation |
| Microphone | Session identity, mute gate and wake-word policy | Command text |
| Camera | Hand confidence, gesture classification and arm state | Pointer actions |
| File deletion request | Absolute path, protected directories, change check | Confirmed move to application recycle folder |

The GUI thread handles widgets. Blocking work runs in workers; results return through queues. The local model server is started lazily, bound to loopback, authenticated, and closed with its owning application. Model output is never evaluated as Python or a shell command.

## Design tradeoffs

Small CPU models reduce hardware requirements but lose accuracy. Native Tk keeps deployment simple but requires explicit queue-based coordination. Gesture control uses a deliberate arming step and an emergency stop. Test doubles make lifecycle behavior repeatable; live model and hardware checks remain separate.
