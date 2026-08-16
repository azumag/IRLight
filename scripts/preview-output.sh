#!/usr/bin/env bash
set -euo pipefail

SOURCE="${1:-rtsp://localhost:8554/output/relay}"
exec ffplay -hide_banner -loglevel warning -fflags nobuffer -flags low_delay "$SOURCE"
