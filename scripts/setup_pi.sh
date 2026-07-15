#!/usr/bin/env bash
# One-shot setup on the Pi. Run from the repo root: bash scripts/setup_pi.sh
# Re-runnable: safe to use for updates after a git pull too.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "== apt deps =="
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv nodejs npm

echo "== sandbox VM (KVM guest reached over vsock) =="
# qemu + aarch64 UEFI for the guest; cloud-image-utils to build the golden image.
sudo apt-get install -y -qq qemu-system-arm qemu-utils qemu-efi-aarch64 cloud-image-utils
# host vsock endpoint (guest dials the host model gateway over this); persist it.
echo vhost_vsock | sudo tee /etc/modules-load.d/vhost_vsock.conf >/dev/null
sudo modprobe vhost_vsock || true
# /dev/kvm and /dev/vhost-vsock are group 'kvm'; the service user must be in it.
sudo usermod -aG kvm "$USER" || true
echo "NOTE: build the golden guest image once with:  bash vm/build_base.sh"

echo "== python venv =="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "== frontend build =="
(cd frontend && npm install --no-fund --no-audit && npm run build)

echo "== config dir =="
mkdir -p ~/.config/jarvis
touch ~/.config/jarvis/env
chmod 600 ~/.config/jarvis/env
grep -q DEEPSEEK ~/.config/jarvis/env || \
  echo "NOTE: put JARVIS_DEEPSEEK_API_KEY=sk-... into ~/.config/jarvis/env"

echo "== systemd user unit =="
mkdir -p ~/.config/systemd/user
cp scripts/jarvis.service ~/.config/systemd/user/jarvis.service
cp scripts/jarvis-backup.service ~/.config/systemd/user/jarvis-backup.service
cp scripts/jarvis-backup.timer ~/.config/systemd/user/jarvis-backup.timer
chmod +x scripts/backup.sh
systemctl --user daemon-reload
systemctl --user enable jarvis.service
systemctl --user enable --now jarvis-backup.timer 2>/dev/null || true
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"

echo "== done =="
echo "create the login user:   .venv/bin/python -m backend.cli create-user <name>"
echo "start:                   systemctl --user restart jarvis"
echo "logs:                    journalctl --user -u jarvis -f"
