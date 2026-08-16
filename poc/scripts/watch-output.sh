#!/usr/bin/env bash
set -euo pipefail

if ! command -v ffplay >/dev/null 2>&1; then
  echo "ffplay が見つかりません。FFmpeg一式をインストールしてください。" >&2
  exit 1
fi

url="${1:-rtmp://127.0.0.1:1935/output/stream}"

exec ffplay \
  -hide_banner \
  -loglevel warning \
  -fflags nobuffer \
  -flags low_delay \
  -framedrop \
  -analyzeduration 1000000 \
  -probesize 1000000 \
  "$url"
