#!/bin/bash
# Downloads the wake word (KWS), VAD and ASR models into server/models.
set -e
cd "$(dirname "$0")" && mkdir -p models && cd models
B=https://github.com/k2-fsa/sherpa-onnx/releases/download
get() { [ -e "$2" ] || { curl -fSL -o "$2.tar.bz2" "$B/$1/$2.tar.bz2" && tar xjf "$2.tar.bz2" && rm "$2.tar.bz2"; }; }
get kws-models sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
get asr-models sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
[ -e silero_vad.onnx ] || curl -fSL -O $B/asr-models/silero_vad.onnx
echo "models ready"
