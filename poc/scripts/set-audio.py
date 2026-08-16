#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(url: str, *, method: str = "GET", payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urlopen(request, timeout=5) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("API response must be a JSON object")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="IRLight Phase 0の音声出力状態を変更します。")
    parser.add_argument("mode", choices=("LIVE", "MUTED"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    try:
        current = request_json(f"{base_url}/api/state")
        audio = current.get("audio")
        if not isinstance(audio, dict) or not isinstance(audio.get("version"), int):
            raise ValueError("state response does not contain audio.version")

        updated = request_json(
            f"{base_url}/api/audio",
            method="PUT",
            payload={"mode": args.mode, "expected_version": audio["version"]},
        )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"APIエラー: HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"操作に失敗しました: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(updated, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
