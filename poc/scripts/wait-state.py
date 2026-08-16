#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


def bool_arg(value: str) -> bool:
    normalized = value.lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("boolean must be true or false")


def matches(state: dict[str, Any], args: argparse.Namespace) -> bool:
    expected = {
        "session_status": args.session_status,
        "display_source": args.display_source,
        "output_connected": args.output_connected,
    }
    for key, value in expected.items():
        if value is not None and state.get(key) != value:
            return False

    audio = state.get("audio")
    if not isinstance(audio, dict):
        return False
    if args.audio_desired is not None and audio.get("desired") != args.audio_desired:
        return False
    if args.audio_actual is not None and audio.get("actual") != args.audio_actual:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="IRLightの状態が条件へ収束するまで待機します。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--session-status")
    parser.add_argument("--display-source")
    parser.add_argument("--output-connected", type=bool_arg)
    parser.add_argument("--audio-desired")
    parser.add_argument("--audio-actual")
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    last_state: dict[str, Any] | None = None
    last_error: Exception | None = None
    url = f"{args.base_url.rstrip('/')}/api/state"

    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state response must be a JSON object")
            last_state = payload
            last_error = None
            if matches(payload, args):
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.5)

    print("期待した状態へ収束しませんでした。", file=sys.stderr)
    if last_error is not None:
        print(f"最後のAPIエラー: {last_error}", file=sys.stderr)
    if last_state is not None:
        print(json.dumps(last_state, ensure_ascii=False, indent=2), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
