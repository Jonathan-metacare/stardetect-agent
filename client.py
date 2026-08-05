#!/usr/bin/env python3
import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the Stardetect FastAPI agent a question.")
    parser.add_argument("question", help="User question to send to the agent.")
    parser.add_argument(
        "--image-url",
        help="Optional http(s) image URL or base64 data:image URL to analyze.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8001",
        help="FastAPI base URL. Default: http://127.0.0.1:8001",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Request timeout in seconds. Default: 120",
    )
    args = parser.parse_args()

    try:
        response = ask_agent(args.base_url, args.question, args.timeout, args.image_url)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"invalid JSON response: {exc}", file=sys.stderr)
        return 1

    answer = response.get("answer")
    if not isinstance(answer, str):
        print("response JSON does not contain a string 'answer' field", file=sys.stderr)
        return 1

    print(answer)
    return 0


def ask_agent(
    base_url: str,
    question: str,
    timeout: float,
    image_url: str | None = None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/chat"
    payload_data: dict[str, str] = {"message": question}
    if image_url is not None:
        payload_data["image_url"] = image_url
    payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


if __name__ == "__main__":
    raise SystemExit(main())
