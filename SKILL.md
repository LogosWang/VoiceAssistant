---
name: carnot-voice
description: Install, start, configure and troubleshoot "Carnot", a local voice assistant for Claude Code on macOS. It listens on the Mac's microphone for the wake word "Carnot" (卡诺), transcribes speech offline (sherpa-onnx SenseVoice), runs headless `claude -p`, and speaks the reply (edge-tts). Echo cancellation subtracts everything the Mac plays, so you can interrupt it and media isn't taken for a command. Use when the user wants to set up, deploy, run, tune or debug the voice assistant / 语音助手 / Carnot / 卡诺 / wake word / 回声消除, or asks why it doesn't wake, mishears, or answers media playing in the room.
---

# Carnot voice assistant

Everything is in this folder:

```
install.sh        one-time setup (re-runnable)
run.sh            start on the Mac mic; extra args go to voice_server.py
app/
  voice_server.py   audio loop: AEC -> wake word (KWS) -> VAD endpointing -> ASR -> commands
  aec.py            echo cancellation: Speex MDF + residual suppression, aligned on hardware timestamps
  syscap/main.swift ScreenCaptureKit helper that streams what the Mac plays (the AEC reference)
  claude_bridge.py  runs `claude -p --output-format stream-json`, keeps the session, speaks sentences
  tts.py            edge-tts (falls back to macOS `say`)
  vision.py         camera (ffmpeg) / screen snapshots attached to commands
  config.toml       all settings
  test_clips/command.wav  self-test clip
```

Requirements: macOS 13+, Homebrew, Xcode command line tools, Python 3.11+, Claude Code CLI logged in. About 1.3 GB on disk after install (models + venv, both inside `app/`).

## Install

Run `./install.sh` from this folder (you may run it yourself; it takes a few minutes, mostly the 1 GB model download). It installs `speexdsp` via brew, builds the venv with pinned packages, downloads models, compiles `syscap`, checks for `claude`, and runs a dry-run self-test on `test_clips/command.wav`. A good self-test prints `[wake] Carnot` and an `[asr]` line.

If it stops early it prints why: missing Xcode tools (`xcode-select --install` was launched; rerun after it finishes), missing Homebrew (point the user to https://brew.sh; don't pipe a remote script into bash yourself), or no `claude` CLI.

## Start

**Don't start the server yourself.** It runs Claude with `permission_mode = "bypassPermissions"` and stays in the foreground. Give the user the command to run in their own terminal:

```
"<this folder>/run.sh"
```

`run.sh` kills any previous instance first. Other modes: `run.sh --dry-run` (print transcripts, never call Claude), `run.sh --save-dir /tmp/carnot` (save every recording as wav for debugging).

First start: macOS asks for **Microphone** and **Screen Recording** (系统设置 → 隐私与安全性 → 屏幕与系统录音) for the terminal app. Both are needed; after granting Screen Recording the terminal must be restarted. The menu-bar screen-recording indicator stays on while it runs; that is the system-audio capture, nothing is recorded to disk.

Healthy startup log: `ready, wake word: Carnot`, `[aec] capturing system audio`, `using microphone: <name>`.

## Configure (`app/config.toml`)

Ask before changing what the user didn't mention. Common edits:

| Want | Key |
|---|---|
| where Claude works | `[claude] workdir` (default `~`), `add_dirs` |
| model / safety | `[claude] model`, `permission_mode` (`"auto"` blocks risky actions instead of never asking) |
| which microphone | `[mic] device`: part of the name; empty = system default input |
| wakes on its own | raise `[wake] threshold` (0.20) |
| misses the wake word | lower `threshold` or raise `score` (1.5); raise `[record] gain` for a quiet mic |
| another wake name | `[wake] name`, `pronunciations` (Chinese characters, or CMU phones from `app/models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/en.phone`), `asr_aliases` |
| cuts you off / waits too long | `[record] end_silence` (1.0 s) |
| voice | `[tts] edge_voice`, `edge_rate`; `engine = "say"` for offline |
| no follow-up listening | `[record] follow_up = false` |
| camera | `[vision] camera` name or index (`ffmpeg -f avfoundation -list_devices true -i ""`), needs `brew install ffmpeg` |
| turn echo cancellation off | `[aec] enabled = false` |

Restart (`run.sh`) after editing.

## How the echo cancellation works

These details matter when debugging:

- `syscap` captures system audio through ScreenCaptureKit. No output device is switched and nothing is ducked; media sounds exactly the same. Apple's VoiceProcessingIO was rejected because it ducks other audio about 8 dB, and WebRTC AEC3 squashed the near-end voice. Don't switch back to either.
- Mic frames carry the PortAudio `inputBufferAdcTime`, and syscap records carry their CMSampleBuffer PTS. Both are on the host clock (= `time.monotonic()`), so each 10 ms mic frame is paired with the reference that was playing when it was recorded. A GCC-PHAT check realigns only if the echo falls outside the filter tail (`tail_ms = 400`).
- Speex needs a few seconds of playback to converge, giving ~10–15 dB echo reduction plus residual suppression. While other media is playing, the follow-up window (no wake word) is skipped, so leftover media can't become a command.
- Barge-in: saying the wake word while Carnot speaks mutes the rest of the reply (the Claude task keeps running; "停止" kills it).

## Troubleshooting

Get the actual log lines from the user (or a `--save-dir` recording) before changing anything. Don't guess.

| Symptom | Check |
|---|---|
| `[aec] off: ... Screen Recording permission?` | grant 屏幕与系统录音 to the terminal app, restart the terminal |
| `[aec] system audio capture hears nothing while Carnot speaks` | permission granted but capture is silent: same fix; check that `app/syscap/syscap` runs and prints `capturing` |
| `libspeexdsp not found` | `brew install speexdsp` |
| no reaction at all | `using microphone:` line shows the wrong device → set `[mic] device`; check Microphone permission; run with `--save-dir` and inspect the `[level]` lines (peak ≈ 0 means no audio is arriving) |
| media triggers commands | confirm `[aec] capturing` is present; raise `threshold` |
| `调用 Claude 出错了` | run `claude -p hi` in the same terminal to see the error (login, model access); set `[claude] model = ""` to use the account default |
| `edge-tts` errors offline | it falls back to `say`; or set `engine = "say"` |

## Voice commands

After the wake word:
- 停止 / 取消 / 别说了: kill the task and stop speaking.
- 新对话: start a fresh Claude session.
- 打开/关闭摄像头, 看屏幕 / 停止看屏幕: turn snapshots on or off. A longer sentence such as "打开摄像头看看这是什么" also goes to Claude.
- 谢谢 / 没事了: end the follow-up.

Anything else goes to Claude. Commands spoken while Claude is busy are queued. The session id is in `app/.claude_session` (`claude --resume <id>` opens it in a terminal).

`handle_rest()` in `voice_server.py` is the user's own unfinished exercise (rest mode). Leave it alone unless they ask for it.
