#!/usr/bin/env python3
"""SAtella Launcher — GUI for managing the SAtella daemon."""
from __future__ import annotations
import configparser
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import threading
import tempfile
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

# ── VS Code dark color palette ────────────────────────────────────────────────
BG     = "#1e1e1e"
PANEL  = "#252526"
INPUT  = "#3c3c3c"
BORDER = "#454545"
FG     = "#d4d4d4"
DIM    = "#858585"
ACCENT = "#007acc"
GREEN  = "#4ec9b0"
RED    = "#f44747"
BTN_BG = "#0e639c"
BTN_FG = "#ffffff"

DAEMON_SCRIPT = Path(__file__).parent / "SAtella_daemon.py"
CONFIG_PATH   = Path(__file__).parent / "SAtella_daemon.ini"
GTA_STEAM_ID  = "12120"

# Only third-party packages that require pip install.
# stdlib modules are always available and excluded intentionally.
DEPS = [
    ("anthropic",      "anthropic"),
    ("piper-tts",      "piper"),
    ("faster-whisper", "faster_whisper"),
    ("sounddevice",    "sounddevice"),
    ("numpy",          "numpy"),
]

CLAUDE_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-8",
    "claude-3-haiku-20240307",
]
CHATGPT_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]

# Mod file manifest: (display name, path relative to game_dir, raw download URL or None)
# Set URL to None for files that must be placed manually.
MOD_FILES: list[tuple[str, str, "str | None"]] = [
    ("SAtella_sa[fs][mem].js", "cleo/SAtella_sa[fs][mem].js",                      None),
    ("PED_PROFILES.csv",       "PED_PROFILES.csv",                                  "https://raw.githubusercontent.com/ne-ogoz/satella/refs/heads/main/PED_PROFILES.csv"),
    ("SAtella_daemon.py",      "SAtella_daemon.py",                                 None),
    ("SAtella_launcher.py",    "SAtella_launcher.py",                               None),
    ("SAtella_daemon.ini",     "SAtella_daemon.ini",                                None),
    ("arctic.onnx",            "SAtella_voices/en_US-arctic-medium.onnx",           "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx?download=true"),
    ("arctic.onnx.json",       "SAtella_voices/en_US-arctic-medium.onnx.json",      "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json?download=true"),
    ("Cleo",                   "CLEO.asi",                                           None),
    ("Cleo Redux",             "cleo_redux.asi",                                    None),
    ("cleo.ini",               "cleo/.config/cleo.ini",                             None),
]

# ── Widget helpers ────────────────────────────────────────────────────────────

def _entry(parent, textvariable, **kw) -> tk.Entry:
    return tk.Entry(parent, textvariable=textvariable,
                    bg=INPUT, fg=FG, insertbackground=FG,
                    relief="flat", highlightthickness=1,
                    highlightbackground=BORDER, highlightcolor=ACCENT, **kw)


def _label(parent, text, dim=False, **kw) -> tk.Label:
    return tk.Label(parent, text=text, bg=BG, fg=DIM if dim else FG, **kw)


def _btn(parent, text, cmd, *, accent=False, small=False, width=None) -> tk.Button:
    kw: dict = dict(
        text=text, command=cmd,
        bg=BTN_BG if accent else INPUT,
        fg=BTN_FG,
        activebackground=ACCENT if accent else BORDER,
        activeforeground=BTN_FG,
        relief="flat", bd=0, cursor="hand2",
        padx=6 if small else 10,
        pady=2 if small else 5,
        font=("Segoe UI", 8 if small else 9),
    )
    if width:
        kw["width"] = width
    return tk.Button(parent, **kw)


def _sep(parent):
    tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=0, pady=3)


def _make_icon_data() -> bytes:
    """Build a 32×32 RGBA ICO file entirely in memory (struct + zlib, no PIL)."""
    import struct, zlib

    W = H = 32
    _BG   = (0x1e, 0x1e, 0x1e, 0xff)
    BLUE  = (0x00, 0x7a, 0xcc, 0xff)
    WHITE = (0xd4, 0xd4, 0xd4, 0xff)

    grid = [[_BG] * W for _ in range(H)]

    def fill(color: tuple, x1: int, y1: int, x2: int, y2: int) -> None:
        for y in range(max(0, y1), min(H, y2)):
            for x in range(max(0, x1), min(W, x2)):
                grid[y][x] = color

    # Speech bubble body
    fill(BLUE, 2, 2, W - 2, H - 9)
    # Rounded corners (clip 2 px)
    for cx, cy in ((2, 2), (W - 4, 2), (2, H - 11), (W - 4, H - 11)):
        fill(_BG, cx, cy, cx + 2, cy + 2)
    # Bubble tail (staircase down-left)
    fill(BLUE,  7, H - 9, 14, H - 7)
    fill(BLUE,  7, H - 7, 11, H - 5)
    fill(BLUE,  7, H - 5,  9, H - 3)
    # Three text lines inside the bubble
    fill(WHITE, 6,  8, W -  6, 10)
    fill(WHITE, 6, 13, W -  6, 15)
    fill(WHITE, 6, 18, W - 12, 20)

    def png_chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)   # RGBA, 8-bit
    raw  = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in grid)
    png  = (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(raw))
            + png_chunk(b"IEND", b""))

    # ICO wrapper: 6-byte file header + 16-byte directory entry + PNG data
    offset = 6 + 16
    header = struct.pack("<HHH", 0, 1, 1)
    entry  = struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png), offset)
    return header + entry + png


def _make_icon(size: int = 32) -> tk.PhotoImage:
    """Draw the app icon in memory — no external asset files required.
    Speech bubble: dark background, blue body, three white text lines.
    """
    img = tk.PhotoImage(width=size, height=size)

    def p(color: str, x1: int, y1: int, x2: int, y2: int) -> None:
        img.put(color, to=(x1, y1, x2, y2))

    p("#1e1e1e", 0, 0, size, size)                              # background
    p("#007acc", 2, 2, size - 2, size - 9)                      # bubble body
    for cx, cy in ((2, 2), (size - 4, 2), (2, size - 11), (size - 4, size - 11)):
        p("#1e1e1e", cx, cy, cx + 2, cy + 2)                    # rounded corners
    p("#007acc", 7, size - 9, 14, size - 7)                     # tail row 1
    p("#007acc", 7, size - 7, 11, size - 5)                     # tail row 2
    p("#007acc", 7, size - 5,  9, size - 3)                     # tail row 3
    p("#d4d4d4", 6,      8, size - 6,  10)                      # long text line
    p("#d4d4d4", 6,     13, size - 6,  15)                      # long text line
    p("#d4d4d4", 6,     18, size - 12, 20)                      # short text line

    return img


# ── Main application ──────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SAtella Launcher")
        self.configure(bg=BG)
        self.resizable(True, False)
        self.minsize(580, 0)

        # Generate the ICO in memory and write to a temp file for wm_iconbitmap.
        # This avoids shipping a separate asset file with the mod.
        ico_fd, self._ico_tmp = tempfile.mkstemp(suffix=".ico", prefix="satella_")
        os.write(ico_fd, _make_icon_data())
        os.close(ico_fd)
        self.wm_iconbitmap(default=self._ico_tmp)   # title bar icon
        self._icon = _make_icon(32)
        self.wm_iconphoto(True, self._icon)          # child dialogs (filedialog, etc.)
        self.after_idle(self._apply_taskbar_icon)    # taskbar requires explicit WM_SETICON

        self._daemon_proc: subprocess.Popen | None = None
        self._cfg = configparser.ConfigParser()
        self._dep_labels:   dict[str, tk.Label]  = {}
        self._dep_btns:     dict[str, tk.Button] = {}
        self._mic_ids: list[int | None] = [None]     # None = system default
        self._mod_labels:   dict[str, tk.Label]  = {}
        self._mod_btns:     dict[str, tk.Button] = {}

        self._style_combobox()
        self._build_ui()
        self._load_config()
        self._check_deps()
        self._populate_mics()
        self._check_mod_files()
        self.game_dir_var.trace_add("write", lambda *_: self.after(200, self._check_mod_files))
        self._poll()

    # ── Styles ────────────────────────────────────────────────────────────────

    def _style_combobox(self):
        s = ttk.Style()
        s.theme_use("default")
        s.configure("TCombobox",
                    fieldbackground=INPUT, background=INPUT,
                    foreground=FG, arrowcolor=FG,
                    selectbackground=ACCENT, selectforeground=BTN_FG,
                    borderwidth=0)
        s.map("TCombobox",
              fieldbackground=[("readonly", INPUT)],
              foreground=[("readonly", FG)],
              selectbackground=[("readonly", ACCENT)])

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        P = {"padx": 12, "pady": 5}

        # Game directory picker
        f = tk.Frame(self, bg=BG); f.pack(fill="x", **P)
        _label(f, "Game Dir", dim=True, width=9, anchor="w").pack(side="left")
        self.game_dir_var = tk.StringVar()
        _entry(f, self.game_dir_var).pack(side="left", fill="x", expand=True, padx=(4, 4))
        _btn(f, "…", self._pick_dir, width=3).pack(side="left")

        # AI backend and model selection
        f = tk.Frame(self, bg=BG); f.pack(fill="x", **P)
        _label(f, "Backend", dim=True, width=9, anchor="w").pack(side="left")
        self.backend_var = tk.StringVar(value="claude")
        bc = ttk.Combobox(f, textvariable=self.backend_var,
                          values=["claude", "ollama", "chatgpt", "stub"],
                          state="readonly", width=10)
        bc.pack(side="left", padx=(4, 8))
        bc.bind("<<ComboboxSelected>>", self._on_backend)

        _label(f, "Model", dim=True).pack(side="left")
        self.model_var = tk.StringVar()
        self.model_cb = ttk.Combobox(f, textvariable=self.model_var,
                                     values=CLAUDE_MODELS, state="readonly", width=28)
        self.model_cb.pack(side="left", padx=(4, 0))

        self.refresh_btn = _btn(f, "⟳", self._fetch_ollama, small=True)
        # Shown only when the ollama backend is active

        # API key input (hidden for ollama/stub)
        self.key_row = tk.Frame(self, bg=BG)
        self.key_row.pack(fill="x", **P)
        _label(self.key_row, "API Key", dim=True, width=9, anchor="w").pack(side="left")
        self.api_key_var = tk.StringVar()
        self.key_entry = _entry(self.key_row, self.api_key_var, show="•")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self.key_row, text="Show", variable=self.show_var,
                       bg=BG, fg=DIM, activebackground=BG, activeforeground=FG,
                       selectcolor=INPUT, bd=0, highlightthickness=0,
                       command=lambda: self.key_entry.config(
                           show="" if self.show_var.get() else "•")
                       ).pack(side="left")

        # Microphone device selector
        f = tk.Frame(self, bg=BG); f.pack(fill="x", **P)
        _label(f, "Mic Input", dim=True, width=9, anchor="w").pack(side="left")
        self.mic_var = tk.StringVar(value="Default (system)")
        self.mic_cb = ttk.Combobox(f, textvariable=self.mic_var,
                                   values=["Default (system)"], state="readonly")
        self.mic_cb.pack(side="left", fill="x", expand=True, padx=(4, 4))
        _btn(f, "⟳", self._populate_mics, small=True).pack(side="left")

        _sep(self)

        # Dependencies panel
        _label(self, "Dependencies", dim=True,
               font=("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(2, 0))
        dep_outer = tk.Frame(self, bg=BG)
        dep_outer.pack(fill="x", padx=12, pady=(2, 4))
        dep_row1 = tk.Frame(dep_outer, bg=BG); dep_row1.pack(fill="x", pady=(0, 2))
        dep_row2 = tk.Frame(dep_outer, bg=BG); dep_row2.pack(fill="x")
        dep_half = (len(DEPS) + 1) // 2

        for i, (pip_name, _) in enumerate(DEPS):
            row = dep_row1 if i < dep_half else dep_row2
            cell = tk.Frame(row, bg=BG)
            cell.pack(side="left", padx=(0, 14))
            lbl = tk.Label(cell, text=f"◌ {pip_name}", bg=BG, fg=DIM,
                           font=("Consolas", 9))
            lbl.pack(side="left")
            btn = _btn(cell, "install", lambda p=pip_name: self._install(p), small=True)
            self._dep_labels[pip_name] = lbl
            self._dep_btns[pip_name]   = btn

        _sep(self)

        # Mod files panel
        hdr = tk.Frame(self, bg=BG); hdr.pack(fill="x", padx=12, pady=(2, 0))
        _label(hdr, "Mod Files", dim=True, font=("Segoe UI", 8)).pack(side="left")
        _btn(hdr, "⟳", self._check_mod_files, small=True).pack(side="left", padx=(6, 0))

        mod_outer = tk.Frame(self, bg=BG)
        mod_outer.pack(fill="x", padx=12, pady=(2, 4))
        mod_row1 = tk.Frame(mod_outer, bg=BG); mod_row1.pack(fill="x", pady=(0, 2))
        mod_row2 = tk.Frame(mod_outer, bg=BG); mod_row2.pack(fill="x")
        mod_half = (len(MOD_FILES) + 1) // 2

        for i, (display, rel_path, url) in enumerate(MOD_FILES):
            row = mod_row1 if i < mod_half else mod_row2
            cell = tk.Frame(row, bg=BG)
            cell.pack(side="left", padx=(0, 14))
            lbl = tk.Label(cell, text=f"◌ {display}", bg=BG, fg=DIM,
                           font=("Consolas", 9))
            lbl.pack(side="left")
            if url:
                btn = _btn(cell, "↓", lambda r=rel_path, u=url, n=display: self._download(n, r, u),
                           small=True)
            else:
                btn = _btn(cell, "↓", lambda: None, small=True)
                btn.config(state="disabled", fg=DIM)
            self._mod_labels[display] = lbl
            self._mod_btns[display]   = btn

        _sep(self)

        # Quick test buttons
        f = tk.Frame(self, bg=BG); f.pack(fill="x", padx=12, pady=4)
        _btn(f, "Test Mic (STT)", self._test_mic).pack(side="left", padx=(0, 8))
        _btn(f, "Test Voice (TTS)", self._test_tts).pack(side="left")

        _sep(self)

        # Daemon control buttons
        f = tk.Frame(self, bg=BG); f.pack(fill="x", padx=12, pady=6)
        self.start_btn = _btn(f, "▶  Start Daemon", self._start, accent=True)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = _btn(f, "■  Stop", self._stop)
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.stop_btn.config(state="disabled")
        _btn(f, "🎮  Launch GTA SA", self._launch).pack(side="right")

        _sep(self)

        # Log output area
        self.log = tk.Text(self, bg=PANEL, fg=FG, font=("Consolas", 9),
                           relief="flat", bd=0, state="disabled",
                           height=11, wrap="word", insertbackground=FG)
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log.tag_config("ts",  foreground=DIM)
        self.log.tag_config("ok",  foreground=GREEN)
        self.log.tag_config("err", foreground=RED)
        self.log.tag_config("dim", foreground=DIM)

    # ── Config persistence ────────────────────────────────────────────────────

    def _load_config(self):
        if CONFIG_PATH.exists():
            self._cfg.read(CONFIG_PATH, encoding="utf-8")
        c = lambda s, k, fb="": self._cfg.get(s, k, fallback=fb)

        gd = c("daemon", "game_dir")
        if gd:
            self.game_dir_var.set(gd)

        backend = c("daemon", "backend", "claude")
        self.backend_var.set(backend)
        self._apply_backend(backend, from_config=True)

    def _save_config(self):
        backend = self.backend_var.get()
        if "daemon" not in self._cfg: self._cfg["daemon"] = {}
        self._cfg["daemon"]["game_dir"] = self.game_dir_var.get()
        self._cfg["daemon"]["backend"]  = backend

        key   = self.api_key_var.get().strip()
        model = self.model_var.get()
        if backend == "claude":
            if "claude"  not in self._cfg: self._cfg["claude"]  = {}
            self._cfg["claude"]["api_key"] = key
            self._cfg["claude"]["model"]   = model
        elif backend == "chatgpt":
            if "chatgpt" not in self._cfg: self._cfg["chatgpt"] = {}
            self._cfg["chatgpt"]["api_key"] = key
            self._cfg["chatgpt"]["model"]   = model
        elif backend == "ollama":
            if "ollama"  not in self._cfg: self._cfg["ollama"]  = {}
            self._cfg["ollama"]["model"] = model

        dev_id = self._selected_mic_id()
        if "stt" not in self._cfg: self._cfg["stt"] = {}
        self._cfg["stt"]["input_device_id"]   = str(dev_id) if dev_id is not None else ""
        self._cfg["stt"]["input_device_name"] = self.mic_var.get() if dev_id is not None else ""

        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            self._cfg.write(fh)
        self._log("Config saved.", tag="dim")

    # ── Backend switching ─────────────────────────────────────────────────────

    def _on_backend(self, *_):
        self._apply_backend(self.backend_var.get())

    def _apply_backend(self, backend: str, from_config: bool = False):
        c = lambda s, k, fb="": self._cfg.get(s, k, fallback=fb)

        needs_key = backend in ("claude", "chatgpt")
        self.key_entry.config(state="normal" if needs_key else "disabled",
                              fg=FG if needs_key else BORDER)
        if needs_key:
            self.key_row.pack(fill="x", padx=12, pady=5)
        else:
            self.key_row.pack_forget()

        self.refresh_btn.pack_forget()

        if backend == "claude":
            self.model_cb.config(values=CLAUDE_MODELS, state="readonly")
            saved = c("claude", "model", CLAUDE_MODELS[0])
            self.model_var.set(saved if saved in CLAUDE_MODELS else CLAUDE_MODELS[0])
            self.api_key_var.set(c("claude", "api_key"))
        elif backend == "ollama":
            self.model_cb.config(values=[], state="readonly")
            self.model_var.set(c("ollama", "model", ""))
            self.api_key_var.set("")
            self.refresh_btn.pack(side="left", padx=(4, 0))
            self._fetch_ollama()
        elif backend == "chatgpt":
            self.model_cb.config(values=CHATGPT_MODELS, state="readonly")
            saved = c("chatgpt", "model", CHATGPT_MODELS[0])
            self.model_var.set(saved if saved in CHATGPT_MODELS else CHATGPT_MODELS[0])
            self.api_key_var.set(c("chatgpt", "api_key"))
        else:  # stub — no API needed
            self.model_cb.config(values=["n/a"], state="disabled")
            self.model_var.set("n/a")
            self.api_key_var.set("")

    def _fetch_ollama(self):
        def _run():
            try:
                url = self._cfg.get("ollama", "url", fallback="http://localhost:11434")
                with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as r:
                    data = json.loads(r.read())
                models = [m["name"] for m in data.get("models", [])]
                if models:
                    self.after(0, lambda: self._set_ollama_models(models))
                else:
                    self.after(0, lambda: self._log("Ollama: no models found. Run: ollama pull gemma3:4b", tag="err"))
            except Exception as e:
                self.after(0, lambda: self._log(f"Ollama unreachable: {e}", tag="err"))
        threading.Thread(target=_run, daemon=True).start()

    def _set_ollama_models(self, models: list[str]):
        saved = self._cfg.get("ollama", "model", fallback="")
        self.model_cb.config(values=models)
        self.model_var.set(saved if saved in models else models[0])
        self._log(f"Ollama: {len(models)} model(s) available.", tag="ok")

    # ── Microphone device ─────────────────────────────────────────────────────

    def _populate_mics(self):
        def _run():
            try:
                import sounddevice as sd
                devs = sd.query_devices()
                inputs = [(i, d["name"]) for i, d in enumerate(devs)
                          if d["max_input_channels"] > 0]
                self.after(0, lambda: self._set_mics(inputs))
            except ImportError:
                pass  # sounddevice not installed yet — the dep panel will show it
            except Exception as e:
                self.after(0, lambda: self._log(f"Mic list error: {e}", tag="err"))
        threading.Thread(target=_run, daemon=True).start()

    def _set_mics(self, inputs: list[tuple[int, str]]):
        self._mic_ids = [None] + [idx for idx, _ in inputs]
        names = ["Default (system)"] + [f"[{idx}] {name}" for idx, name in inputs]
        self.mic_cb.config(values=names)

        saved = self._cfg.get("stt", "input_device_name", fallback="")
        selected = 0
        if saved:
            for i, n in enumerate(names):
                if saved in n:
                    selected = i
                    break
        self.mic_var.set(names[selected] if names else "Default (system)")

    def _selected_mic_id(self) -> "int | None":
        try:
            idx = self.mic_cb.current()
            return self._mic_ids[idx] if 0 <= idx < len(self._mic_ids) else None
        except Exception:
            return None

    # ── Dependency management ─────────────────────────────────────────────────

    def _check_deps(self):
        for pip_name, mod_name in DEPS:
            ok = importlib.util.find_spec(mod_name) is not None
            lbl = self._dep_labels[pip_name]
            btn = self._dep_btns[pip_name]
            if ok:
                lbl.config(text=f"✓ {pip_name}", fg=GREEN)
                btn.pack_forget()
            else:
                lbl.config(text=f"✗ {pip_name}", fg=RED)
                btn.pack(side="left", padx=(3, 0))

    def _install(self, pip_name: str):
        self._log(f"pip install {pip_name} …", tag="dim")
        def _run():
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", pip_name],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if r.returncode == 0:
                    self.after(0, lambda: self._log(f"{pip_name} installed OK.", tag="ok"))
                else:
                    self.after(0, lambda: self._log(r.stderr.strip()[:300], tag="err"))
            except Exception as e:
                self.after(0, lambda: self._log(str(e), tag="err"))
            self.after(0, self._check_deps)
        threading.Thread(target=_run, daemon=True).start()

    # ── Mod file status ───────────────────────────────────────────────────────

    def _check_mod_files(self):
        gd = self.game_dir_var.get().strip()
        for display, rel_path, url in MOD_FILES:
            lbl = self._mod_labels.get(display)
            btn = self._mod_btns.get(display)
            if lbl is None:
                continue
            if not gd:
                lbl.config(text=f"◌ {display}", fg=DIM)
                if btn: btn.pack_forget()
                continue
            exists = (Path(gd) / rel_path).exists()
            if exists:
                lbl.config(text=f"✓ {display}", fg=GREEN)
                if btn: btn.pack_forget()
            else:
                lbl.config(text=f"✗ {display}", fg=RED)
                if btn: btn.pack(side="left", padx=(3, 0))

    def _download(self, display: str, rel_path: str, url: str):
        gd = self.game_dir_var.get().strip()
        if not gd:
            self._log("Set Game Dir first.", tag="err")
            return
        dest = Path(gd) / rel_path
        self._log(f"Downloading {display}…", tag="dim")
        btn = self._mod_btns.get(display)
        if btn: btn.config(state="disabled")

        def _run():
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(url, timeout=30) as r:
                    total = int(r.headers.get("Content-Length", 0))
                    chunk_size = 65536
                    downloaded = 0
                    with open(dest, "wb") as f:
                        while True:
                            chunk = r.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                pct = downloaded * 100 // total
                                self.after(0, lambda p=pct, n=display:
                                           self._log(f"  {n}: {p}%", tag="dim"))
                self.after(0, lambda: self._log(f"{display} downloaded.", tag="ok"))
            except Exception as e:
                self.after(0, lambda: self._log(f"Download error: {e}", tag="err"))
                if btn: self.after(0, lambda: btn.config(state="normal"))
            self.after(0, self._check_mod_files)
        threading.Thread(target=_run, daemon=True).start()

    # ── Hardware tests ────────────────────────────────────────────────────────

    def _test_mic(self):
        dev_id = self._selected_mic_id()
        label  = self.mic_var.get()
        self._log(f"Recording 5 s via «{label}»… speak now.", tag="dim")
        def _run():
            try:
                import numpy as np
                import sounddevice as sd
                from faster_whisper import WhisperModel
                audio = sd.rec(int(5 * 16000), samplerate=16000,
                               channels=1, dtype="float32", device=dev_id)
                sd.wait()
                mdl = WhisperModel("tiny", device="cpu", compute_type="int8")
                segs, _ = mdl.transcribe(audio.flatten(), language="en",
                                         beam_size=1, best_of=1)
                text = " ".join(s.text.strip() for s in segs).strip() or "(silence)"
                self.after(0, lambda: self._log(f"STT: {text!r}", tag="ok"))
            except ImportError as e:
                self.after(0, lambda: self._log(f"Missing dep: {e}", tag="err"))
            except Exception as e:
                self.after(0, lambda: self._log(f"Mic error: {e}", tag="err"))
        threading.Thread(target=_run, daemon=True).start()

    def _test_tts(self):
        gd = self.game_dir_var.get().strip()
        if not gd:
            self._log("Set Game Dir first.", tag="err"); return
        self._log("Synthesizing…", tag="dim")
        wav = Path(gd) / "CLEO" / "tts_test.wav"
        def _run():
            try:
                try:
                    from piper.voice import PiperVoice, SynthesisConfig
                except ImportError:
                    from piper import PiperVoice  # type: ignore
                    SynthesisConfig = None        # type: ignore
                onnx = Path(gd) / "SAtella_voices" / "en_US-arctic-medium.onnx"
                if not onnx.exists():
                    self.after(0, lambda: self._log(f"Voice not found: {onnx}", tag="err"))
                    return
                voice = PiperVoice.load(str(onnx))
                import wave as wv
                wav.parent.mkdir(parents=True, exist_ok=True)
                with wv.open(str(wav), "wb") as wf:
                    if SynthesisConfig:
                        voice.synthesize_wav("Hey, what you want from me?", wf,
                                             syn_config=SynthesisConfig(speaker_id=7,
                                                                        length_scale=0.9))
                    else:
                        voice.synthesize_wav("Hey, what you want from me?", wf)
                import winsound
                winsound.PlaySound(str(wav), winsound.SND_FILENAME)
                self.after(0, lambda: self._log("TTS test played.", tag="ok"))
            except ImportError as e:
                self.after(0, lambda: self._log(f"Missing dep: {e}", tag="err"))
            except Exception as e:
                self.after(0, lambda: self._log(f"TTS error: {e}", tag="err"))
        threading.Thread(target=_run, daemon=True).start()

    # ── Daemon lifecycle ──────────────────────────────────────────────────────

    def _start(self):
        if self._daemon_proc and self._daemon_proc.poll() is None:
            self._log("Daemon already running.", tag="dim"); return

        self._save_config()
        gd = self.game_dir_var.get().strip()
        if not gd:
            self._log("Game Dir not set.", tag="err"); return

        backend = self.backend_var.get()
        cmd = [sys.executable, str(DAEMON_SCRIPT),
               "--game-dir", gd, "--backend", backend]

        key = self.api_key_var.get().strip()
        if backend == "claude":
            if key: cmd += ["--claude-api-key", key]
            cmd += ["--claude-model", self.model_var.get()]
        elif backend == "chatgpt":
            if key: cmd += ["--openai-api-key", key]
            cmd += ["--chatgpt-model", self.model_var.get()]
        elif backend == "ollama":
            cmd += ["--ollama-model", self.model_var.get()]

        dev_id = self._selected_mic_id()
        if dev_id is not None:
            cmd += ["--stt-device-id", str(dev_id)]

        try:
            self._daemon_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as e:
            self._log(f"Cannot start daemon: {e}", tag="err"); return

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._log(f"Daemon started (PID {self._daemon_proc.pid})", tag="ok")
        threading.Thread(target=self._read_output, daemon=True).start()

    def _stop(self):
        if self._daemon_proc:
            self._daemon_proc.terminate()
            self._daemon_proc = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._log("Daemon stopped.", tag="dim")

    def _read_output(self):
        proc = self._daemon_proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.after(0, lambda l=line: self._log(l))
        self.after(0, self._on_exit)

    def _on_exit(self):
        rc = self._daemon_proc.poll() if self._daemon_proc else None
        self._daemon_proc = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._log(f"Daemon exited (rc={rc}).", tag="dim" if rc == 0 else "err")

    # ── Game launch ───────────────────────────────────────────────────────────

    def _launch(self):
        # Prefer Steam launch so the overlay and cloud saves work correctly
        try:
            os.startfile(f"steam://rungameid/{GTA_STEAM_ID}")
            self._log("GTA SA launched via Steam.", tag="ok")
            return
        except Exception:
            pass
        # Fallback: launch the executable directly from the game directory
        gd = self.game_dir_var.get().strip()
        exe = Path(gd) / "gta_sa.exe" if gd else None
        if exe and exe.exists():
            try:
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
                self._log(f"Launched: {exe}", tag="ok")
            except Exception as e:
                self._log(f"Launch error: {e}", tag="err")
        else:
            self._log("gta_sa.exe not found. Check Game Dir or use Steam.", tag="err")

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _pick_dir(self):
        d = filedialog.askdirectory(
            title="Select GTA San Andreas folder",
            initialdir=self.game_dir_var.get() or "C:/")
        if d:
            self.game_dir_var.set(str(Path(d)))

    def _log(self, msg: str, *, tag: str = ""):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.config(state="normal")
        self.log.insert("end", f"[{ts}] ", "ts")
        self.log.insert("end", msg + "\n", tag or "")
        self.log.see("end")
        self.log.config(state="disabled")

    def _apply_taskbar_icon(self) -> None:
        """Set the taskbar icon via Windows API (ctypes). wm_iconbitmap alone
        only sets the title-bar icon; the taskbar requires an explicit WM_SETICON."""
        if os.name != "nt":
            return
        try:
            import ctypes
            self.update_idletasks()
            # GetParent returns the container HWND that the taskbar actually sees
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            load = ctypes.windll.user32.LoadImageW
            send = ctypes.windll.user32.SendMessageW
            # LR_LOADFROMFILE = 0x10, IMAGE_ICON = 1
            h_small = load(None, self._ico_tmp, 1, 16, 16, 0x10)
            h_large = load(None, self._ico_tmp, 1, 32, 32, 0x10)
            # WM_SETICON = 0x80, ICON_SMALL = 0, ICON_BIG = 1
            send(hwnd, 0x80, 0, h_small)
            send(hwnd, 0x80, 1, h_large)
        except Exception as e:
            print(f"[icon] taskbar: {e}")

    def _poll(self):
        """Detect unexpected daemon death while the stop button is still active."""
        if (self._daemon_proc
                and self._daemon_proc.poll() is not None
                and str(self.stop_btn["state"]) == "normal"):
            self._on_exit()
        self.after(1000, self._poll)

    def _on_close(self):
        self._stop()
        try:
            os.unlink(self._ico_tmp)
        except OSError:
            pass
        self.destroy()


if __name__ == "__main__":
    # Must be set before creating the window; otherwise Windows ignores the
    # taskbar grouping icon and falls back to the Python interpreter's icon.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "SAtella.Launcher.1"
            )
        except Exception:
            pass

    app = App()
    app.protocol("WM_DELETE_WINDOW", app._on_close)
    app.mainloop()
