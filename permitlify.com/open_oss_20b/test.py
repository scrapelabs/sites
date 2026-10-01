"""Non-interactive, online-first smoke checks for Permitlify's model API."""

import argparse
import ipaddress
import json
import math
import os
import re
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn
from urllib.parse import urlsplit

import requests


DEFAULT_API_URL = "https://permitlify.com/ai/v1"
PREFERRED_MODEL = "gpt-oss-20b"


class SmokeTestError(Exception):
    """A failed check with a diagnostic safe to display without redaction."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        """Never echo malformed arguments, which may contain credentials."""
        self.print_usage(sys.stderr)
        self.exit(2, "Error: invalid command-line arguments; use --help.\n")


def positive_timeout(value: str) -> float:
    """Parse a positive, finite connect/read timeout in seconds."""
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive and finite")
    return number


def positive_tokens(value: str) -> int:
    """Parse a positive integer token budget."""
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("max-tokens must be positive")
    return number


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Build the CLI without putting secret defaults in help output."""
    parser = SafeArgumentParser(
        description="Check model discovery, JSON chat, and streaming chat.",
        epilog=(
            "Remote requests require HTTPS and PERMITLIFY_AI_API_KEY. "
            "Plaintext HTTP with a key is permitted only for loopback hosts; "
            "http://127.0.0.1:8010/v1 may be used without a key. "
            "URLs must not contain userinfo, query strings, or fragments. "
            "Redirects and automatic proxy/.netrc settings are disabled. "
            "Output is status-only, never raw response bodies."
        ),
    )
    parser.add_argument(
        "--api-url", default=DEFAULT_API_URL,
        help=f"API base URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("PERMITLIFY_AI_API_KEY"),
        help="Override PERMITLIFY_AI_API_KEY (prefer the environment to avoid shell history)",
    )
    parser.add_argument(
        "--model",
        help="Override discovery; otherwise prefer gpt-oss-20b, then the first model",
    )
    parser.add_argument(
        "--timeout", type=positive_timeout, default=120.0,
        help=("Positive finite connect/read timeout in seconds (default: 120); "
              "also sets a streaming progress deadline"),
    )
    parser.add_argument("--no-stream", action="store_true", help="Skip streaming chat")
    parser.add_argument(
        "--responses", action="store_true",
        help="Also check /responses (opt-in; native backend support may vary)",
    )
    parser.add_argument(
        "--prompt", default="What is the capital of France? Answer briefly.",
        help="Prompt used for all inference checks",
    )
    parser.add_argument(
        "--max-tokens", type=positive_tokens, default=256,
        help="Output token budget, including reasoning (default: 256)",
    )
    return parser.parse_args(argv)


def validate_connection(base_url: str, api_key: str | None) -> str:
    """Reject unsafe destinations before constructing any authenticated request."""
    try:
        parsed = urlsplit(base_url)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.port != 0
            and not any(char.isspace() or ord(char) < 32 for char in base_url)
            and not any(char in base_url for char in ("\\", "?", "#"))
        )
    except ValueError:
        valid = False
    if not valid:
        raise SmokeTestError(
            "Invalid API URL; use an HTTP(S) base URL without credentials, query, or fragment."
        )
    host = parsed.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if parsed.scheme != "https" and not loopback:
        raise SmokeTestError(
            "Remote API requests require HTTPS; plaintext HTTP is loopback-only."
        )
    if not api_key and not loopback:
        raise SmokeTestError("Set PERMITLIFY_AI_API_KEY for remote API requests.")
    if api_key and not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", api_key):
        raise SmokeTestError("Invalid Bearer key format; check PERMITLIFY_AI_API_KEY.")
    return base_url.rstrip("/")


def parse_json(data: bytes | str) -> dict[str, Any]:
    """Reject invalid JSON, encoding and API errors without echoing the body."""

    def reject_constant(value: str) -> NoReturn:
        raise ValueError("Nonstandard JSON number")

    try:
        value = json.loads(data, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise SmokeTestError("Malformed JSON response or SSE data.") from None
    if not isinstance(value, dict):
        raise SmokeTestError("Expected a JSON object.")
    if value.get("error") is not None:
        raise SmokeTestError("API returned an error object.")
    return value


@dataclass
class SmokeClient:
    """Small requests adapter shared by the three API checks."""

    session: requests.Session
    base_url: str
    api_key: str | None
    timeout: float

    def request(
        self, path: str, payload: dict[str, Any] | None = None, *, stream: bool = False
    ) -> requests.Response:
        """Apply authentication, timeouts and redirect policy to every request."""
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.session.request(
            "GET" if payload is None else "POST",
            self.base_url + path,
            headers=headers,
            json=payload,
            timeout=self.timeout,
            allow_redirects=False,
            stream=stream,
        )
        if not 200 <= response.status_code < 300:
            response.close()
            raise SmokeTestError(
                f"{path}: HTTP {response.status_code} (redirects are not followed)."
            )
        return response

    def json(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read a JSON response, always closing it on success or failure."""
        with self.request(path, payload) as response:
            return parse_json(response.content)


def discover_model(body: dict[str, Any], override: str | None) -> str:
    """Select the stable live alias if advertised, never guess a stale model ID."""
    models = body.get("data")
    if not isinstance(models, list) or not models:
        raise SmokeTestError("Model discovery returned no valid models.")
    names = []
    for model in models:
        name = model.get("id") if isinstance(model, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise SmokeTestError("Model discovery returned a malformed model ID.")
        names.append(name)
    return override or (PREFERRED_MODEL if PREFERRED_MODEL in names else names[0])


def first_choice(body: dict[str, Any]) -> dict[str, Any]:
    """Require the single choice requested by this smoke client."""
    choices = body.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        raise SmokeTestError("Malformed chat choices.")
    index = choices[0].get("index", 0)
    if type(index) is not int or index != 0:
        raise SmokeTestError("Unexpected chat choice index.")
    return choices[0]


def require_answer(content: Any) -> None:
    """Reasoning, nulls and whitespace are not an assistant answer."""
    if not isinstance(content, str) or not content.strip():
        raise SmokeTestError("No non-empty assistant answer was returned.")


def check_chat(body: dict[str, Any]) -> None:
    """Verify JSON chat produced a complete answer rather than just reasoning."""
    choice = first_choice(body)
    if choice.get("finish_reason") != "stop":
        raise SmokeTestError(
            "Chat did not complete normally; consider increasing --max-tokens."
        )
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise SmokeTestError("Malformed assistant message.")
    require_answer(message.get("content"))


def sse_lines(response: requests.Response, deadline: float) -> Iterator[str]:
    """Handle CR, LF and split CRLF without iter_lines' chunk-boundary ambiguity."""
    pending = bytearray()
    after_cr = False
    for chunk in response.iter_content(chunk_size=1):
        if time.monotonic() >= deadline:
            raise SmokeTestError("Streaming exceeded --timeout.")
        for byte in chunk:
            if after_cr and byte == 10:
                after_cr = False
                continue
            after_cr = byte == 13
            if byte in (10, 13):
                try:
                    yield pending.decode("utf-8")
                except UnicodeError:
                    raise SmokeTestError("Malformed UTF-8 in SSE stream.") from None
                pending.clear()
            else:
                pending.append(byte)


def sse_events(response: requests.Response, deadline: float) -> Iterator[str]:
    """Read UTF-8 SSE frames, including multiline data and comment heartbeats."""
    data: list[str] = []
    event = ""
    for line in sse_lines(response, deadline):
        if not line:
            if event == "error":
                raise SmokeTestError("SSE stream reported an error event.")
            if data:
                yield "\n".join(data)
            data, event = [], ""
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
            elif field == "event":
                event = value
            elif field not in ("id", "retry"):
                raise SmokeTestError("Malformed SSE field.")
    raise SmokeTestError("SSE stream ended without a complete [DONE] event.")


def check_stream(response: requests.Response, deadline: float) -> None:
    """Require actual content, normal completion and the terminal SSE marker."""
    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type != "text/event-stream":
        raise SmokeTestError("Expected a text/event-stream response.")
    answer: list[str] = []
    finished = False
    for event in sse_events(response, deadline):
        if event == "[DONE]":
            if not finished:
                raise SmokeTestError("SSE stream is missing normal chat completion.")
            require_answer("".join(answer))
            return
        body = parse_json(event)
        if body.get("choices") == [] and isinstance(body.get("usage"), dict):
            continue
        choice = first_choice(body)
        delta = choice.get("delta")
        if finished or not isinstance(delta, dict):
            raise SmokeTestError("Malformed or out-of-order SSE chat delta.")
        if delta.get("role", "assistant") != "assistant":
            raise SmokeTestError("Unexpected SSE message role.")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise SmokeTestError("Malformed SSE assistant content.")
            answer.append(content)
        finish = choice.get("finish_reason")
        if finish is not None:
            if finish != "stop":
                raise SmokeTestError(
                    "Stream did not complete normally; consider increasing --max-tokens."
                )
            finished = True


def check_responses(body: dict[str, Any]) -> None:
    """Check the optional Responses API without counting reasoning as output."""
    if body.get("status") != "completed" or not isinstance(body.get("output"), list):
        raise SmokeTestError("Responses API did not return a completed response.")
    answer: list[str] = []
    for item in body["output"]:
        if not isinstance(item, dict):
            raise SmokeTestError("Malformed Responses output item.")
        if item.get("type") != "message":
            continue
        if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
            raise SmokeTestError("Malformed Responses assistant message.")
        if item.get("status", "completed") != "completed":
            raise SmokeTestError("Responses assistant message is incomplete.")
        for part in item["content"]:
            if not isinstance(part, dict):
                raise SmokeTestError("Malformed Responses content.")
            if part.get("type") == "output_text":
                if not isinstance(part.get("text"), str):
                    raise SmokeTestError("Malformed Responses output text.")
                answer.append(part["text"])
    require_answer("".join(answer))


def main(argv: Sequence[str] | None = None) -> int:
    """Run requested checks; return nonzero on the first failure, without input()."""
    if sys.platform == "win32":
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        base_url = validate_connection(args.api_url, args.api_key)
        if not args.prompt.strip() or (args.model is not None and not args.model.strip()):
            raise SmokeTestError("Prompt and explicit model must not be empty.")
        with requests.Session() as session:
            # Do not silently substitute .netrc credentials or forward a local key
            # through an environment-configured proxy.
            session.trust_env = False
            client = SmokeClient(session, base_url, args.api_key, args.timeout)
            model = discover_model(client.json("/models"), args.model)
            print("Model discovery: OK")
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": args.prompt}],
                "max_tokens": args.max_tokens,
                "temperature": 0.0,
            }
            check_chat(client.json("/chat/completions", payload))
            print("JSON chat: OK (non-empty, completed assistant answer)")
            if not args.no_stream:
                deadline = time.monotonic() + args.timeout
                with client.request(
                    "/chat/completions", {**payload, "stream": True}, stream=True
                ) as response:
                    check_stream(response, deadline)
                print("Streaming chat: OK (answer, completion and [DONE])")
            if args.responses:
                check_responses(client.json(
                    "/responses",
                    {
                        "model": model,
                        "input": args.prompt,
                        "max_output_tokens": args.max_tokens,
                        "temperature": 0.0,
                    },
                ))
                print("Responses API: OK")
    except SmokeTestError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException:
        # Request exceptions can include authenticated URLs and server bodies.
        print("FAILED: network, TLS or timeout error contacting the API.", file=sys.stderr)
        return 1
    print("All requested smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
