# EVA Intelligent Resources & Master Architecture Reference
> **Version:** 2.0  
> **Status:** Active Blueprint & Architectural Knowledge Resource  
> **Scope:** 3D Waifu Avatar, Voice Engines, Mood Framework, eDEX CLI, Browser Automation, OS Control, and Memory Systems.

---

## 1. 3D Waifu Avatar Engine & Asset Stack

### Exact Attached Stack in Codebase (`assets/eva/` & `eva_desktop_pet.py`)
Currently, EVA's 3D desktop pet is **not** a single monolithic external app, but a **decoupled, modular WebGL architecture** built from these exact components:

1. **Rendering Engine**: `@pixiv/three-vrm` (v0.6.11 UMD Bundle) + `Three.js` (r128) + `GLTFLoader.js`.
2. **3D Character Model**: Standard VRM 1.0/0.x Humanoid Model (`assets/eva/eva.vrm` — 16.5MB binary model asset).
3. **Spectral Viseme Lip-Sync (Donor: [Violet-Persona](https://github.com/Violet-Persona/Violet-Persona))**:
   - Real-time Web Audio API `AudioContext` + `AnalyserNode` (FFT size 1024, silence gate `0.04`, smoothing `0.6`).
   - Spectral centroid calculation maps low frequencies to open vowels (`aa`), mid frequencies to rounded vowels (`oh`), and high frequencies to sibilants (`ee`).
4. **Humanoid Posture State Machine (Donor: [liqu-companion](https://github.com/liqu-companion/liqu-companion) / [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber))**:
   - Inverse/Forward kinematics posture controller supporting rest pose, breathing weight shift, gaze tracking (`lookAt` cursor), and behavior triggers (`wave`, `think`, `dance`, `stretch`, `celebrate`).

### Alternative 3D Avatar Options & GitHub Repositories

| Option / Project | GitHub Repository | Pros | Cons | Verdict for EVA |
|---|---|---|---|---|
| **Custom Three-VRM (Current)** | Internal (`assets/eva/`) | Ultra-lightweight (RAM < 80MB), native PyWebView window, full control over events & visemes | Requires explicit JS animation loops | **PRIMARY ACTIVE FRONTEND** |
| **Open-LLM-VTuber** | [Open-LLM-VTuber/Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) | Complete Live2D/VRM desktop mode, expression controller | Heavy monolithic python stack | **STUDY / PATTERN DONOR** |
| **WaifuAI Avatar** | [altesco/WaifuAI](https://github.com/altesco/WaifuAI) | Unity-based or web-based 3D character with built-in camera distances | Harder to integrate with PyWebView | **MOOD & PERSONALITY DONOR** |
| **ChatdollKit** | [uezo/ChatdollKit](https://github.com/uezo/ChatdollKit) | Unity 3D VTuber framework with lip sync & speech response | Requires full Unity runtime executable | **MODEL ASSET DONOR ONLY** |

---

## 2. Voice Systems & Waifu Speech Engines

### Current Engine: Microsoft Edge Neural TTS + Web Speech API
- **TTS Engine**: `edge-tts` (Microsoft Neural Synthesizer).
- **Voices**:
  - `en-US-AnaNeural`: Sweet, gentle female waifu voice.
  - `ur-PK-UzmaNeural`: Native Urdu female voice.
  - `hi-IN-SwaraNeural`: Roman Urdu female voice.
- **STT Engine**: Native Web Speech API (`webkitSpeechRecognition`) for hands-free continuous streaming microphone transcription directly inside the UI.

### Advanced Open-Source Voice Engines Matrix

| Voice Engine | GitHub Repository | Features | Waifu Suitability | Latency / Hardware Cost |
|---|---|---|---|---|
| **Edge-TTS** (Active) | Python `edge-tts` package | Free, zero GPU cost, 300+ neural voices (`AnaNeural`) | ⭐⭐⭐⭐⭐ (Very sweet) | Fast (< 300ms network stream) |
| **SileroTTS** | [snakers4/silero-models](https://github.com/snakers4/silero-models) | Completely offline PyTorch C++ / ONNX TTS models | ⭐⭐⭐⭐ (High quality offline) | Minimal CPU footprint |
| **Kokoro-TTS** | [hexgrad/kokoro](https://github.com/hexgrad/kokoro) | Ultra-high quality 82M param open TTS model | ⭐⭐⭐⭐⭐ (Human quality) | Runs locally on CPU/GPU |
| **GPT-SoVITS** | [RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) | Zero-shot voice cloning (5s audio sample required) | ⭐⭐⭐⭐⭐ (Custom anime voice) | Requires VRAM/GPU |
| **Whisper.cpp** | [ggerganov/whisper.cpp](https://github.com/ggerganov/whisper.cpp) | Blazing fast offline C++ speech-to-text | ⭐⭐⭐⭐⭐ (Offline STT) | Extremely low CPU/RAM |

---

## 3. Mood System & Personality Archetype Matrix (Inspiration: WaifuAI)

EVA incorporates a **Probabilistic Mathematical Mood Framework** that dynamically alters EVA's attitude, prompt style, and autonomous proactive triggers based on user interactions.

```text
               ┌─────────────────────────────────────────┐
               │         EVA MOOD VECTOR (M)             │
               │ [Affection, Engagement, Mood, Energy]   │
               └────────────────────┬────────────────────┘
                                    │
       ┌────────────────────────────┼────────────────────────────┐
       ▼                            ▼                            ▼
  PROMPT ADAPTER             BEHAVIOR TRIGGER             AUTONOMOUS TASK
(Archetype Shaper)          (Wave / Smile / Sleep)       (Proactive Follow-up)
```

### The 4 State Parameters
1. **Affection ($A \in [0, 100]$)**: How much EVA trusts and likes the user (increases with polite goals & successful tasks).
2. **Engagement ($E \in [0, 100]$)**: EVA's focus level (increases during active goal execution).
3. **Mood ($M \in [-100, 100]$)**: Positivity vs Frustration (-100 = upset/tsundere, +100 = joyful/genki).
4. **Energy ($N \in [0, 100]$)**: Fatigue indicator (decreases after heavy task loops; low energy triggers rest behavior).

### The 13 Anime Personality Archetypes

```python
ARCHETYPES = {
    "tsundere":  "Cold and harsh initially ('It's not like I wanted to help you!'), but extremely helpful and loyal.",
    "kuudere":   "Calm, logical, cool, emotionless JARVIS tone, speaks with precise efficiency.",
    "dandere":   "Shy, quiet, gentle, speaks softly but executes tasks with extreme precision.",
    "deredere":  "Completely affectionate, energetic, loving, and cheerful assistant.",
    "genki":     "Hyperactive, high-energy, enthusiastic ('Let's do this right now, Boss!').",
    "yandere":   "Obsessively devoted to managing your tasks, protective over your computer system.",
    "teasedere": "Playful, teases the user gently while finishing proposals and tasks effortlessly.",
    "dorodere":  "Sweet on the outside, deeply sarcastic and sharp-witted on the inside.",
    "utsudere":  "Melancholic, sad, quiet, needs encouragement from user.",
    "bakadere":  "Clumsy, super energetic, funny, but surprisingly loyal.",
    "darudere":  "Lazy, casual, speaks in short minimal phrases ('Fine, done. What's next?').",
    "hinedere":  "Cynical, sarcastic, acts unimpressed by hard tasks.",
    "sadodere":  "Playfully strict, demands high efficiency from the user."
}
```

---

## 4. eDEX-UI Sci-Fi CLI Design Specification (`eva_cli.py`)

Inspired by **[eDEX-UI](https://github.com/GitSquared/edex-ui)** (the iconic sci-fi cyberpunk terminal interface):

### Visual & Interactive Rules
- **Minimalist Prompt**: `EVA ❯ ` (Clean, sci-fi prompt).
- **Telemetry Panel**: Live execution status, active goal ID, step count, and system metrics.
- **Rich Terminal Styling**: `rich` library formatting with dark cyan/neon borders (`#00e5ff`).
- **Interactive Commands**:
  - `/debug`: Toggle real-time perception telemetry logs.
  - `/archetype <name>`: Switch EVA's personality archetype on the fly.
  - `/mood`: Print EVA's current Affection/Engagement/Mood/Energy vector.
  - `/run "<goal>"`: Headless one-shot mode for script execution.

---

## 5. Headless Browser Automation & OS Control Architecture

### Browser Automation Matrix

```text
                          BROWSER AUTOMATION HARNESS
                                      │
           ┌──────────────────────────┴──────────────────────────┐
           ▼                                                     ▼
   LAYER A: DETERMINISTIC HANDS                           LAYER B: AI REASONING
  (Playwright CDP Attach)                                (Browser-Use / Agent)
  - Connects to user's real Chrome profile              - Used only when DOM selectors fail
  - Preserves WhatsApp, Gmail, YouTube logins            - Multi-step LLM vision/DOM navigation
  - Zero browser downloads required                      - Controlled by EVA Task Router
```

| Technology | GitHub Repository | Purpose | Integration Plan |
|---|---|---|---|
| **Playwright Python** | [microsoft/playwright-python](https://github.com/microsoft/playwright-python) | Layer A Browser Control (`connect_over_cdp`) | Primary browser execution engine |
| **Browser Use** | [browser-use/browser-use](https://github.com/browser-use/browser-use) | Layer B Site Escalation Engine | Optional plugin for complex un-selectable sites |
| **Agent Browser** | Open-source CDP wrapper | Lightweight headless page scraper | Fast HTML/DOM extraction |

### Application & OS Automation Stack
1. **System Perception**: `ctypes.windll.user32` Win32 API calls for active window titles, process IDs, and cursor coordinates.
2. **App Launch Registry**: `apps.launch_registry` mapping generic requests (e.g. "open chrome", "open vscode") to exact Windows executable paths (`chrome.exe`, `code.cmd`).
3. **OS File Automation**: `skills/os_file_organizer_skill.py` for automated file downloads, folder management, and directory sorting.

---

## 6. Memory & Knowledge Synthesis Architecture

### SQLite FTS5 + Hybrid Vector Stack (Zero-Cost Local Default)

```text
                            MEMORY SYSTEM
                                  │
      ┌───────────────────────────┼───────────────────────────┐
      ▼                           ▼                           ▼
WORKING MEMORY             EPISODIC MEMORY             SEMANTIC MEMORY
(Short-term ledger)        (Task execution logs)       (User facts & prefs)
      │                           │                           │
      └───────────────────────────┼───────────────────────────┘
                                  ▼
                         SQLITE FTS5 SEARCH
                   (Fast, local, 0MB extra RAM)
```

| Memory Component | Capability Donor / Concept | Technical Implementation |
|---|---|---|
| **Working Memory** | Mem0 short-term ledger | In-memory queue of recent step inputs & outputs |
| **Episodic Memory** | OpenHands task replay log | SQLite table `episodic_tasks` with step-by-step resolution |
| **Semantic Memory** | Mem0 entity retrieval + Letta state | SQLite FTS5 table `semantic_facts` with confidence decay |
| **Procedural Skills** | Skills taxonomy (`skills/`) | Python skill modules for A4 proposals, delivery dispatch, etc. |

---

## 7. Complete Open-Source Capability Donors Index

| Project Name | GitHub Link | Category | EVA Donor Contribution |
|---|---|---|---|
| **Violet-Persona** | [Violet-Persona/Violet-Persona](https://github.com/Violet-Persona/Violet-Persona) | Avatar & LipSync | Web Audio API Spectral Centroid Viseme LipSync Engine |
| **liqu-companion** | [liqu-companion/liqu-companion](https://github.com/liqu-companion/liqu-companion) | Avatar Posture | Humanoid IK posture controller & behavior state machine |
| **Open-LLM-VTuber** | [Open-LLM-VTuber/Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) | Desktop Companion | Desktop widget windowing & gaze tracking patterns |
| **WaifuAI** | [altesco/WaifuAI](https://github.com/altesco/WaifuAI) | Mood & Archetype | 4-Parameter Mood Vector & 13 Anime Personality Archetypes |
| **SileroTTS** | [snakers4/silero-models](https://github.com/snakers4/silero-models) | Voice TTS | Offline C++ local waifu voice synthesis models |
| **Kokoro-TTS** | [hexgrad/kokoro](https://github.com/hexgrad/kokoro) | Voice TTS | Lightweight ultra-high quality open-weight TTS engine |
| **eDEX-UI** | [GitSquared/edex-ui](https://github.com/GitSquared/edex-ui) | Sci-Fi UI/CLI | Cyberpunk terminal styling, telemetry tables, ASCII branding |
| **Playwright** | [microsoft/playwright-python](https://github.com/microsoft/playwright-python) | Automation | CDP connection to real Chrome profile for WhatsApp/Web automation |
| **Browser Use** | [browser-use/browser-use](https://github.com/browser-use/browser-use) | Web Agent | Escalation engine for complex web interactions |
| **Mem0** | [mem0ai/mem0](https://github.com/mem0ai/mem0) | Memory Architecture | Working vs Long-Term memory split & entity extraction patterns |





# Don't forget to read .env.example for the .env file structure(thing).