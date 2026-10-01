#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export CVAT_VERSION=v2.77.0 CVAT_HOST=localhost
docker compose --project-directory "$PWD" -f vendor/cvat/docker-compose.yml -f compose.override.yml "$@"
