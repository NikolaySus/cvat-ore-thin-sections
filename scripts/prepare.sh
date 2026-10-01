#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/ore
if [ ! -d vendor/cvat/.git ]; then
  git clone --depth 1 --branch v2.77.0 https://github.com/cvat-ai/cvat.git vendor/cvat
fi
expected=7dac20c766d7d4297fdf67589436387ab26f7411
actual=$(git -C vendor/cvat rev-parse HEAD)
if [ "$actual" != "$expected" ]; then
  echo "Expected CVAT v2.77.0 ($expected), found $actual" >&2
  exit 1
fi
mkdir -p vendor/cvat/cvat-ui/plugins/ore
cp -R plugin/. vendor/cvat/cvat-ui/plugins/ore/
# Compose paths are relative to this repository, so make the upstream bind mounts explicit.
mkdir -p components
cp -R vendor/cvat/components/. components/
