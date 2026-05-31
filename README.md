# SAtella — AI NPC Dialogue for GTA San Andreas
Satell is GTA San Andreas mod which allows you to naturally speak to NPCs using a Speech-to-Text → LLMs → Text-to-Speech pipeline

Press **T** for text input or **Y** for voice input near any pedestrian and have a live conversation with them, powered by an LLM. The NPC knows the time of day, your wanted level, and the history of your exchange — and can react by fleeing, attacking, calling the cops, handing over cash, and more. Voice input (speech-to-text) and synthesized voice output (text-to-speech) are both supported.

---

## Features

- **Multi-turn conversations** — up to 6 back-and-forth turns with conversation memory per NPC
- **Text or voice input** — type with **T**, speak with **Y** (Whisper STT, runs locally)
- **Synthesized NPC voice** — Piper TTS plays positional 3D audio attached to the NPC
- **Per-ped personality profiles** — 200+ pedestrian models mapped to detailed character sheets in `PED_PROFILES.csv`
- **NPC reactions** — flee, attack, surrender, give money, call cops, draw weapon, call gang reinforcements
- **Multiple LLM backends** — Claude (Haiku 4.5), Ollama (any local model), ChatGPT, offline stub
- **GUI launcher** — `SAtella_launcher.py` handles config, dependency installs, daemon start/stop, and GTA launch
- **No game file edits** — everything is an external mod; uninstall by removing the files

---

## Architecture

```
GTA San Andreas (CLEO Redux)          Python Daemon
────────────────────────────          ──────────────
Player presses T / Y                 polls request.json (150 ms)
  │                                        │
  ▼                                        ▼
collect NPC context + history    ──►  call LLM backend
write CLEO\SAtella_request.json       synthesize TTS  (piper)
                                      write SAtella_speech.wav
poll SAtella_response.ini (100 ms)◄── write SAtella_response.ini
  │
  ▼
play 3D audio stream from WAV
display speech bubble above NPC head
execute NPC reaction (flee / attack / …)
```

**IPC is purely file-based** — no sockets, no shared memory, just JSON and INI files in the `CLEO\` folder.

---

## File Overview

| File | Purpose |
|------|---------|
| `cleo/SAtella_sa[fs][mem].js` | CLEO Redux script — game-side logic, input, HUD, reactions |
| `SAtella_daemon.py` | Python daemon — LLM calls, TTS synthesis, STT transcription |
| `SAtella_launcher.py` | Tkinter GUI — config, dependency management, daemon control |
| `SAtella_daemon.ini` | Configuration file (game dir, backend, API keys, mic device) |
| `PED_PROFILES.csv` | Per-model personality profiles (pipe-delimited) |
| `CLEO/SAtella_request.json` | IPC: game → daemon (written by JS, read by Python) |
| `CLEO/SAtella_response.ini` | IPC: daemon → game (written by Python, read by JS) |
| `CLEO/SAtella_speech.wav` | TTS audio for the current NPC line |
| `CLEO/SAtella_dialogues.log` | Human-readable log of all exchanges |
| `SAtella_voices/` | Piper voice model files (`.onnx` + `.onnx.json`) |

---

## Prerequisites

**Game:**
- GTA San Andreas **1.0 US** (HOODLUM / compact build)
  - Other builds use different CPed pool addresses — the script will need updating for Steam/Rockstar Launcher versions
- [CLEO 5.4.0](https://cleo.li/) + [CLEO Redux 1.4.3](https://github.com/cleolibrary/CLEO-Redux)

**Python:**
- Python 3.10 or later
- Core dependencies:

```
pip install anthropic faster-whisper sounddevice piper-tts numpy
```

The launcher's **Dependencies** panel can install these for you with a single click.

---

## Installation

1. Copy all project files into your GTA San Andreas folder:
   ```
   Grand Theft Auto San Andreas\
   ├── cleo\
   │   └── SAtella_sa[fs][mem].js
   ├── SAtella_daemon.py
   ├── SAtella_launcher.py
   ├── SAtella_daemon.ini
   ├── PED_PROFILES.csv
   └── SAtella_voices\          ← create this folder
       ├── en_US-arctic-medium.onnx
       └── en_US-arctic-medium.onnx.json
   ```

2. Download the Piper voice model (required for TTS):
   - `en_US-arctic-medium.onnx` and `en_US-arctic-medium.onnx.json`
   - The launcher's **Mod Files** panel has a **↓** download button for these.
   - Or download manually from [rhasspy/piper-voices on HuggingFace](https://huggingface.co/rhasspy/piper-voices).

3. Make sure CLEO Redux has filesystem write permissions. The script filename contains `[fs]` which grants this automatically.

---

## Quick Start

### Via the GUI launcher (recommended)

```
python SAtella_launcher.py
```

1. Set **Game Dir** to your GTA SA folder.
2. Choose a **Backend** and enter your API key (or pick Ollama / stub for offline use).
3. Click **▶ Start Daemon**.
4. Click **🎮 Launch GTA SA** (or start the game manually).
5. In-game: walk up to any pedestrian and press **T**.

### Via command line

```powershell
# Claude (requires ANTHROPIC_API_KEY)
$env:ANTHROPIC_API_KEY="sk-ant-..."
python SAtella_daemon.py --game-dir "E:\SteamLibrary\steamapps\common\Grand Theft Auto San Andreas"

# Ollama (local, no key needed)
python SAtella_daemon.py --backend ollama --ollama-model llama3.2 --game-dir "..."

# ChatGPT
$env:OPENAI_API_KEY="sk-..."
python SAtella_daemon.py --backend chatgpt --game-dir "..."

# Offline stub (for testing)
python SAtella_daemon.py --backend stub --game-dir "..."
```

---

## Configuration

`SAtella_daemon.ini` stores all settings. The launcher reads and writes this file automatically.

```ini
[daemon]
game_dir = C:\SteamLibrary\steamapps\common\Grand Theft Auto San Andreas
backend  = claude          ; claude | ollama | chatgpt | stub

[claude]
api_key  =                 ; or set ANTHROPIC_API_KEY env var
model    = claude-haiku-4-5-20251001

[ollama]
model    = gemma3:4b
url      = http://localhost:11434

[chatgpt]
api_key  =                 ; or set OPENAI_API_KEY env var
model    = gpt-4o-mini

[stt]
input_device_id   =        ; sounddevice device index; empty = system default
input_device_name =
```

---

## LLM Backends

| Backend | Model | Notes |
|---------|-------|-------|
| `claude` | Claude Haiku 4.5 | Best role-play quality; requires Anthropic API key |
| `ollama` | any local model | Free, private, runs on your GPU/CPU; requires [Ollama](https://ollama.com) |
| `chatgpt` | gpt-4o-mini (default) | Requires OpenAI API key |
| `stub` | — | Offline; returns scripted responses for testing |

Claude Haiku 4.5 is the recommended backend. Legacy Haiku 3 is significantly weaker at role-playing and instruction-following.

---

## In-Game Controls

| Key | Action |
|-----|--------|
| **T** | Talk (type your line) |
| **Y** | Voice input — Python records your mic and transcribes locally via Whisper |
| **Enter** | Submit typed text |
| **Backspace** | Delete character; Backspace on empty input cancels |
| **T** again | Continue to the next exchange after the NPC speaks |
| Walk away | Ends the conversation automatically |

The dialogue box shows a blinking cursor while you type. Long inputs scroll automatically (the tail is always visible).

---

## NPC Reactions

The LLM chooses one reaction per turn based on context:

| Reaction | Effect |
|----------|--------|
| `none` | Keeps talking (default) |
| `flee` | NPC panics and runs |
| `walk_away` | NPC calmly walks off |
| `attack` | NPC attacks CJ (melee) |
| `hands_up` | NPC surrenders, freezes |
| `give_money` | Adds $10–$2000 to your wallet |
| `call_cops` | NPC uses phone; +1 wanted star |
| `draw_weapon` | NPC pulls a gun (pistol / uzi / AK-47) |
| `call_gang` | 2 armed gang members spawn behind you |

---

## Ped Profiles (`PED_PROFILES.csv`)

Every pedestrian model ID (1–187+) maps to a character sheet that the daemon injects into the LLM system prompt:

```
model_id | gender | race | age | clothing | profession | character traits | speaker_id
```

- `speaker_id` selects the TTS voice variant (arctic model speakers: `0`=awb, `1`=rms, `2`=slt, `7`=bdl, etc.)
- Models without an entry fall back to a generic "Resident of San Andreas" profile.
- Edit the CSV to customize any NPC's personality.

Full model ID reference: [gtamods.com/wiki/Peds_(GTA_SA)](https://gtamods.com/wiki/Peds_(GTA_SA))

---

## TTS (Text-to-Speech)

- Powered by **Piper TTS** (local, no API needed).
- Voice model: `SAtella_voices/en_US-arctic-medium.onnx` (multi-speaker arctic model).
- Audio plays as **positional 3D sound** attached to the NPC — volume falls off with distance.
- Stage directions wrapped in `*asterisks*` are stripped before synthesis but still render in the speech bubble.
- Disable TTS by setting `TTS_ENABLED = False` in `SAtella_daemon.py`.

Configurable constants at the top of `SAtella_daemon.py`:
```python
TTS_ENABLED       = True
TTS_VOICE         = "SAtella_voices/en_US-arctic-medium.onnx"
TTS_LENGTH_SCALE  = 0.9   # speech rate; < 1.0 = faster
TTS_SPEAKER_ID    = 7     # default speaker (bdl — male American)
```

---

## STT (Speech-to-Text)

- Powered by **faster-whisper** (local Whisper inference) + **sounddevice** for recording.
- Press **Y** to start recording; the daemon records until 0.8 s of silence, then transcribes.
- Whisper model size is configurable (`tiny` / `base` / `small`) — `tiny` (~39 MB) is fast enough for conversational use.
- Select your microphone in the launcher's **Mic Input** dropdown.

Configurable constants:
```python
STT_LANGUAGE   = "en"     # language code; "" = auto-detect
STT_MODEL      = "tiny"   # tiny | base | small
STT_DEVICE     = "cpu"    # cpu | cuda
```

---

## Notes & Known Limitations

- **GTA SA 1.0 US only** — The CPed pool address (`0xB74490`) and `SIZEOF_CPED` stride (`1988`) are hardcoded for this build. Verify with Cheat Engine if you use a different version.
- **CLEO read opcodes** — `READ_STRING_FROM_FILE` (0AD7) and `READ_FROM_FILE` (0ADE) crash at C-level in CLEO 5.4.0; the script uses `std.loadFile` (QuickJS built-in) instead.
- **CLEO Redux 1.4.3** — `File.isOpen` always returns `false` in this version; the script works around it.
- **Response INI encoding** — Written as cp1251 for compatibility with SA's CLEO runtime string reader.
- Long NPC text is split into 110-character chunks because `READ_STRING_FROM_INI_FILE` has a ~120-byte buffer limit.

---

## License

This project is released as-is for modding and educational purposes.
