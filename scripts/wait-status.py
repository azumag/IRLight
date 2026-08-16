#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.request import urlopen


def bool_arg(value: str) -> bool:
    normalized = value.lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("boolean must be true or false")


def matches(payload: dict[str, object], args: argparse.Namespace) -> bool:
    control = payload.get("control")
    runtime = payload.get("runtime")
    if not isinstance(control, dict) or not isinstance(runtime, dict):
        return False

    expected_runtime = {
        "session_status": args.session_status,
        "video_source": args.video_source,
        "actual_audio_mode": args.audio_actual,
    }
    for key, value in expected_runtime.items():
        if value is not None and runtime.get(key) != value:
            return False

    if args.audio_desired is not None and control.get("audio_mode") != args.audio_desired:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="IRLight Phase 0の/api/statusが期待状態へ収束するまで待機します。"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--session-status")
    parser.add_argument("--video-source")
    parser.add_argument("--audio-desired")
    parser.add_argument("--audio-actual")
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    last_state: dict[str, object] | None = None
    last_error: Exception | None = None
    url = f"{args.base_url.rstrip('/')}/api/status"

    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("status response must be a JSON object")
            last_state = payload
            last_error = None
            if matches(payload, args):
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0
        except (OSError, ValueError) as exc:
            last_error = exc
        time.sleep(0.25)

    print("期待した状態へ収束しませんでした。", file=sys.stderr)
    if last_error is not None:
        print(f"最後のAPIエラー: {last_error}", file=sys.stderr)
    if last_state is not None:
        print(json.dumps(last_state, ensure_ascii=False, indent=2), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
