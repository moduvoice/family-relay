#!/usr/bin/env bash
# One-time setup of a dev/test Ubuntu 24.04 instance (arm64 or x86) for Daily Korean Scene.
# Mounts the instance NVMe at /nvme (if any), creates a venv, installs Playwright Chromium and CJK fonts.
# Usage: bash ec2_dev_setup.sh   (idempotent)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! mountpoint -q /nvme; then
  DEV=$(lsblk -dpno NAME,MODEL | awk '/Instance Storage/ {print $1; exit}')
  sudo mkdir -p /nvme
  if [ -n "${DEV:-}" ]; then
    sudo mkfs.ext4 -F -q "$DEV"
    sudo mount "$DEV" /nvme
  fi
  sudo chown "$USER" /nvme
fi
mkdir -p /nvme/dks/{logs,tmp,cache}
export TMPDIR=/nvme/dks/tmp XDG_CACHE_HOME=/nvme/dks/cache PLAYWRIGHT_BROWSERS_PATH=/nvme/dks/ms-playwright

sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip fonts-noto-cjk fonts-noto-color-emoji unzip zstd >/dev/null

if [ ! -x /nvme/dks/venv/bin/python ]; then
  python3 -m venv /nvme/dks/venv
fi
/nvme/dks/venv/bin/pip install -q --upgrade pip
/nvme/dks/venv/bin/pip install -q -r ~/dks/app/requirements.txt playwright
sudo -E /nvme/dks/venv/bin/playwright install-deps chromium >/dev/null
/nvme/dks/venv/bin/playwright install chromium >/dev/null
echo "SETUP_OK $(/nvme/dks/venv/bin/python -c 'import mcp,importlib.metadata as m;print("mcp",m.version("mcp"))')"
