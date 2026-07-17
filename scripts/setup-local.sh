#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
docker compose version >/dev/null
if [[ ! -f .env ]]; then
  cp .env.example .env
  secret=$(openssl rand -hex 32)
  token=$(openssl rand -hex 24)
  password=$(openssl rand -hex 20)
  sed -i.bak "s/replace-with-a-long-random-value/$secret/; s/replace-with-one-time-random-token/$token/; s/change-me/$password/g" .env
  rm -f .env.bak
  echo "Initial setup token: $token"
  echo "Edit .env to set mount paths, host names, and API credentials."
  echo "Then run ./scripts/run-local.sh."
  exit 0
fi
docker compose up -d --build
echo "Service started at http://localhost:8000"
