#!/usr/bin/env python3
"""
SAtella daemon.

Polls CLEO/SAtella/request.json, calls an LLM backend,
and writes the response to SAtella_response.ini.
Run this separately from the game process.

Dependencies (core):
    pip install anthropic faster-whisper sounddevice piper-tts

LLM backends (selected via --backend):
    claude    — Claude Haiku 4.5 via the official Anthropic SDK (pip install anthropic)
                Get your key: https://console.anthropic.com/settings/keys
                  set ANTHROPIC_API_KEY=sk-ant-...    (cmd)
                  $env:ANTHROPIC_API_KEY="sk-ant-..." (PowerShell)

    ollama    — Local Ollama (http://localhost:11434), no extra dependencies.
                Install: https://ollama.com  →  ollama pull llama3.2
                  python SAtella_daemon.py --backend ollama --ollama-model llama3.2 ...

    chatgpt   — OpenAI ChatGPT API, no extra dependencies.
                  set OPENAI_API_KEY=sk-...    (cmd)
                  $env:OPENAI_API_KEY="sk-..."  (PowerShell)
                  python SAtella_daemon.py --backend chatgpt --chatgpt-model gpt-4o-mini ...

    stub      — Offline stub for testing without an API key.

Usage examples:
    python SAtella_daemon.py --game-dir "E:\\SteamLibrary\\steamapps\\common\\Grand Theft Auto San Andreas"
    python SAtella_daemon.py --backend ollama --game-dir "..."
    python SAtella_daemon.py --backend chatgpt --game-dir "..."
"""
from __future__ import annotations
import argparse
import configparser
import csv
import datetime
import json
import os
import re
import time
import wave
from pathlib import Path

# ───────────── TTS settings (piper-tts) ─────────────
TTS_ENABLED = True
# Path to the .onnx voice file, relative to --game-dir.
# Download: https://github.com/rhasspy/piper/releases  (need both .onnx and .onnx.json)
TTS_VOICE = "SAtella_voices/en_US-arctic-medium.onnx"
# Speech rate: 1.0 = normal, 0.9 = slightly faster (street speech feel).
TTS_LENGTH_SCALE = 0.9
# Voice ID for multi-speaker models (arctic: 0=awb 1=rms 2=slt 7=bdl).
# Set to None for single-speaker models.
TTS_SPEAKER_ID: "int | None" = 7   # bdl — male, American; fits GTA SA aesthetic

# ───────────── STT settings (faster-whisper + sounddevice) ─────────────
# Language code: "en", "ru", "es", etc.  Empty string = auto-detect.
STT_LANGUAGE = "en"
# Model size: "tiny" (39 MB) / "base" (74 MB) / "small" (244 MB).
STT_MODEL = "tiny"
# Inference device: "cpu" or "cuda" (NVIDIA GPU). int8 is faster than float32 on CPU.
STT_DEVICE = "cpu"
STT_COMPUTE_TYPE = "int8"
# RMS silence threshold (0.0–1.0). Lower this value for quiet microphones.
STT_SILENCE_THRESHOLD = 0.02
# Seconds of silence after speech that signals end of recording.
STT_SILENCE_DURATION = 0.8
# Maximum recording length per turn in seconds.
STT_MAX_DURATION = 12.0
# Sample rate in Hz (Whisper requires 16 kHz).
STT_SAMPLERATE = 16000
# Input device index for sounddevice. None = system default.
# Set via --stt-device-id or configured automatically by a launcher.
STT_INPUT_DEVICE: "int | None" = None

# ───────────── Ped model profiles ─────────────
# Full model ID list: https://gtamods.com/wiki/Peds_(GTA_SA)
# Edit PED_PROFILES.csv — pipe-delimited (|)
# Columns: model_id|gender|race|age|clothing|profession|character traits and behavior|speaker_id
# arctic speaker IDs: 0=awb(Scottish♂) 1=rms(American♂) 2=slt(American♀)
#                     4=aew(American♂) 7=bdl(American♂ deep) 8=clb(American♀)

def _load_ped_profiles() -> "dict[int, dict]":
    """Load ped personality profiles from PED_PROFILES.csv."""
    csv_path = Path(__file__).parent / "PED_PROFILES.csv"
    profiles: dict[int, dict] = {}
    try:
        with open(csv_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter="|"):
                sid = row["speaker_id"].strip()
                if not sid.isdigit():
                    print(f"[profiles] SKIP model_id={row['model_id']!r}: missing speaker_id")
                    continue
                profiles[int(row["model_id"])] = {
                    "gender":     row["gender"].strip(),
                    "race":       row["race"].strip(),
                    "age":        row["age"].strip(),
                    "clothing":   row["clothing"].strip(),
                    "profession": row["profession"].strip(),
                    "traits":     row["character traits and behavior"].strip(),
                    "speaker_id": int(sid),
                }
        print(f"[profiles] Loaded {len(profiles)} ped profiles from PED_PROFILES.csv")
    except FileNotFoundError:
        print(f"[profiles] WARNING: PED_PROFILES.csv not found at {csv_path} — using empty profiles")
    return profiles


PED_PROFILES: "dict[int, dict]" = _load_ped_profiles()

DEFAULT_PROFILE: dict = {
    "gender": "Unknown", "race": "Unknown", "age": "Unknown",
    "clothing": "casual street clothes",
    "profession": "Resident of San Andreas",
    "traits": "An ordinary citizen going about their day.",
    "speaker_id": TTS_SPEAKER_ID,
}

# ───────────── System prompt ─────────────
SYSTEM_PROMPT = """You are a random NPC in GTA San Andreas (California, 1992, gangsta rap era, crack epidemic, racial tension).
CJ (young black guy, former Grove Street Families member) has approached you.
If you from gangs (Ballas, Vagos, etc.) — you know CJ by sight and may have history with him.

Respond ONLY with a valid JSON object — no text outside it:
{"text": "1-2 short lines of dialogue", "reaction": "REACTION", "reaction_value": NUMBER}

Dialogue rules:
- Stay fully in character. No meta-info ("I'm AI", "as NPC").
- English only, Latin characters, no emojis.
- Account for time of day, wanted level (>0 = nervous/hostile), and conversation history.
- You can use profanity.

Reactions — choose ONE that fits what CJ just said and your personality:
- "none"        keep talking (default)
- "flee"        run away in panic
- "walk_away"   calmly leave, done talking
- "attack"      fight CJ (melee or weapon)
- "hands_up"    surrender, freeze with hands raised
- "give_money"  hand over cash  (reaction_value = dollars, 10-2000)
- "call_cops"   call 911, CJ gets +1 wanted star
- "draw_weapon" pull a gun on CJ  (reaction_value: 22=pistol 28=uzi 30=ak47)
- "call_gang"   signal crew, 2 gang members spawn behind CJ
"""


_tts_voice = None
_tts_voice_path: "Path | None" = None


def _load_tts():
    """Load the Piper voice model once and cache it. Returns a PiperVoice object or None."""
    global _tts_voice
    if _tts_voice is not None:
        return _tts_voice
    if not TTS_ENABLED or _tts_voice_path is None:
        return None
    if not _tts_voice_path.exists():
        print(f"[TTS] Voice model not found: {_tts_voice_path}")
        print("[TTS] Download from: https://github.com/rhasspy/piper/releases")
        print(f"[TTS] Required files: {_tts_voice_path.name} and {_tts_voice_path.name}.json")
        return None
    try:
        from piper.voice import PiperVoice
    except ImportError:
        try:
            from piper import PiperVoice  # type: ignore[no-redef]
        except ImportError:
            print("[TTS] piper1-gpl not installed: pip install piper1-gpl")
            return None
    print(f"[TTS] Loading voice: {_tts_voice_path.name}")
    _tts_voice = PiperVoice.load(str(_tts_voice_path))
    print("[TTS] Voice ready.")
    return _tts_voice


def synthesize_speech(text: str, wav_path: Path, speaker_id: "int | None" = None) -> int:
    """Synthesize text to a WAV file. Returns duration in milliseconds, or 0 on error."""
    voice = _load_tts()
    if voice is None:
        return 0
    # Strip *action* stage directions before synthesis
    spoken = re.sub(r"\*[^*]+\*", "", text).strip()
    if not spoken:
        return 0
    try:
        from piper.voice import SynthesisConfig
        syn_cfg = SynthesisConfig(
            speaker_id=speaker_id if speaker_id is not None else TTS_SPEAKER_ID,
            length_scale=TTS_LENGTH_SCALE,
        )
        with wave.open(str(wav_path), "wb") as wf:
            # synthesize_wav sets channel/sample-width/frame-rate on the first chunk.
            voice.synthesize_wav(spoken, wf, syn_config=syn_cfg)
            sample_rate = wf.getframerate()
            n_frames    = wf.getnframes()
        duration_ms = int(n_frames / sample_rate * 1000)
        print(f"[TTS] {duration_ms} ms  ({n_frames} frames @ {sample_rate} Hz)")
        return duration_ms
    except Exception as e:
        print(f"[TTS] Synthesis error: {e}")
        return 0


_whisper_model = None


def _load_whisper():
    """Load faster-whisper once and cache it. Returns the model or None."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[STT] faster-whisper is not installed.")
        print("[STT]   pip install faster-whisper sounddevice")
        return None
    print(f"[STT] Loading '{STT_MODEL}' on {STT_DEVICE}/{STT_COMPUTE_TYPE} ...")
    print("[STT] (First run will download the model from huggingface.co)")
    _whisper_model = WhisperModel(STT_MODEL, device=STT_DEVICE, compute_type=STT_COMPUTE_TYPE)
    print("[STT] Model ready.")
    return _whisper_model


def transcribe_speech() -> str:
    """Record from the microphone until silence is detected, then transcribe locally.

    Returns the recognized text, or an empty string on error or silence.
    """
    model = _load_whisper()
    if model is None:
        return ""

    try:
        import sounddevice as sd
        import numpy as np
    except ImportError:
        print("[STT] sounddevice is not installed: pip install sounddevice")
        return ""

    chunk_frames = int(STT_SAMPLERATE * 0.1)       # 100 ms per chunk
    silence_need = int(STT_SILENCE_DURATION / 0.1)  # chunks of silence needed to stop
    max_chunks   = int(STT_MAX_DURATION / 0.1)

    print("[STT] Listening...")
    chunks: list = []
    silent_count = 0
    has_speech   = False
    try:
        with sd.InputStream(samplerate=STT_SAMPLERATE, channels=1, dtype="float32",
                            device=STT_INPUT_DEVICE) as stream:
            for _ in range(max_chunks):
                data, _ = stream.read(chunk_frames)
                chunks.append(data.copy())
                rms = float(np.sqrt(np.mean(data ** 2)))
                if rms > STT_SILENCE_THRESHOLD:
                    has_speech   = True
                    silent_count = 0
                elif has_speech:
                    silent_count += 1
                    if silent_count >= silence_need:
                        break
    except Exception as e:
        print(f"[STT] Recording error: {e}")
        return ""

    if not has_speech:
        print("[STT] No speech detected")
        return ""

    print("[STT] Transcribing...")
    audio = np.concatenate(chunks).flatten()
    try:
        lang = STT_LANGUAGE or None
        segs, _ = model.transcribe(audio, language=lang, beam_size=1, best_of=1,
                                   condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segs).strip()
        print(f"[STT] Result: {text!r}")
        return text
    except Exception as e:
        print(f"[STT] Transcription error: {e}")
        return ""


def build_system_prompt(req: dict) -> str:
    """Build a system prompt that includes the personality profile of the specific NPC."""
    model_id = req["npc"]["model_id"]
    p = PED_PROFILES.get(model_id, DEFAULT_PROFILE)
    character_block = (
        f"## Your character\n"
        f"Role: {p['profession']}\n"
        f"Appearance: {p['gender']}, {p['race']}, {p['age']}\n"
        f"Clothing: {p['clothing']}\n"
        f"Personality & background: {p['traits']}"
    )
    return SYSTEM_PROMPT.rstrip() + "\n\n" + character_block


def get_npc_speaker_id(model_id: int) -> int:
    """Return the TTS speaker_id for the given NPC model ID."""
    return PED_PROFILES.get(model_id, DEFAULT_PROFILE)["speaker_id"]


def build_messages(req: dict) -> list[dict]:
    """Build the message list for the LLM API from conversation history + current turn.

    History format from CLEO JS: [{role: "player"|"npc", text: "..."}]
    Always an even number of entries (player+npc per completed turn).
    The current player message is passed separately in req["player"]["text"].
    """
    history: list[dict] = req.get("history", [])
    hour = req["world"]["hour"]
    wanted = req["player"]["wanted_level"]

    messages = []
    for entry in history:
        role = "user" if entry["role"] == "player" else "assistant"
        messages.append({"role": role, "content": entry["text"]})

    # Inject time-of-day and wanted level into the current player turn
    ctx = f"[{hour:02d}:00 | wanted {wanted}/6]\nCJ: {req['player']['text']}"
    messages.append({"role": "user", "content": ctx})
    return messages


# ───────────── LLM backends ─────────────

# Claude Haiku 4.5 is the cheapest current Claude model.
# Legacy Haiku 3 ("claude-3-haiku-20240307", $0.25/$1.25 per M tokens) is significantly
# weaker at role-playing and instruction following, so Haiku 4.5 is preferred.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

OLLAMA_DEFAULT_MODEL  = "gemma3:4b"
OLLAMA_DEFAULT_URL    = "http://localhost:11434"
CHATGPT_DEFAULT_MODEL = "gpt-4o-mini"


def _parse_npc_response(raw: str) -> dict:
    """Extract the JSON payload from an LLM response. Falls back to reaction=none on any error."""
    text = raw.strip()
    # Strip markdown code fences (```json ... ```)
    if "```" in text:
        for part in text.split("```"):
            s = part.strip().lstrip("json").strip()
            if s.startswith("{"):
                text = s
                break
    # Isolate the first {...} block in case the model added extra text around it
    start, end = text.find("{"), text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]
    try:
        data = json.loads(text)
        return {
            "text":           str(data.get("text", raw)).strip() or "*ped does not respond*",
            "reaction":       str(data.get("reaction", "none")).strip(),
            "reaction_value": int(data.get("reaction_value", 0)),
        }
    except (json.JSONDecodeError, ValueError):
        return {"text": raw or "*ped does not respond*", "reaction": "none", "reaction_value": 0}


def call_claude(req: dict, *, api_key: str, model: str = CLAUDE_MODEL) -> dict:
    """Call the Claude API via the official Anthropic SDK."""
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)

    turns = len(req.get("history", [])) // 2
    print(f"  history={turns} turn(s)  player={req['player']['text'][:50]!r}")
    resp = client.messages.create(
        model=model,
        max_tokens=300,
        system=build_system_prompt(req),
        messages=build_messages(req),
        temperature=0.9,
    )
    raw = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    result = _parse_npc_response(raw)
    print(f"  reaction={result['reaction']}({result['reaction_value']})  text={result['text'][:60]!r}")
    return result


def _call_openai_compat(req: dict, *, base_url: str, api_key: str, model: str) -> dict:
    """Shared helper for OpenAI-compatible endpoints (Ollama /v1/ and ChatGPT)."""
    import urllib.request
    import urllib.error

    messages = [{"role": "system", "content": build_system_prompt(req)}] + build_messages(req)
    turns = len(req.get("history", [])) // 2
    print(f"  history={turns} turn(s)  player={req['player']['text'][:50]!r}")

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.9,
    }).encode()
    url = base_url.rstrip("/") + "/v1/chat/completions"
    http_req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e

    raw = data["choices"][0]["message"]["content"].strip()
    result = _parse_npc_response(raw)
    print(f"  reaction={result['reaction']}({result['reaction_value']})  text={result['text'][:60]!r}")
    return result


def call_ollama(req: dict, model: str, url: str = OLLAMA_DEFAULT_URL) -> dict:
    """Call a local Ollama instance via its OpenAI-compatible endpoint."""
    return _call_openai_compat(req, base_url=url, api_key="ollama", model=model)


def call_chatgpt(req: dict, model: str, api_key: str) -> dict:
    """Call the OpenAI ChatGPT API."""
    return _call_openai_compat(req, base_url="https://api.openai.com", api_key=api_key, model=model)


def call_stub(req: dict) -> dict:
    """Offline stub backend for testing without an API key."""
    profile = PED_PROFILES.get(req["npc"]["model_id"], DEFAULT_PROFILE)
    hour    = req["world"]["hour"]
    wanted  = req["player"]["wanted_level"]
    turns   = len(req.get("history", [])) // 2
    if turns > 0:
        return {"text": f"(turn {turns+1}) Yeah we still talking. What else?",
                "reaction": "none", "reaction_value": 0}
    if wanted >= 3:
        return {"text": "Man you got cops on you! Get away from me!",
                "reaction": "flee", "reaction_value": 0}
    if hour < 6 or hour >= 22:
        return {"text": "Late night, homie. I'm out.",
                "reaction": "walk_away", "reaction_value": 0}
    return {"text": f"Yo CJ. [{profile['profession']}] What you want?",
            "reaction": "none", "reaction_value": 0}


# ───────────── Response writing / logging ─────────────

def _log_dialogue(log_path: Path, rid: int, npc_model: int,
                  player_text: str, npc_text: str,
                  reaction: str, reaction_value: int) -> None:
    """Append a dialogue exchange to the human-readable log file."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"[{ts}] req#{rid} ped={npc_model}\n"
        f"  CJ:  {player_text}\n"
        f"  NPC: {npc_text}\n"
        f"  reaction={reaction}({reaction_value})\n"
        "---\n"
    )
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as e:
        print(f"[log] Write error: {e}")


# Maximum characters per INI text chunk (CLEO string read limit).
_INI_CHUNK_SIZE = 110


def _write_ini(path: Path, rid: int, *, player_text: str, text: str,
               reaction: str, reaction_value: int, speech_duration_ms: int = 0) -> None:
    """Atomically write SAtella_response.ini via a temp file to avoid partial reads by CLEO."""
    text_clean = text.replace("\r", "").replace("\n", " ").strip()
    chunks = [text_clean[i:i + _INI_CHUNK_SIZE] for i in range(0, len(text_clean), _INI_CHUNK_SIZE)]
    player_text_safe = player_text.replace("\r", "").replace("\n", " ").strip()[:_INI_CHUNK_SIZE]
    lines = [
        "[SAtella]",
        f"id={rid}",
        f"player_text={player_text_safe}",
        f"speech_duration_ms={speech_duration_ms}",
        f"text_chunks={len(chunks)}",
        f"reaction={reaction}",
        f"reaction_value={reaction_value}",
    ]
    for i, chunk in enumerate(chunks):
        lines.append(f"text{i}={chunk}")
    ini_content = "\n".join(lines) + "\n"
    tmp = path.with_suffix(".ini.tmp")
    # Write with cp1251 encoding for compatibility with GTA SA's CLEO runtime
    tmp.write_text(ini_content, encoding="cp1251")
    os.replace(tmp, path)


# ───────────── Main loop ─────────────

def main():
    # Pre-parse --config before building the full argument parser so that
    # INI defaults are available when registering all other arguments.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=str(Path(__file__).parent / "SAtella_daemon.ini"))
    pre_args, _ = pre.parse_known_args()
    cfg_path = Path(pre_args.config)

    cfg = configparser.ConfigParser()
    if cfg_path.exists():
        cfg.read(cfg_path, encoding="utf-8")
        print(f"[config] Loaded {cfg_path}")
    else:
        print(f"[config] No config file found at {cfg_path}")

    def _c(section: str, key: str, fallback: str = "") -> str:
        """Read a value from the INI config with a fallback."""
        return cfg.get(section, key, fallback=fallback)

    ap = argparse.ArgumentParser(
        description="SAtella daemon. Optional config file: SAtella_daemon.ini",
    )
    ap.add_argument("--config", default=str(cfg_path),
                    help="path to the .ini configuration file")
    ap.add_argument("--game-dir", default=_c("daemon", "game_dir"),
                    help="folder containing gta_sa.exe")
    ap.add_argument("--backend", choices=["claude", "ollama", "chatgpt", "stub"],
                    default=_c("daemon", "backend", "claude"),
                    help="LLM backend to use (default: claude)")
    # Claude
    ap.add_argument("--claude-api-key", default=_c("claude", "api_key"),
                    help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    ap.add_argument("--claude-model", default=_c("claude", "model", CLAUDE_MODEL),
                    help=f"Claude model ID (default: {CLAUDE_MODEL})")
    # Ollama
    ap.add_argument("--ollama-model", default=_c("ollama", "model", OLLAMA_DEFAULT_MODEL),
                    help=f"Ollama model name (default: {OLLAMA_DEFAULT_MODEL})")
    ap.add_argument("--ollama-url", default=_c("ollama", "url", OLLAMA_DEFAULT_URL),
                    help=f"Ollama base URL (default: {OLLAMA_DEFAULT_URL})")
    # ChatGPT
    ap.add_argument("--chatgpt-model", default=_c("chatgpt", "model", CHATGPT_DEFAULT_MODEL),
                    help=f"OpenAI model name (default: {CHATGPT_DEFAULT_MODEL})")
    ap.add_argument("--openai-api-key", default=_c("chatgpt", "api_key"),
                    help="OpenAI API key (or set OPENAI_API_KEY env var)")
    # STT
    ap.add_argument("--stt-device-id", type=int, default=None,
                    help="sounddevice input device index (default: system default)")
    args = ap.parse_args()

    if not args.game_dir:
        ap.error("--game-dir is required (or set game_dir in SAtella_daemon.ini)")

    ipc_dir = Path(args.game_dir) / "CLEO"
    ipc_dir.mkdir(parents=True, exist_ok=True)
    req_path      = ipc_dir / "SAtella_request.json"
    resp_ini_path = ipc_dir / "SAtella_response.ini"
    log_path      = ipc_dir / "SAtella_dialogues.log"

    global _tts_voice_path, STT_INPUT_DEVICE
    _tts_voice_path  = Path(args.game_dir) / TTS_VOICE
    STT_INPUT_DEVICE = args.stt_device_id

    if args.backend == "claude":
        claude_key = args.claude_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not claude_key:
            ap.error("--claude-api-key or ANTHROPIC_API_KEY env var is required for the claude backend")
        backend    = lambda req: call_claude(req, api_key=claude_key, model=args.claude_model)
        model_name = args.claude_model
    elif args.backend == "ollama":
        backend    = lambda req: call_ollama(req, model=args.ollama_model, url=args.ollama_url)
        model_name = args.ollama_model
    elif args.backend == "chatgpt":
        openai_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        if not openai_key:
            ap.error("--openai-api-key or OPENAI_API_KEY env var is required for the chatgpt backend")
        backend    = lambda req: call_chatgpt(req, args.chatgpt_model, openai_key)
        model_name = args.chatgpt_model
    else:
        backend    = call_stub
        model_name = "n/a"

    print(f"SAtella daemon: watching {ipc_dir} (backend={args.backend}, model={model_name})")
    print(f"[log] Dialogues → {log_path}")

    # Pre-warm models so the first T/Y keypress in-game doesn't stall
    _load_whisper()
    _load_tts()

    last_id: int | None = None
    while True:
        time.sleep(0.15)
        try:
            raw = req_path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        if rid is None or rid == last_id:
            continue
        last_id = rid

        turns = len(req.get("history", [])) // 2
        mode  = req.get("mode", "text")
        print(f"[req #{rid}] ped={req['npc']['model_id']} "
              f"hour={req['world']['hour']} history={turns}t mode={mode}")

        # Voice input: record and transcribe before calling the LLM
        player_text = req["player"].get("text", "")
        if mode == "stt":
            player_text = transcribe_speech()
            req["player"]["text"] = player_text
            print(f"[req #{rid}] STT result: {player_text!r}")
            if not player_text.strip():
                # Nothing heard — write an empty response so CLEO exits its wait state
                _write_ini(resp_ini_path, rid, player_text="", text="", reaction="none", reaction_value=0)
                print(f"[req #{rid}] STT empty — wrote empty response")
                continue

        try:
            result = backend(req)
        except Exception as e:
            print(f"[req #{rid}] backend error: {e}")
            result = {"text": "*ped does not respond*", "reaction": "none", "reaction_value": 0}

        text           = result.get("text", "*ped does not respond*")
        reaction       = result.get("reaction", "none")
        reaction_value = result.get("reaction_value", 0)
        print(f"[req #{rid}] reaction={reaction}({reaction_value})")

        # Generate TTS audio before writing the INI so the WAV is ready when CLEO opens it
        speech_wav  = ipc_dir / "SAtella_speech.wav"
        npc_speaker = get_npc_speaker_id(req["npc"]["model_id"])
        speech_ms   = synthesize_speech(text, speech_wav, speaker_id=npc_speaker)
        if speech_ms:
            print(f"[req #{rid}] TTS: {speech_ms} ms")

        _write_ini(resp_ini_path, rid, player_text=player_text, text=text,
                   reaction=reaction, reaction_value=reaction_value,
                   speech_duration_ms=speech_ms)
        _log_dialogue(log_path, rid, req["npc"]["model_id"], player_text, text, reaction, reaction_value)
        print(f"[req #{rid}] wrote response to INI")


if __name__ == "__main__":
    main()
