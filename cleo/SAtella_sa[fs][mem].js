/// <reference path=".config/sa.d.ts" />
//
// SAtella — AI NPC dialogue in GTA San Andreas via CLEO Redux.
// File name must contain [fs] for CLEO Redux write permissions.
//
// Architecture (file-based IPC):
//   1. Player presses T near a pedestrian.
//   2. Script freezes the ped, collects context, writes request.json.
//   3. External Python daemon polls the folder, calls LLM/TTS, writes response.json.
//   4. Script reads the response and displays text above the ped.
//
// Notes:
//   - Ped pool address (0xB74490) is for GTA SA 1.0 US (HOODLUM).
//     Other builds (Steam, Rockstar Launcher) use different addresses — verify with Cheat Engine.
//   - Opcodes use Sanny Builder Library naming (https://library.sannybuilder.com/#/sa).
//   - CLEO Redux version tested: 1.4.3 with CLEO 5.4.0.

// ──────────── CONFIG ────────────

const TALK_KEY = 0x54;              // T — text input
const VOICE_KEY = 0x59;             // Y — voice input (STT via Python daemon)
const CANCEL_KEY = 0x1B;            // ESC — cancel waiting for response (text mode only)
const TALK_RANGE = 3.0;             // metres to nearest ped; raise if hard to trigger
const POLL_MS = 100;                // ms between INI checks; lower = more responsive, higher CPU
const RESPONSE_TIMEOUT_MS = 30000;  // if Python takes longer than this, dialogue is aborted
const MAX_TALK_DISTANCE = 10.0;     // if player walks farther than this during dialogue, it cancels
const MAX_HISTORY_TURNS = 6;        // player+npc pairs sent in context; raise for longer memory (bigger JSON)
const CONTINUE_TIMEOUT_MS = 15000;  // how long the player has to press T for the next line
const CONV_EXPIRE_MS = 600000;      // 10 min idle → NPC forgets the conversation; set to Infinity to never expire

// IPC files live directly in CLEO\ — OPEN_FILE does not create intermediate directories,
// so subdirectories must exist on disk before the script runs.
const REQUEST_PATH  = "CLEO\\SAtella_request.json";
const RESPONSE_PATH = "CLEO\\SAtella_response.json";  // unused by this script; Python reads it
const RESPONSE_INI  = "CLEO\\SAtella_response.ini";
const SPEECH_WAV    = "CLEO\\SAtella_speech.wav";     // TTS audio; Python writes this before updating the INI

// Opcode 0AD8 WRITE_STRING_TO_FILE has an internal buffer of ~128 bytes per call.
// We use 120 instead of 128 to leave headroom for the null terminator and encoding overhead.
const FILE_CHUNK = 120;
// SA renders text as individual glyphs; Cyrillic chars are 2 bytes in UTF-8,
// so a 70-char limit keeps bubbles on screen even with mixed-language text.
const BUBBLE_MAX_CHARS = 70;
const INPUT_MAX_LEN = 120;          // hard cap on typed input length
// When the input string exceeds this, only the last INPUT_VISIBLE_LEN chars are shown.
// SA's word-wrap fires at CenterSize px — showing the tail avoids confusing mid-word splits.
const INPUT_VISIBLE_LEN = 55;

// VK code → [unshifted char, shifted char] lookup table.
// Windows VK codes: 0x41–0x5A = A–Z (as uppercase VK; we emit lowercase at [0]).
//                   0x30–0x39 = 0–9.
// Must be declared before while(true) — const/let are NOT hoisted like function declarations.
const VK_CHARS = {};
for (let _i = 0; _i < 26; _i++)
    VK_CHARS[0x41 + _i] = [String.fromCharCode(0x61 + _i), String.fromCharCode(0x41 + _i)];
for (let _i = 0; _i < 10; _i++)
    VK_CHARS[0x30 + _i] = [String.fromCharCode(0x30 + _i), ")!@#$%^&*("[_i]];
VK_CHARS[0x20] = [" ",  " "];   // Space
VK_CHARS[0xBE] = [".",  ">"];   // OEM_PERIOD
VK_CHARS[0xBC] = [",",  "<"];   // OEM_COMMA
VK_CHARS[0xBD] = ["-",  "_"];   // OEM_MINUS
VK_CHARS[0xBF] = ["/",  "?"];   // OEM_2
VK_CHARS[0xDE] = ["'",  '"'];   // OEM_7
const CHAR_VKS = Object.keys(VK_CHARS).map(Number);

// GTA SA 1.0 US (HOODLUM/compact) CPed pool layout.
// ADDR_PED_POOL_PTR points to a CPool<CPed,CPed>* — a pointer to the pool struct, not the array.
// SIZEOF_CPED is the stride between pool slots; wrong value = reading garbage addresses.
const ADDR_PED_POOL_PTR = 0xB74490;
const SIZEOF_CPED = 1988;

// safeNative absorbs unknown opcode names without crashing — add new anims freely.
const TALK_ANIMS = [
    { anim: "IDLE_CHAT",   pack: "PED"      },
    { anim: "GESTICULATE", pack: "GESTURES" },
    { anim: "WAVE_A",      pack: "GESTURES" },
    { anim: "POINT",       pack: "GESTURES" },
    { anim: "NOD_YES",     pack: "PED"      },
];

// ──────────── STATE ────────────

const player = new Player(0);
const playerChar = player.getChar();
let requestCounter = 0;  // monotonically increasing; used to match requests to responses
let busy = false;        // prevents re-entry if a conversation is already running
// npcId → { history: [{role, text}], lastSeen: timestamp }
// Must be declared before while(true) — const/let are not hoisted.
const conversations = new Map();

log("SAtella: started. T = text input, Y = voice input (STT). Near a pedestrian.");
// Probe which global classes exist — helps diagnose missing CLEO Redux features at startup.
log("API probe: Task=" + (typeof Task) + " File=" + (typeof File) +
    " Text=" + (typeof Text) + " Clock=" + (typeof Clock) +
    " native=" + (typeof native));

// ──────────── MAIN LOOP ────────────

while (true) {
    wait(50);
    if (busy) continue;
    if (!player.isPlaying()) continue;
    const tDown = Pad.IsKeyPressed(TALK_KEY);
    const vDown = Pad.IsKeyPressed(VOICE_KEY);
    if (!tDown && !vDown) continue;
    // Debounce: wait for both keys to be released before acting, so the
    // key-press isn't immediately re-detected inside getPlayerInput.
    while (Pad.IsKeyPressed(TALK_KEY) || Pad.IsKeyPressed(VOICE_KEY)) wait(50);

    const handle = findNearestPedHandle(TALK_RANGE);
    if (!handle) {
        safeNative("PRINT_FORMATTED_NOW", null, "No one nearby.", 1500);
        continue;
    }

    busy = true;
    try {
        runConversation(new Char(handle), vDown);
    } catch (e) {
        log("conversation error: " + e);
    }
    busy = false;
}

// ──────────── PED LOOKUP ────────────
//
// Scans the CPed pool via raw memory — the standard CLEO approach because
// SA has no opcode to enumerate all live peds.
// Returns an SCM handle (integer) suitable for new Char(handle), or 0 if none found.

function findNearestPedHandle(maxRange) {
    const ppos = playerChar.getCoordinates();
    const px = ppos.x, py = ppos.y, pz = ppos.z;

    // ADDR_PED_POOL_PTR holds a pointer-to-pointer: dereference once to get the pool struct.
    const poolPtr = Memory.ReadI32(ADDR_PED_POOL_PTR, false);
    if (!poolPtr) return 0;

    // CPool<CPed> memory layout (SA 1.0 US):
    //   +0x00  CPed* objectsArr  — base address of the inline object array
    //   +0x04  u8*   flagsArr    — one byte per slot; bit 7 = free, low 7 bits = ref counter
    //   +0x08  int   size        — total pool capacity (usually 140)
    const objectsArr = Memory.ReadI32(poolPtr + 0x00, false);
    const flagsArr   = Memory.ReadI32(poolPtr + 0x04, false);
    const size       = Memory.ReadI32(poolPtr + 0x08, false);

    const playerHandle = +playerChar;
    let bestHandle = 0;
    let bestDistSq = maxRange * maxRange;

    for (let i = 0; i < size; i++) {
        const flag = Memory.ReadU8(flagsArr + i, false);
        // Bit 7 of the flags byte is set when the slot is free/unused.
        if (flag & 0x80) continue;

        // objectsArr is an inline array (not a pointer array) — objects are packed at stride SIZEOF_CPED.
        const pedAddr = objectsArr + i * SIZEOF_CPED;

        // Position lives inside the CEntity matrix (CEntity::m_matrix at +0x14, a CMatrix* pointer).
        // If the matrix pointer is null (can happen during construction), fall back to
        // the embedded CSimpleTransform at entity base +0x04.
        const matrixPtr = Memory.ReadI32(pedAddr + 0x14, false);
        let x, y, z;
        if (matrixPtr) {
            x = Memory.ReadFloat(matrixPtr + 0x30, false);  // CMatrix::pos.x
            y = Memory.ReadFloat(matrixPtr + 0x34, false);  // CMatrix::pos.y
            z = Memory.ReadFloat(matrixPtr + 0x38, false);  // CMatrix::pos.z
        } else {
            x = Memory.ReadFloat(pedAddr + 0x04, false);
            y = Memory.ReadFloat(pedAddr + 0x08, false);
            z = Memory.ReadFloat(pedAddr + 0x0C, false);
        }

        const dx = x - px, dy = y - py, dz = z - pz;
        const distSq = dx * dx + dy * dy + dz * dz;
        if (distSq >= bestDistSq) continue;

        // SCM handle encoding: upper 24 bits = pool index, low 8 bits = ref counter (0–127).
        // The ref counter increments each time the slot is reused, so a stale handle from a
        // dead ped will fail CLEO's validity check when you try to use it later.
        const handle = (i << 8) | (flag & 0x7F);
        if (handle === playerHandle) continue;

        bestDistSq = distSq;
        bestHandle = handle;
    }

    return bestHandle;
}

// ──────────── CONVERSATION FLOW ────────────

function runConversation(npc, useVoice) {
    const npcId = +npc;
    const playerId = +playerChar;
    // Tracks whether the finally block needs to clear NPC tasks.
    // Set to false by executeReaction when a reaction (flee, attack, etc.) already
    // owns the NPC's task queue — clearing it there would cancel the reaction mid-way.
    let npcTasksNeedClearing = true;
    try {
        log("step: freeze npc voice=" + !!useVoice);
        // Three-step freeze — ORDER MATTERS because SA's task system is a FIFO queue:
        //   1. CLEAR_CHAR_TASKS_IMMEDIATELY — flush any running AI tasks synchronously.
        //   2. TASK_LOOK_AT_CHAR — enqueue "face the player" (runs first after clear).
        //   3. TASK_STAND_STILL — enqueue "don't move" on top (runs after look-at settles).
        // Reversing steps 2 and 3 causes the NPC to stand still while facing a random direction.
        native("CLEAR_CHAR_TASKS_IMMEDIATELY", npcId);
        native("TASK_LOOK_AT_CHAR", npcId, playerId, 600000);
        native("TASK_STAND_STILL", npcId, 600000);
        // Extra pin: prevents the ambient AI from re-assigning wander tasks between our refreshes.
        safeNative("SET_CHAR_STAY_IN_SAME_PLACE", null, npcId, true);

        const conv = getOrCreateConv(npcId);
        log("conv: npc=" + npcId + " history=" + conv.history.length);

        while (true) {
            let playerText, answer;

            if (useVoice) {
                // Voice mode: Python records the mic, transcribes (STT), and calls the LLM.
                // The player_text field is only known after Python responds, not before.
                log("step: voice input (stt)");
                const ctx = collectContext(npc, "", conv.history, "stt");
                if (!writeFile(REQUEST_PATH, JSON.stringify(ctx))) { showText("IPC write failed.", 2000); break; }
                answer = waitForResponse(ctx.id, npc, true);
                if (!answer) { showText("(no response)", 2000); break; }
                playerText = answer.playerText || "";
                if (!playerText) { showText("(nothing heard)", 1500); break; }
                // Show the STT transcription before the NPC replies so the player knows what was heard.
                showText("You: " + playerText.slice(0, 60), 2000);
                wait(800);
                log("voice said: " + playerText);
            } else {
                // Text mode: block player controls while the keyboard input field is shown.
                // Controls are restored in the finally block even if an exception occurs.
                log("step: text input");
                safeNative("SET_PLAYER_CONTROL", null, 0, false);
                playerText = getPlayerInput();
                safeNative("SET_PLAYER_CONTROL", null, 0, true);
                if (playerText === null) { showText("Cancelled.", 1500); break; }
                log("player said: " + playerText);
                const ctx = collectContext(npc, playerText, conv.history);
                log("request size: " + JSON.stringify(ctx).length);
                if (!writeFile(REQUEST_PATH, JSON.stringify(ctx))) { showText("IPC write failed.", 2000); break; }
                showText("...", 500);
                log("step: wait for response");
                answer = waitForResponse(ctx.id, npc, false);
                if (!answer) { showText("(NPC silent)", 2000); break; }
                log("got answer: " + answer.text.slice(0, 60));
            }

            // Update history regardless of input mode — both paths converge here.
            conv.history.push({ role: "player", text: playerText });
            conv.history.push({ role: "npc",    text: answer.text });
            trimHistory(conv.history);
            conv.lastSeen = Date.now();
            log("conv: history now " + conv.history.length + " entries");

            const hasReaction = answer.reaction && answer.reaction !== "none";
            if (hasReaction) {
                log("reaction: " + answer.reaction + "(" + answer.reactionValue + ")");
                // executeReaction returns false if it took ownership of the task queue
                // (flee, attack, etc.) — in that case finally must NOT clear tasks.
                npcTasksNeedClearing = executeReaction(npc, answer.reaction, answer.reactionValue);
            }

            // Start TTS audio and show text simultaneously (startNpcVoice is fire-and-forget).
            const voiceStream = answer.speechMs ? startNpcVoice(npc) : null;
            showNpcLine(npc, answer.text || "...", answer.speechMs, !hasReaction);
            stopNpcVoice(voiceStream);

            // After a reaction the NPC is no longer frozen — end the conversation.
            if (hasReaction) break;

            if (!waitForContinue(npc, useVoice)) break;

            // Re-freeze after waitForContinue because TASK_STAND_STILL has a finite
            // duration and may have expired while the player was reading/thinking.
            native("TASK_LOOK_AT_CHAR", npcId, playerId, 600000);
            native("TASK_STAND_STILL", npcId, 600000);
        }
    } finally {
        // Always run on exit — whether by break, return, or uncaught exception.
        safeNative("SET_CHAR_STAY_IN_SAME_PLACE", null, npcId, false);
        if (npcTasksNeedClearing) native("CLEAR_CHAR_TASKS_IMMEDIATELY", npcId);
        // Restore controls in case we exited while text-input had them disabled.
        safeNative("SET_PLAYER_CONTROL", null, 0, true);
    }
}

// Builds the JSON payload sent to the Python daemon.
// All fields except `history` are sampled at the moment the player submits input.
function collectContext(npc, playerText, history, mode) {
    const ppos = playerChar.getCoordinates();
    const npos = npc.getCoordinates();
    const time = getTimeSafe();
    const npcId = +npc;
    return {
        id: ++requestCounter,   // monotonic; Python echoes this in the INI so we can match response to request
        ts: Date.now(),
        mode: mode || "text",   // "text" | "stt" — tells Python whether to record audio first
        npc: {
            handle: npcId,
            model_id: safeNative("GET_CHAR_MODEL", 0, npcId),
            health: safeNative("GET_CHAR_HEALTH", 0, npcId),
            pos: [npos.x, npos.y, npos.z],
        },
        player: {
            pos: [ppos.x, ppos.y, ppos.z],
            wanted_level: safeNative("STORE_WANTED_LEVEL", 0, 0),
            money: safeNative("STORE_SCORE", 0, 0),
            text: playerText || "",
        },
        world: {
            hour: time.hours,
            minute: time.minutes,
        },
        history: history || [],
    };
}

// ──────────── CONVERSATION HISTORY ────────────

// Returns (or creates) the conversation record for an NPC.
// Also prunes expired conversations to prevent unbounded Map growth.
function getOrCreateConv(npcId) {
    const now = Date.now();
    for (const [id, c] of conversations) {
        if (now - c.lastSeen > CONV_EXPIRE_MS) {
            conversations.delete(id);
            log("conv: expired npc=" + id);
        }
    }
    if (!conversations.has(npcId)) {
        conversations.set(npcId, { history: [], lastSeen: now });
        log("conv: new for npc=" + npcId);
    }
    return conversations.get(npcId);
}

// Keeps history at MAX_HISTORY_TURNS pairs (player+npc = 2 entries per turn).
// Drops the oldest entries first — FIFO sliding window.
function trimHistory(history) {
    const max = MAX_HISTORY_TURNS * 2;
    while (history.length > max) history.shift();
}

// Waits for the player to press T again (continue) or times out / walks away (end dialogue).
// Returns true to continue, false to end.
function waitForContinue(npc, useVoice) {
    // Drain any T keypresses that may still be held from the previous step.
    while (Pad.IsKeyPressed(TALK_KEY)) wait(50);

    const start = Date.now();
    let lastRefresh = Date.now();
    while (Date.now() - start < CONTINUE_TIMEOUT_MS) {
        wait(0);

        const now = Date.now();
        // TASK_STAND_STILL expires after its duration argument — refresh every 3 s to keep
        // the NPC pinned, because the ambient AI will assign new wander tasks otherwise.
        if (now - lastRefresh > 3000) {
            native("TASK_STAND_STILL", +npc, 30000);
            lastRefresh = now;
        }

        const secsLeft = Math.ceil((CONTINUE_TIMEOUT_MS - (now - start)) / 1000);
        renderContinueHint(secsLeft, useVoice);

        // Cancel if player has walked too far (2D check — we don't care about height difference).
        const ppos = playerChar.getCoordinates();
        const npos = npc.getCoordinates();
        const dx = ppos.x - npos.x, dy = ppos.y - npos.y;
        if (dx * dx + dy * dy > MAX_TALK_DISTANCE * MAX_TALK_DISTANCE) {
            log("wfc: too far");
            return false;
        }

        if (Pad.IsKeyPressed(TALK_KEY)) { while (Pad.IsKeyPressed(TALK_KEY)) wait(50); return true; }
    }
    log("wfc: timeout");
    return false;
}

// ──────────── HUD RENDERING ────────────

// Shared Text setup for the bottom-of-screen hint banners.
// CenterSize(560) = full safe area width; text is centered on x=320.
function setupHudText(r, g, b, a) {
    Text.UseCommands(true);
    Text.SetFont(1);            // font 1 = subtitle font (cleaner than font 0)
    Text.SetScale(0.35, 0.70);
    Text.SetEdge(1, 0, 0, 0, 200);
    Text.SetBackground(false);
    Text.SetCenter(true);
    Text.SetCenterSize(560.0);
    Text.SetProportional(true);
    Text.SetColor(r, g, b, a);
}

function renderContinueHint(secsLeft, useVoice) {
    const label = useVoice ? "[T] Speak again" : "[T] Continue";
    setupHudText(160, 220, 160, 210);
    Text.DisplayFormatted(320, 388, label + "   (walk away or wait " + secsLeft + "s)");
}

function renderVoiceHint(phase) {
    setupHudText(255, 160, 80, 220);
    Text.DisplayFormatted(320, 388, phase);
}

// ──────────── PLAYER TEXT INPUT ────────────

// Renders a text input box and returns the typed string when Enter is pressed,
// or null if the player cancels (Backspace on empty input).
// ESC is intentionally NOT handled here — SET_PLAYER_CONTROL(false) does not block
// the pause menu, so pressing ESC would open the menu instead of cancelling.
function getPlayerInput() {
    let text = "";
    let prevPressed = new Set();
    let bsDownAt = 0;   // timestamp when Backspace was first held down
    let lastBsAt = 0;   // timestamp of last Backspace repeat

    while (true) {
        wait(0);

        // Build snapshot of currently held keys each frame.
        const cur = new Set();
        for (const vk of CHAR_VKS) {
            if (Pad.IsKeyPressed(vk)) cur.add(vk);
        }
        const SPECIAL_VKS = [0x10, 0x0D, 0x08]; // Shift, Enter, Backspace
        for (const vk of SPECIAL_VKS) {
            if (Pad.IsKeyPressed(vk)) cur.add(vk);
        }

        const shift = cur.has(0x10);

        // Enter submits only on the leading edge (new press, not held) to avoid
        // the game processing the key again after we return.
        if (cur.has(0x0D) && !prevPressed.has(0x0D)) {
            const trimmed = text.trim();
            if (trimmed) {
                while (Pad.IsKeyPressed(0x0D)) wait(50);
                return trimmed;
            }
        }

        // Backspace: delete one char on leading edge; hold for 400 ms then repeat every 80 ms.
        // On empty input, Backspace cancels the dialogue (safer than ESC — see note above).
        if (cur.has(0x08)) {
            const now = Date.now();
            if (!prevPressed.has(0x08)) {
                if (text.length > 0) {
                    text = text.slice(0, -1);
                    bsDownAt = now;
                    lastBsAt = now;
                } else {
                    while (Pad.IsKeyPressed(0x08)) wait(50);
                    return null;
                }
            } else if (now - bsDownAt > 400 && now - lastBsAt > 80) {
                if (text.length > 0) text = text.slice(0, -1);
                lastBsAt = now;
            }
        }

        // Append printable chars only on leading-edge press to prevent key-repeat duplicates.
        if (text.length < INPUT_MAX_LEN) {
            for (const vk of CHAR_VKS) {
                if (!cur.has(vk) || prevPressed.has(vk)) continue;
                const pair = VK_CHARS[vk];
                if (pair) text += shift ? pair[1] : pair[0];
            }
        }

        renderInputBox(text);
        prevPressed = cur;
    }
}

function renderInputBox(inputText) {
    // Blink cursor at 1 Hz (500 ms on, 500 ms off).
    const cursor = (Math.floor(Date.now() / 500) % 2 === 0) ? "|" : " ";
    // When the string is longer than INPUT_VISIBLE_LEN, show only the tail with a ">" prefix
    // so the player knows there is hidden text to the left.
    const visible = inputText.length > INPUT_VISIBLE_LEN
        ? ">" + inputText.slice(-INPUT_VISIBLE_LEN)
        : inputText;

    Text.UseCommands(true);
    Text.SetFont(1);
    Text.SetScale(0.35, 0.70);
    Text.SetEdge(1, 0, 0, 0, 200);
    Text.SetBackground(false);
    Text.SetProportional(true);

    // Hint line — centered over the input line.
    Text.SetCenter(true);
    Text.SetCenterSize(560.0);
    Text.SetColor(180, 180, 200, 220);
    Text.DisplayFormatted(320, 365, "What do you say?  [Enter] Send   [Backspace on empty] Cancel");

    // Input line — also centered; a wider CenterSize would cause word-wrap on spaces.
    Text.SetCenter(true);
    Text.SetColor(255, 255, 150, 255);
    Text.DisplayFormatted(320, 388, "> " + visible + cursor);
}

// Calls a native opcode by name. Returns fallback instead of throwing if the
// opcode is unknown or produces null/undefined — keeps the script running on
// CLEO Redux builds that are missing optional opcodes.
function safeNative(name, fallback, ...args) {
    try {
        const v = native(name, ...args);
        return (v === undefined || v === null) ? fallback : v;
    } catch (e) {
        log("native(" + name + ") failed: " + e);
        return fallback;
    }
}

// Polls the response INI until Python writes a matching id, or until timeout/cancel/walk-away.
// voiceMode = true: renders voice HUD every frame (wait(0) inner loop) instead of sleeping.
function waitForResponse(reqId, npc, voiceMode) {
    const start = Date.now();
    let pollCount = 0;
    while (Date.now() - start < RESPONSE_TIMEOUT_MS) {
        if (voiceMode) {
            // Voice mode needs per-frame HUD updates so "Listening..." / "Thinking..."
            // renders smoothly. We can't use wait(POLL_MS) here because that would
            // freeze the screen for 100 ms between frames.
            const loopEnd = Date.now() + POLL_MS;
            while (Date.now() < loopEnd) {
                wait(0);
                const elapsed = Date.now() - start;
                renderVoiceHint(elapsed < 8000 ? "Listening..." : "Thinking...");
            }
        } else {
            wait(POLL_MS);
        }
        pollCount++;

        // ESC cancel is only meaningful in text mode; in voice mode Python is recording.
        if (!voiceMode && Pad.IsKeyPressed(CANCEL_KEY)) {
            log("wfr: cancelled");
            return null;
        }

        // TASK_STAND_STILL has a finite duration — the ambient AI will override it after ~3 s.
        // Re-issue every ~3 s (30 polls × 100 ms) to keep the NPC frozen while we wait.
        if (pollCount % 30 === 0) {
            native("TASK_STAND_STILL", +npc, 30000);
        }

        // Abort if player has walked away — no point waiting for a response they won't see.
        const ppos = playerChar.getCoordinates();
        const npos = npc.getCoordinates();
        const dx = ppos.x - npos.x, dy = ppos.y - npos.y, dz = ppos.z - npos.z;
        if (dx*dx + dy*dy + dz*dz > MAX_TALK_DISTANCE * MAX_TALK_DISTANCE) {
            log("wfr: too far");
            return null;
        }

        // Python writes `id = <reqId>` to the INI when the response is ready.
        // We skip if the id doesn't match — an old response from a previous conversation.
        const id = safeNative("READ_INT_FROM_INI_FILE", 0, RESPONSE_INI, "SAtella", "id");
        if (!id || id !== reqId) continue;

        log("wfr: id=" + id + " on poll #" + pollCount);

        // player_text is populated by STT; empty string in text mode (Python echoes what it received).
        const playerText = safeNative("READ_STRING_FROM_INI_FILE", "", RESPONSE_INI, "SAtella", "player_text") || "";

        // Long NPC text is split into text0, text1, … because READ_STRING_FROM_INI_FILE
        // is limited to ~120 chars per call. text_chunks tells us how many to read.
        const chunkCount = safeNative("READ_INT_FROM_INI_FILE", 0, RESPONSE_INI, "SAtella", "text_chunks");
        let text = "";
        if (chunkCount && chunkCount > 0) {
            for (let i = 0; i < chunkCount; i++) {
                const part = safeNative("READ_STRING_FROM_INI_FILE", null, RESPONSE_INI, "SAtella", "text" + i);
                if (part) text += part;
            }
        } else {
            // Fallback: single `text` key for short responses or older Python daemon versions.
            text = safeNative("READ_STRING_FROM_INI_FILE", "", RESPONSE_INI, "SAtella", "text") || "";
        }
        log("wfr: text len=" + text.length + " chunks=" + chunkCount + " playerText=" + playerText.slice(0, 40));
        if (!text && !playerText) continue;

        const reaction      = safeNative("READ_STRING_FROM_INI_FILE", "none", RESPONSE_INI, "SAtella", "reaction") || "none";
        const reactionValue = safeNative("READ_INT_FROM_INI_FILE", 0, RESPONSE_INI, "SAtella", "reaction_value");
        const speechMs      = safeNative("READ_INT_FROM_INI_FILE", 0, RESPONSE_INI, "SAtella", "speech_duration_ms");
        log("wfr: reaction=" + reaction + "(" + reactionValue + ") speechMs=" + speechMs);

        // Reset id to 0 so the next call to waitForResponse doesn't pick up this same response.
        // This is the "consume" step — without it, every subsequent poll would re-read the old id.
        safeNative("WRITE_INT_TO_INI_FILE", null, 0, RESPONSE_INI, "SAtella", "id");

        return { id, text: text.replace(/\\n/g, "\n"), reaction, reactionValue, playerText, speechMs };
    }
    log("wfr: timeout after " + pollCount + " polls");
    return null;
}

// ──────────── TTS AUDIO ────────────

// Loads the pre-generated WAV as a positional 3D audio stream attached to the NPC.
// Audio volume falls off with distance, which makes speech feel immersive.
// Returns the stream handle so the caller can stop it when the bubble finishes.
function startNpcVoice(npc) {
    const stream = AudioStream3D.Load(SPEECH_WAV);
    if (!stream) { log("tts: stream load failed"); return null; }
    stream.setPlayAtChar(npc);
    stream.setState(1); // 1 = play; 0 = stop
    log("tts: 3D stream started for npc=" + (+npc));
    return stream;
}

function stopNpcVoice(stream) {
    if (!stream) return;
    // Wrapped in try/catch — the stream may have already ended naturally if the audio
    // was shorter than the displayed text.
    try { stream.setState(0); stream.remove(); } catch (e) {}
}

// ──────────── NPC REACTIONS ────────────
//
// executeReaction clears the freeze tasks and assigns new NPC behaviour.
// Return value: true  → finally block should call CLEAR_CHAR_TASKS_IMMEDIATELY
//               false → reaction already owns the task queue; do not touch it

function executeReaction(npc, reaction, value) {
    const npcId    = +npc;
    const playerId = +playerChar;

    // Always unfreeze first — every reaction needs a clean task queue to work correctly.
    safeNative("SET_CHAR_STAY_IN_SAME_PLACE", null, npcId, false);
    native("CLEAR_CHAR_TASKS_IMMEDIATELY", npcId);

    if (reaction === "flee") {
        // 100.0 = flee radius in metres; -1 = flee indefinitely (no time limit).
        safeNative("TASK_SMART_FLEE_CHAR", null, npcId, playerId, 100.0, -1);
        return false;
    }

    if (reaction === "walk_away") {
        // TASK_WANDER_STANDARD hands control back to the ambient AI (normal pedestrian behaviour).
        safeNative("TASK_WANDER_STANDARD", null, npcId);
        return false;
    }

    if (reaction === "attack") {
        // TASK_KILL_CHAR_ON_FOOT (05E2) — SA name; signature: (killer, target), no 3rd arg.
        // Do NOT add extra arguments — the opcode signature differs from VC/III.
        safeNative("TASK_KILL_CHAR_ON_FOOT", null, npcId, playerId);
        return false;
    }

    if (reaction === "hands_up") {
        // TASK_HANDS_UP (05C4) exists in SA with a duration parameter.
        // Do not follow up with TASK_STAND_STILL — it would override the animation.
        safeNative("TASK_HANDS_UP", null, npcId, 15000);
        return false;
    }

    if (reaction === "give_money") {
        const amount = (value > 0 && value <= 500) ? value : 50;
        safeNative("ADD_SCORE", null, 0, amount);   // 0 = player index
        showText("+" + amount + "$", 2500);
        safeNative("TASK_WANDER_STANDARD", null, npcId);
        return false;
    }

    if (reaction === "call_cops") {
        safeNative("TASK_USE_MOBILE_PHONE", null, npcId, 3500);
        const cur = safeNative("STORE_WANTED_LEVEL", 0, 0);
        // ALTER_WANTED_LEVEL (010D) sets stars absolutely (not delta).
        // SET_PLAYER_WANTED_LEVEL does not exist in SA — use ALTER_WANTED_LEVEL instead.
        safeNative("ALTER_WANTED_LEVEL", null, 0, Math.min(6, cur + 1));
        return false;
    }

    if (reaction === "draw_weapon") {
        const weaponId = (value > 0) ? value : 22;  // 22 = pistol; see weapon IDs in Sanny Builder lib
        safeNative("GIVE_WEAPON_TO_CHAR", null, npcId, weaponId, 30, true);
        safeNative("TASK_AIM_GUN_AT_CHAR", null, npcId, playerId, 8000);
        return false;
    }

    if (reaction === "call_gang") {
        spawnGangBehindPlayer(2);
        safeNative("TASK_WANDER_STANDARD", null, npcId); // informant walks away after signalling
        return false;
    }

    // Unknown reaction — let the finally block clean up.
    return true;
}

// Spawns `count` hostile peds behind the player (outside their FOV) to simulate
// called reinforcements. Each ped is armed and immediately attacks the player.
function spawnGangBehindPlayer(count) {
    const ppos    = playerChar.getCoordinates();
    const heading = safeNative("GET_CHAR_HEADING", 0.0, +playerChar);
    // SA heading: 0° = north (+Y), increases clockwise (unlike standard math angles).
    // Adding 180° gives the direction directly behind the player.
    const backRad = (heading + 180.0) * Math.PI / 180.0;
    const sinB = Math.sin(backRad), cosB = Math.cos(backRad);
    const DIST  = 7.0;   // metres behind the player
    const SIDE  = 2.0;   // lateral spread between peds so they don't spawn on top of each other

    const MODELS = [102, 103, 104, 21, 28, 29]; // Ballas (102–104) + generic street gang

    for (let i = 0; i < count; i++) {
        const sideOff = (i % 2 === 0 ? 1 : -1) * SIDE;
        const sx = ppos.x + sinB * DIST + cosB * sideOff;
        const sy = ppos.y + cosB * DIST - sinB * sideOff;

        const model = MODELS[i % MODELS.length];
        // REQUEST_MODEL starts an async load. We must poll HAS_MODEL_LOADED before
        // calling CREATE_CHAR — passing an unloaded model ID causes an immediate C-level crash.
        safeNative("REQUEST_MODEL", null, model);
        const deadline = Date.now() + 3000;
        while (!safeNative("HAS_MODEL_LOADED", false, model) && Date.now() < deadline) {
            wait(50);
        }
        if (!safeNative("HAS_MODEL_LOADED", false, model)) {
            log("call_gang: model " + model + " load timeout, skip i=" + i);
            continue;
        }

        // CREATE_CHAR args: pedType=4 (gang member), modelId, x, y, z → returns handle
        const ped = safeNative("CREATE_CHAR", 0, 4, model, sx, sy, ppos.z);
        if (!ped) { log("call_gang: CREATE_CHAR failed i=" + i); continue; }

        safeNative("GIVE_WEAPON_TO_CHAR", null, ped, 22, 30, true);
        // TASK_KILL_CHAR_ON_FOOT (05E2) — no 3rd argument in SA (differs from VC).
        safeNative("TASK_KILL_CHAR_ON_FOOT", null, ped, +playerChar);
        log("call_gang: spawned ped=" + ped + " model=" + model);
    }
}

// ──────────── NPC SPEECH DISPLAY ────────────

// Splits raw LLM text into alternating plain / action segments.
// Action text is wrapped in *asterisks* by the LLM prompt convention,
// e.g. "Sure thing. *shifts nervously*" → [{plain}, {action}].
function parseSegments(text) {
    const segs = [];
    const re = /\*([^*]+)\*/g;
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
        if (m.index > last) {
            const t = text.slice(last, m.index).trim();
            if (t) segs.push({ text: t, isAction: false });
        }
        const t = m[1].trim();
        if (t) segs.push({ text: t, isAction: true });
        last = m.index + m[0].length;
    }
    const tail = text.slice(last).trim();
    if (tail) segs.push({ text: tail, isAction: false });
    return segs;
}

// Breaks a string into display-safe chunks of at most maxLen characters,
// splitting at the last space before the limit to avoid cutting words.
function splitText(text, maxLen) {
    const chunks = [];
    let rem = text.trim();
    while (rem.length > maxLen) {
        let cut = maxLen;
        // Walk backwards to find a space — prefer breaking at a word boundary.
        while (cut > 0 && rem[cut] !== ' ') cut--;
        if (!cut) {
            // No space to the left — find the next space to the right and break there.
            cut = rem.indexOf(' ', maxLen);
            if (cut === -1) break; // single long word with no spaces — show it whole
        }
        chunks.push(rem.slice(0, cut).trim());
        rem = rem.slice(cut).trim();
    }
    if (rem) chunks.push(rem);
    return chunks;
}

// playNpcGesture: any TASK_PLAY_ANIM* variant preempts TASK_STAND_STILL in SA,
// causing the NPC to walk away mid-sentence. No overlay opcode exists that leaves
// the primary task intact — function is kept for future use but not called.
function playNpcGesture(npc, durationMs) {
    const entry = TALK_ANIMS[Math.floor(Math.random() * TALK_ANIMS.length)];
    safeNative("TASK_PLAY_ANIM_SECONDARY", null,
        +npc, entry.anim, entry.pack,
        4.0, true, false, false, false,
        durationMs
    );
}

// Displays all chunks of an NPC line sequentially, distributing TTS time evenly.
// Plain text renders in warm white; action text (*...*) renders in pale blue.
function showNpcLine(npc, text, totalMs, allowGestures) {
    if (allowGestures === undefined) allowGestures = true;
    log("Text to display: " + text);
    const segs = parseSegments(text);
    const flat = [];
    for (const seg of segs) {
        for (const chunk of splitText(seg.text, BUBBLE_MAX_CHARS)) {
            flat.push({ chunk, isAction: seg.isAction });
        }
    }
    if (flat.length === 0) return;
    // Divide total TTS duration across all chunks.
    // Enforce a 1500 ms minimum so fast TTS doesn't make text unreadable.
    // If there is no TTS (speechMs = 0 or undefined), default to 3500 ms per chunk.
    const msEach = (totalMs && totalMs > 0)
        ? Math.max(1500, Math.floor(totalMs / flat.length))
        : 3500;
    for (const { chunk, isAction } of flat) {
        if (isAction) displayBubble(npc, chunk, msEach, 100, 180, 255, allowGestures);  // blue = emote
        else          displayBubble(npc, chunk, msEach, 255, 255, 200, allowGestures);  // warm white = speech
        wait(200); // short gap between chunks so rapid text doesn't blur together
    }
}

// Renders a speech bubble above the NPC's head for `ms` milliseconds.
// Projects the NPC's world position to screen space each frame — the bubble tracks
// movement smoothly instead of snapping to a fixed screen coordinate.
//
// SA internal render resolution is 640×448 (widescreen HUD still uses this grid).
// CONVERT_3D_TO_SCREEN_2D returns coordinates in that space, or null if behind camera.
function displayBubble(npc, text, ms, r, g, b, allowFreeze) {
    if (r === undefined) r = 255;
    if (g === undefined) g = 255;
    if (b === undefined) b = 200;
    if (allowFreeze === undefined) allowFreeze = true;
    // Strip SA format codes (~r~, ~w~, ~n~, etc.) — they appear as literal tildes in
    // Text.DisplayFormatted and make the bubble look broken.
    const safe = text.replace(/~[^~]*~/g, '');
    // BOX_HALF = half of CenterSize(320): used to clamp cx so the text box never
    // crosses the screen edge (which would clip it or render garbage on the right side).
    const BOX_HALF = 160;
    const start = Date.now();
    let lastFreeze = Date.now();
    while (Date.now() - start < ms) {
        wait(0); // yield every frame so the game doesn't freeze
        // Keep the NPC pinned — TASK_PLAY_ANIM* would preempt TASK_STAND_STILL,
        // so we refresh here too (same 2 s cadence as waitForContinue).
        if (allowFreeze && Date.now() - lastFreeze > 2000) {
            native("TASK_STAND_STILL", +npc, 30000);
            lastFreeze = Date.now();
        }
        const npos = npc.getCoordinates();
        // Offset z by +1.0 to place text just above the ped's head (head ≈ +1.0 m from origin).
        // CONVERT_3D_TO_SCREEN_2D returns null when the point is behind the camera frustum.
        const sc = safeNative("CONVERT_3D_TO_SCREEN_2D", null,
            npos.x, npos.y, npos.z + 1.0, true, true);
        if (!sc || sc.x < 0 || sc.x > 640 || sc.y < 0 || sc.y > 448) continue;

        // Clamp the bubble center so the 320-wide text box stays fully on screen.
        const cx = Math.max(BOX_HALF, Math.min(sc.x, 640 - BOX_HALF));

        Text.UseCommands(true);
        Text.SetFont(1);
        Text.SetScale(0.40, 0.80);
        Text.SetColor(r, g, b, 255);
        Text.SetEdge(1, 0, 0, 0, 200); // black outline for readability on any background colour
        Text.SetBackground(false);
        Text.SetCenter(true);
        Text.SetCenterSize(320.0);      // narrower than the hint banners — keeps bubble tidy
        Text.SetProportional(true);
        Text.DisplayFormatted(cx, sc.y, safe);
    }
}

// Wrapper for PRINT_FORMATTED_NOW — used for brief on-screen status messages
// (e.g. "IPC write failed.", "NPC silent") that appear at the bottom of the screen.
function showText(s, ms) {
    safeNative("PRINT_FORMATTED_NOW", null, s, ms);
}

// ──────────── FILE I/O ────────────
//
// Opcodes 0AD8 WRITE_STRING_TO_FILE and 0AD7 READ_STRING_FROM_FILE have an
// internal ~128-byte buffer per call, so long JSON must be chunked.
// FILE_CHUNK is set to 120 (not 128) to leave headroom for null terminators.

// Writes `contents` to `path`, retrying OPEN_FILE up to 5 times.
// Retries are needed because the Python daemon may have the file open briefly
// while writing its response — the OS returns a sharing-violation error in that window.
function writeFile(path, contents) {
    let f = 0;
    for (let attempt = 1; attempt <= 5 && !f; attempt++) {
        f = safeNative("OPEN_FILE", 0, path, "wb");
        if (!f) {
            log("OPEN_FILE attempt " + attempt + "/5 failed for: " + path);
            wait(100);
        }
    }
    if (!f) {
        log("writeFile: giving up on " + path);
        return false;
    }
    let written = 0;
    while (written < contents.length) {
        const chunk = contents.slice(written, written + FILE_CHUNK);
        safeNative("WRITE_STRING_TO_FILE", null, f, chunk);
        written += chunk.length;
        if (written > 1_000_000) break; // safety guard — a valid request JSON is never this large
    }
    safeNative("CLOSE_FILE", null, f);
    return true;
}

// Reads and returns the full text content of `path`, or null on failure.
// Uses a two-pass strategy because the CLEO read opcodes are unreliable in 5.4.0:
//   Pass 1 — open with OPEN_FILE + GET_FILE_SIZE to verify existence and get the byte count,
//             then close. This uses stable opcodes that don't crash.
//   Pass 2 — read the actual content via the safest available method (std.loadFile first,
//             then File class as a fallback for future CLEO Redux versions).
function readFile(path) {
    // Both CLEO 5.4.0 read opcodes (0AD7, 0ADE) crash at C level — try/catch cannot save us.
    // File class (CLEO Redux C++ API) has a bug in 1.4.3 where isOpen is always false.
    // std.loadFile bypasses CLEO entirely and is the only reliable option in current builds.

    // Pass 1: confirm existence and get size.
    const fTmp = safeNative("OPEN_FILE", 0, path, "rb");
    if (!fTmp) return null;
    const size = safeNative("GET_FILE_SIZE", 0, fTmp);
    safeNative("CLOSE_FILE", null, fTmp);
    log("rf: size=" + size);
    // Reject zero-length, negative, or suspiciously large files.
    // 65 536 bytes is far above any valid response JSON — a larger file indicates corruption.
    if (!size || size <= 0 || size > 65536) return null;

    // Pass 2: read content.
    const filename = path.replace(/^CLEO[\\\/]/i, "");
    // __dirname is a Node.js-style global that CLEO Redux exposes — it points to the
    // CLEO\ folder. Using an absolute path avoids ambiguity about the working directory.
    const absPath  = (typeof __dirname !== "undefined")
                     ? (__dirname + "\\" + filename) : null;

    // Attempt A: QuickJS std.loadFile — the engine's own file reader; no CLEO permissions needed.
    // Try multiple path variants because the CWD may differ between CLEO Redux versions.
    if (typeof std !== "undefined" && typeof std.loadFile === "function") {
        for (const p of [absPath, filename, path].filter(Boolean)) {
            try {
                const content = std.loadFile(p);
                if (content) {
                    log("rf: std.loadFile ok len=" + content.length);
                    log("rf: preview=" + content.slice(0, 80));
                    return content;
                }
                log("rf: std.loadFile null for: " + p);
            } catch (e) { log("rf: std ex " + p + ": " + e); }
        }
    } else {
        log("rf: std not available (typeof std=" + typeof std + ")");
    }

    // Attempt B: File class — broken in CLEO Redux 1.4.3 (isOpen always false),
    // kept here as a forward-compatibility safety net for fixed future versions.
    for (const p of [absPath, filename, path.replace(/\\/g, "/"), path].filter(Boolean)) {
        try {
            const f = new File(p, "rb");
            if (!f || !f.isOpen) continue;
            log("rf: File class ok: " + p);
            let result = "";
            while (!f.isAtEnd && result.length < size) {
                const chunk = f.readString(FILE_CHUNK);
                if (!chunk) break;
                result += chunk;
            }
            f.close();
            return result || null;
        } catch (e) {}
    }

    log("rf: all methods failed");
    return null;
}

// ──────────── UTILITIES ────────────

// Safely retrieves the current in-game time.
// Clock.GetTimeOfDay return shape changed between CLEO Redux versions:
//   some return { hours, minutes }, others return [hours, minutes] (array-like object).
// tryParse handles both shapes so the caller doesn't need to care.
function getTimeSafe() {
    const tryParse = (t) => {
        if (!t || typeof t !== "object") return null;
        if ("hours" in t) return { hours: t.hours, minutes: t.minutes || 0 };
        if (0 in t)       return { hours: t[0],    minutes: t[1]       || 0 };
        return null;
    };
    try {
        if (typeof Clock !== "undefined" && Clock.GetTimeOfDay) {
            const r = tryParse(Clock.GetTimeOfDay());
            if (r) return r;
        }
    } catch (e) {}
    try {
        // 00BF: GET_TIME_OF_DAY — opcode fallback for builds without the Clock class.
        const r = tryParse(native("GET_TIME_OF_DAY"));
        if (r) return r;
    } catch (e) {}
    // Last resort default — Python daemon will receive 12:00 if both methods fail.
    return { hours: 12, minutes: 0 };
}
