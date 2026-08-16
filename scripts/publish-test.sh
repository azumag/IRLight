#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-rtmp://localhost:1935/live/input}"
exec ffmpeg -hide_banner -loglevel warning -re \
  -f lavfi -i "testsrc2=size=1280x720:rate=30" \
  -f lavfi -i "sine=frequency=880:sample_rate=48000" \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -pix_fmt yuv420p -g 60 -keyint_min 60 -sc_threshold 0 -b:v 3500k \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  -f flv "$TARGET"
