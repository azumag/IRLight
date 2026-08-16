#!/usr/bin/env bash
set -euo pipefail

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg が見つかりません。先にインストールしてください。" >&2
  exit 1
fi

url="${1:-rtmp://127.0.0.1:1935/live/input}"

exec ffmpeg \
  -hide_banner \
  -re \
  -f lavfi -i "testsrc2=size=1280x720:rate=30" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -c:v libx264 \
  -preset veryfast \
  -tune zerolatency \
  -pix_fmt yuv420p \
  -b:v 2500k \
  -maxrate 2500k \
  -bufsize 5000k \
  -g 60 \
  -keyint_min 60 \
  -sc_threshold 0 \
  -c:a aac \
  -b:a 128k \
  -ar 48000 \
  -ac 2 \
  -f flv \
  "$url"
