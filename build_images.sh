#!/usr/bin/env bash
# Builds all OS images used by OS_MAP in blinedcloud_bot.py.
# Run this once on the host (and again after editing Dockerfile.template).
set -euo pipefail

cd "$(dirname "$0")"

declare -A IMAGES=(
  ["ubuntu20"]="ubuntu:20.04"
  ["ubuntu22"]="ubuntu:22.04"
  ["ubuntu24"]="ubuntu:24.04"
  ["debian11"]="debian:11"
  ["debian12"]="debian:12"
)

for key in "${!IMAGES[@]}"; do
  base="${IMAGES[$key]}"
  tag="blinedcloud/${key}-systemd"
  echo "=== Building ${tag} (from ${base}) ==="
  docker build --build-arg BASE_IMAGE="${base}" -t "${tag}" -f Dockerfile.template .
done

echo ""
echo "Done. Built images:"
docker images | grep '^blinedcloud/'
