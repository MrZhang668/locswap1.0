#!/usr/bin/env bash
# Run ON THE SERVER as root. Idempotent: deploy/push.sh runs it every time and
# it does nothing once the machine is ready.
#
# Written for Ubuntu 22.04 / 24.04 — pick that image when creating the ECS
# instance. Alibaba Cloud Linux is not supported by Docker's install script.
set -euo pipefail

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  . /etc/os-release
  case "$ID" in
    ubuntu|debian) ;;
    *) echo "[FAIL] $PRETTY_NAME: use an Ubuntu 22.04/24.04 image (Docker's installer does not support it)"; exit 1 ;;
  esac
  echo "== installing Docker"
  # Outside the mainland the official installer is reachable directly.
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
docker compose version

# Swap: the build (pip resolving torch, npm) and the first index rebuild both
# peak above steady-state memory. On a 4 GB instance swap turns an OOM kill into
# a few slow seconds.
if [ "$(swapon --noheadings | wc -l)" = "0" ]; then
  echo "== adding a 4 GB swap file"
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Container logs grow without bound by default; on a 40 GB disk that is the
# outage you find in six months.
if [ ! -f /etc/docker/daemon.json ]; then
  echo '{"log-driver":"json-file","log-opts":{"max-size":"20m","max-file":"5"}}' > /etc/docker/daemon.json
  systemctl restart docker
fi
echo "[ok] server ready"
