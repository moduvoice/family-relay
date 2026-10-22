#!/usr/bin/env bash
# One-time setup of a test Ubuntu 24.04 instance (arm64 or x86) for Family Relay - everything for all three
# profiles (aws / local / gpu clients) plus the headless browser for the end-to-end test.
# Expects the app at ~/fr/app and the speaker package at ~/fr/speaker/familyvoice.   Usage: bash ec2_dev_setup.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! mountpoint -q /nvme; then
  DEV=$(lsblk -dpno NAME,MODEL | awk '/Instance Storage/ {print $1; exit}')
  sudo mkdir -p /nvme
  if [ -n "${DEV:-}" ]; then sudo mkfs.ext4 -F -q "$DEV"; sudo mount "$DEV" /nvme; fi
  sudo chown "$USER" /nvme
fi
R=/nvme/fr
mkdir -p $R/{logs,tmp,cache,models}
export TMPDIR=$R/tmp XDG_CACHE_HOME=$R/cache HF_HOME=$R/cache/hf PLAYWRIGHT_BROWSERS_PATH=$R/ms-playwright
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip ffmpeg fonts-noto-cjk bzip2 zstd >/dev/null
[ -x $R/venv/bin/python ] || python3 -m venv $R/venv
P=$R/venv/bin/pip
$P install -q --upgrade pip
$P install -q -r ~/fr/app/requirements.txt -r ~/fr/app/requirements-voice.txt playwright
# speaker module: a local checkout at ~/fr/speaker/familyvoice if present, otherwise from GitHub
if [ -d "$HOME/fr/speaker/familyvoice" ]; then $P install -q -e "$HOME/fr/speaker/familyvoice[device]"; else $P install -q "familyvoice[device] @ git+https://github.com/moduvoice/familyvoice"; fi
sudo -E $R/venv/bin/playwright install-deps chromium >/dev/null
$R/venv/bin/playwright install chromium >/dev/null

# on-device models for FR_PROFILE=local (sherpa-onnx releases): VAD, STT (parakeet 110M int8 + SenseVoice int8
# for non-English), TTS (Piper). LOCAL_MODELS=0 skips them (the aws profile does not need them).
if [ "${LOCAL_MODELS:-1}" = "1" ]; then
GH=https://github.com/k2-fsa/sherpa-onnx/releases/download
cd $R/models
get() { f=$2; [ -e "${f%.tar.bz2}" ] && return 0; wget -q "$GH/$1/$f" && case $f in *.tar.bz2) tar xjf "$f" && rm "$f";; esac; }
get asr-models silero_vad.onnx
get asr-models sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8.tar.bz2
get asr-models sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
get tts-models vits-piper-en_US-amy-low.tar.bz2
fi
# speaker model (ResNet34-LM) into the HF cache once
$R/venv/bin/python -c "from familyvoice.embedder import load_embedder; load_embedder('r34'); print('familyvoice r34 ok')"
du -sh $R/models/* 2>/dev/null | sed "s/^/  /" || true
echo "SETUP_OK $($R/venv/bin/python -c 'import mcp,sherpa_onnx,importlib.metadata as m;print("mcp",m.version("mcp"),"sherpa",sherpa_onnx.__version__)')"
