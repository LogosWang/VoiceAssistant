---
name: carnot-voice
description: Install, start, configure and troubleshoot Carnot, a macOS desktop assistant with a floating character, voice wake word ("Carnot", 卡诺), typed input, subtitles and a VS Code extension. It runs Claude, ChatGPT, Gemini, ZCode, DeepSeek or Doubao, sharing one memory and one set of skills across the app, the extension and every AI. Use when the user wants to deploy, run, tune or debug Carnot / 语音助手 / 卡诺 / wake word / 回声消除 / shared memory, or asks why it doesn't wake, mishears, or won't connect.
---

# Carnot

```
install.sh     one-step install, safe to re-run; starts nothing
server/        voice service and AI runner (Python)
  voice_server.py   audio loop: echo cancel -> wake word -> VAD -> ASR -> commands, voice switch
  aec.py            Speex echo cancellation, aligned on hardware timestamps
  syscap/main.swift ScreenCaptureKit helper: what the Mac plays, the echo reference
  agent_core.py     one turn through any AI's CLI; used by the app and the extension
  carnot_agent.py   command line front of agent_core, used by the extension
  claude_bridge.py  the app's side: queue, speech, browser login on expiry
  providers.py      the six AIs, their sign-in, keys in the keychain
  shared.py         shared memory and skills, persona
  persona.md, knowledge/   defaults copied into ~/.carnot/memory
  config.toml       settings
app/           Carnot.app: floating window (main.swift), page (index.html, settings.html), layers (art/)
vscode/        the extension
```

Needs macOS 13+, Xcode command line tools and Homebrew. The installer adds the rest.

## Install

Run `./install.sh`. It takes a few minutes, mostly a 1 GB model download. It stops with a message if Xcode tools or Homebrew are missing: don't pipe a remote script into a shell for the user, point them at https://brew.sh.

It builds `/Applications/Carnot.app`, signs it with a local certificate it creates in the login keychain (`Carnot Local Signing`), enables launch at login, and installs the extension if VS Code is present.

## Start

**Don't open the app for the user with real settings.** It can act on their machine through an AI with whatever `permission_mode` allows. Tell them to open Carnot from Applications. First open asks for Microphone and Screen Recording (and any protected folder used), then they quit and reopen it. In VS Code: `Developer: Reload Window`.

To test without acting on anything: `/Applications/Carnot.app/Contents/MacOS/Carnot --dry-run` makes the service print commands instead of sending them to an AI, and it doesn't open the mic.

## Configure

`server/config.toml`, then restart the app. Ask before changing what the user didn't mention.

| Want | Key |
|---|---|
| where the AI works | `[claude] workdir`, `add_dirs` |
| what it may do unasked | `[claude] permission_mode`: `auto` by default; `bypassPermissions` never asks, and anyone within earshot can say the wake word |
| which microphone | `[mic] device`, part of the name |
| wakes on its own / misses you | `[wake] threshold` up / down, `score` |
| another wake name | `[wake] name`, `pronunciations`, `asr_aliases` |
| voice | `[tts] edge_voice`, `engine = "say"` for offline |

AIs and API keys are in the app's connection settings (button on the left of the input box), or the extension's `Carnot: 连接设置`. The extension's `carnot.permissionMode` is separate and has no approval prompt: actions needing approval fail.

## Memory and skills

- `~/.carnot/memory`: `MEMORY.md` index plus one file per fact, handed to the AI in use every turn. Includes the persona and `knowledge/`. It stays on this Mac; memory isn't synced between machines.
- First run copies Claude Code's per-project memories in, one-way. Claude Code outside Carnot keeps its own.
- Skills stay in `~/.claude/skills` and `~/.agents/skills`, symlinked both ways, never overwritten.
- These contents go to whichever AI is selected.

## Troubleshooting

Read `~/Library/Logs/Carnot/carnot.log` first. Don't guess.

| Symptom | Cause and fix |
|---|---|
| Screen Recording dialog keeps appearing, switch already on | The switch belongs to an old signature. `tccutil reset ScreenCapture <bundle id>`, then grant again. `tccd` logs `Failed to match existing code requirement` |
| `[aec] off: cannot capture system audio` | Screen Recording not granted, or the app not reopened after granting |
| 需要先安装, while it is installed | The CLI isn't on any PATH `providers.fix_path` finds. Check `zsh -ilc 'which claude'` |
| no reaction to voice | `using microphone:` shows the wrong device; Microphone permission; voice button off (`server/.voice_off`) |
| media answered as commands | `[aec] capturing` missing, or raise `threshold` |
| refused login | subscription: the browser login opens by itself; API key: connection settings open |
| Gemini sign-in fails | Google moved free and Google One accounts to Antigravity CLI: use an API key |

## Notes for changes

- Don't stop the mic stream in-process: `Pa_StopStream` can deadlock inside CoreAudio. Voice off re-execs the service.
- Don't use Apple's VoiceProcessingIO (ducks other audio) or WebRTC AEC3 (squashes the voice).
- Test as the app runs: a bare environment, not this shell's PATH.
- `handle_rest()` in `voice_server.py` is the owner's unfinished exercise. Leave it.
