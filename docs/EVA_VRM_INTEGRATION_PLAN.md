# EVA × AVATAR — Companion Body Integration Plan

> **Decision:** AVATAR (github.com/ARPAHLS/avatar) is EVA's body/presence layer.
> EVA Core (JarvisEngine, memory, skills, agent loop) stays ours — AVATAR is driven
> over its built-in **local agent bus** (HTTP + MCP on `127.0.0.1:47903`).

---

## 1. Why AVATAR (candidate comparison)

| | **AVATAR** | Liqu Companion | open-vrm-companion |
|---|---|---|---|
| License | **MIT** | ⚠️ **no code license** (only `LICENSE-MODEL/` — legally "all rights reserved", cannot fork/modify) | MIT |
| Desktop overlay (always-on-top) | ✅ Electron | ✅ Electron | ❌ browser tab only |
| Windows support | ✅ (source + installer) | ✅ | ✅ (browser) |
| VRM / VRMA | ✅ `.vrm` + `.vrma` motion clips | ✅ `.vrm` + 28 behaviors | ✅ `.vrm` + BVH |
| Lip-sync | ✅ system/mic/tab audio | ✅ (API TTS only) | ✅ TTS |
| **External control API** | ✅ **HTTP + WebSocket + MCP loopback bus, documented** | ❌ none (internal IPC only) | ⚠️ Flask routes but they are its own chat backend |
| Fit with EVA brain | **Best — purpose-built command layer** | Body great, but no handle to drive it | Chat backend would fight EVA's brain |

**Winner: AVATAR.** It is the only candidate with a documented, token-authenticated,
loopback-only command API (`animation.play`, `avatar.set`, `environment.set`,
`audio.source`) plus a Streamable-HTTP **MCP** endpoint — exactly the seam between
"EVA brain" and "companion body". Liqu was the closest feature-for-feature, but with
no code license it cannot legally be adopted or modified.

---

## 2. What is set up and verified (done)
(nothin is setup and verified, it's just from previous code..)

| Item | Value |
|---|---|
| Runtime location | `D:\eva_runtime\avatar` (cloned; **C: drive is 100% full — 0 bytes free**, everything heavy lives on D:) |
| App source | `D:\eva_runtime\avatar\avatar` (Electron + Vite + three.js + @pixiv/three-vrm) |
| EVA's model | `assets/eva/eva.vrm` → copied to `src/assets/avatars/avatar4.vrm` → bundled as **Avatar 4** |
| Config | `C:\Users\Microsoft\AppData\Roaming\avatar\config.yaml` — `avatarId: avatar4`, `agentBus.enabled: true`, `port: 47903`, `requireToken: false` |
| Agent bus | `POST http://127.0.0.1:47903/v1/command`, `GET http://127.0.0.1:47903/v1/state`, `ws://127.0.0.1:47903/v1/socket`, MCP at `http://127.0.0.1:47903/mcp` |
| Background run | Started via the launch command below (currently PID 17484); log at `D:\eva_runtime\avatar-desktop.log` |

### Launch command (important flags)

On this machine the GPU process crashes (`exit_code=34`), which disposes the renderer
frame and makes bus commands silently no-op. **Always launch with GPU disabled:**

```bash
cd /d/eva_runtime/avatar/avatar
NODE_ENV=production ./node_modules/.bin/electron . --disable-gpu --enable-unsafe-swiftshader
```

(First-time setup on a fresh clone: `npm install` with `npm_config_cache` on D:, then
`node node_modules/electron/install.js` — npm 12 blocks Electron's postinstall script;
if extraction stalls, unzip the cached electron zip into `node_modules/electron/dist`
with PowerShell `Expand-Archive` and write `path.txt` containing `electron.exe`.)

### Voice layer verified end-to-end (2026-10-03)

```
python core/avatar_bus.py            -> bus OK, avatar4 on stage, play OK (exit 0)
JarvisEngine().log("SUCCESS")        -> sent 2 cmds; /v1/state overlay = vrma-02 (Greeting) ✅
notify("THINKING") x2                -> overlay = vrma-06 (Model Pose); 2nd coalesced (0 sent) ✅
dead-bus circuit breaker            -> 3 failed, 2 skipped, never raised (asserts passed) ✅
edge-tts synthesize + play + on_speak -> played=True, lipsync source = system (exit 0) ✅
py_compile: avatar_bus/engine/speech_engine/eva/eva_cli/eva_desktop_pet -> exit 0 ✅
```

### Verified end-to-end (2026-10-03)

```
GET  /v1/state          → current.avatarId = "avatar4"        ✅ EVA's model on stage
POST avatar.set avatar1  → ok:true, config.yaml rewritten     ✅ commands actually apply
POST avatar.set avatar4  → ok:true, EVA back on stage         ✅
POST animation.play "Peace Sign" mode:once → ok:true (vrma-03) ✅
POST environment.set color #101828             → ok:true       ✅
POST audio.source microphone                   → ok:true       ✅
GET  /v1/state          → catalog: 4 avatars, 9 animations,
                          3 environments, 5 audio sources     ✅
```

---

## 3. Architecture: brain stays ours, body is driven

```
        EVA (Python)                                AVATAR (Electron, MIT)
┌────────────────────────────┐              ┌─────────────────────────────────┐
│ eva.py / eva_cli.py        │              │  transparent always-on-top      │
│ core/engine.py (Jarvis)    │  HTTP/WS     │  VRM renderer (eva.vrm =        │
│ core/agent_loop.py         │ ───────────▶ │  Avatar 4) + SpringBone +       │
│ core/speech_engine.py      │  127.0.0.1   │  VRMA motion + expressions     │
│ skills/*                   │  :47903      │                                 │
└────────────────────────────┘              └─────────────────────────────────┘
   agent state events                            │ listens to device output
   → animation / environment                     ▼
   TTS audio ──────────────────────────▶ lip-sync (mouth follows EVA's voice)
```

### 3.1 State → presence mapping (agent loop → body)

| EVA state (`core/engine.py`) | Command |
|---|---|
| `IDLE` | `animation.default` (loop) |
| `THINKING` | `animation.play {id:"Model Pose"/"Squat", mode:"once"}` |
| `WORKING` | `animation.play {id:"Spin", mode:"once"}` or keep default |
| `SUCCESS` | `animation.play {id:"Greeting", mode:"once"}` |
| `NEEDS_ATTENTION` | `environment.set {type:"color", value:"#ffcf70"}` |
| speak / laugh / dance intents from LLM | `animation.play` one-shots (`Peace Sign`, `Shoot`, …) |

One-shots **must** send `"mode":"once"` — omitting mode persists the selection into
`config.yaml` (menu-pick semantics).

### 3.2 Voice + lip-sync (EVA stays the TTS owner)

EVA's `NeuralVoiceSynthesizer` (edge-tts) generates and plays the audio; the
companion only *listens* to the default output device (`system` audio source) and
moves the mouth from amplitude — so swapping TTS engines later (ElevenLabs, local)
changes nothing in the brain/body contract.

**Bug found & fixed while wiring this:** `playsound` is not installed on this
machine, so `eva.py`, `eva_cli.py` and `eva_desktop_pet.py` were silently dropping
every TTS clip — EVA was mute (and the mouth could never move). Added
`core/speech_engine.play_audio_file()`: playsound → pydub+ffmpeg → WAV →
`winsound` (SND_SYNC) → ffplay fallback chain, and pointed all three callers at it.

**Wiring points (done):**
- `core/engine.py::log()` → `companion.notify_engine_event(level, stream)` — THINKING → "Model Pose", SUCCESS → "Greeting", WARN → amber ambience (cooldowns: 12s/12s/30s).
- `eva.py::play_speech_bg` / `eva_cli.py::play_tts` → `companion.on_speak()` before playback — pins lip-sync to `system` on first speech.
- All events run on a daemon worker queue with a 3-failure/30s circuit breaker — a closed companion window can never block or crash EVA Core.

### 3.3 Proposed bridge module (next step): `core/avatar_bus.py`

```python
"""EVA brain -> AVATAR companion body (local agent bus)."""
import os, requests

BUS = os.getenv("AVA_BUS_URL", "http://127.0.0.1:47903")
# token only when config.yaml sets agentBus.requireToken: true
TOKEN = os.getenv("AVA_BUS_TOKEN")

def _post(command: str, payload: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    r = requests.post(f"{BUS}/v1/command", json={"command": command, "payload": payload or {}},
                      headers=headers, timeout=3)
    r.raise_for_status()
    return r.json()

def state() -> dict:          return requests.get(f"{BUS}/v1/state", timeout=3).json()
def play(anim: str) -> dict:  return _post("animation.play", {"id": anim, "mode": "once"})
def set_env(color: str) -> dict: return _post("environment.set", {"type": "color", "value": color})
def idle() -> dict:           return _post("animation.default")
```

Wire it into `core/engine.py` at the existing state-transition points (the same place
that drives the pet window visualizer today) inside a try/except so a down companion
never breaks automation.

---

## 4. Hardening / open items

1. **C: drive is full (143 GB, 0–212 MB free).** Windows/user-data writes live on C:;
   this will bite Electron caches, temp files and EVA itself. Free space before daily use.
2. **Re-enable the bus token** (Settings → Agents → Require token, or set
   `agentBus.requireToken: true` and store the token in EVA's `.env` as `AVA_BUS_TOKEN`).
   Loopback-only is safe for a dev box, but the token costs nothing.
3. **GPU flag:** either always launch with `--disable-gpu` (wrapper script) or fix the
   GPU driver crash — otherwise commands return `ok:true` but never reach the window.
4. **Lip-sync source** is `system` by default; switching to `microphone` is a user
   setting (`audio.source`) — do not expose it through MCP-driven automation.
5. **Custom animations:** drop `.vrma` files in a folder and set
   `directories.animations` to `mode: custom` to give EVA a real dance clip.
6. **Official installer path (optional):** `AVATAR-Setup-0.8.0.exe` + Settings →
   Directories → Avatars → Custom = no source build needed on other machines.
