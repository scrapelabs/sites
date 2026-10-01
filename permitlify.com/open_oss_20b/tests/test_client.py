"""Offline CLI regressions; every HTTP request is intercepted at the transport."""

import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

import requests


CLIENT = Path(__file__).resolve().parents[1] / "test.py"
API_URL = "https://permitlify.com/ai/v1"
TEST_KEY = "unit-test-credential-not-a-real-key"


class ResponseBody(io.BytesIO):
    """Model urllib3's release hook after a response is fully consumed."""

    def release_conn(self) -> None:
        self.close()


def json_response(body: object, status: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(body).encode("utf-8")
    response._content_consumed = True
    return response


def models(*names: str) -> requests.Response:
    return json_response(
        {"object": "list", "data": [{"id": name, "object": "model"} for name in names]}
    )


def chat(content: object = "Paris.", finish: object = "stop") -> requests.Response:
    return json_response(
        {
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ],
        }
    )


def chunk(content: object = None, finish: object = None, **delta: object) -> dict:
    if content is not None:
        delta["content"] = content
    return {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def sse(*events: object) -> bytes:
    return "".join(
        f"data: {event if isinstance(event, str) else json.dumps(event)}\n\n"
        for event in events
    ).encode("utf-8")


def stream_response(body: bytes | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    response.raw = ResponseBody(
        body if body is not None else sse(chunk("Paris."), chunk(finish="stop"), "[DONE]")
    )
    return response


def responses_body(text: object = "Paris.", status: str = "completed") -> dict:
    return {
        "object": "response",
        "status": status,
        "error": None,
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ],
    }


class ClientTests(unittest.TestCase):
    def run_client(
        self,
        args: tuple[str, ...] = (),
        replies: tuple = (),
        key: str | None = TEST_KEY,
        platform: str = "linux",
    ) -> tuple[int, str, list]:
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {} if key is None else {"PERMITLIFY_AI_API_KEY": key}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("sys.argv", [str(CLIENT), *args]),
            patch("sys.platform", platform),
            patch("builtins.input") as prompt,
            patch("requests.sessions.Session.request", autospec=True) as transport,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            transport.side_effect = list(replies)
            try:
                runpy.run_path(str(CLIENT), run_name="__main__")
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
            prompt.assert_not_called()
        self.assertIsInstance(code, int)
        return code, stdout.getvalue() + stderr.getvalue(), transport.call_args_list

    def assert_failure(self, result: tuple[int, str, list]) -> None:
        code, output, _ = result
        self.assertNotEqual(code, 0, output)
        self.assertNotIn("All requested smoke tests passed", output)
        self.assertNotIn(TEST_KEY, output)

    def test_default_discovers_stable_alias_and_checks_chat_and_stream(self) -> None:
        streaming = stream_response()
        code, output, calls = self.run_client(
            replies=(models("other-model", "gpt-oss-20b"), chat(), streaming)
        )
        self.assertEqual(code, 0, output)
        self.assertIn("All requested smoke tests passed", output)
        self.assertEqual([call.args[1] for call in calls], ["GET", "POST", "POST"])
        self.assertEqual(
            [call.args[2] for call in calls],
            [f"{API_URL}/models", f"{API_URL}/chat/completions", f"{API_URL}/chat/completions"],
        )
        for call in calls:
            self.assertFalse(call.args[0].trust_env)
            self.assertEqual(call.kwargs["headers"]["Authorization"], f"Bearer {TEST_KEY}")
            self.assertEqual(call.kwargs["timeout"], 120.0)
            self.assertIs(call.kwargs["allow_redirects"], False)
        for call in calls[1:]:
            self.assertEqual(call.kwargs["json"]["model"], "gpt-oss-20b")
            self.assertIn(call.kwargs["json"]["max_tokens"], (128, 256))
        self.assertTrue(calls[2].kwargs["stream"])
        self.assertTrue(calls[2].kwargs["json"]["stream"])
        self.assertTrue(streaming.raw.closed)
        self.assertNotIn(TEST_KEY, output)

    def test_falls_back_to_first_discovered_model(self) -> None:
        code, output, calls = self.run_client(
            ("--no-stream",), (models("live-alias", "second-alias"), chat())
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(calls[1].kwargs["json"]["model"], "live-alias")
        self.assertEqual(len(calls), 2)

    def test_cli_overrides_and_responses_are_opt_in(self) -> None:
        code, output, calls = self.run_client(
            (
                "--api-url", "https://example.test/custom/v1/", "--api-key", "override",
                "--model", "chosen", "--timeout", "3.5", "--no-stream", "--responses",
                "--prompt", "Say hello.", "--max-tokens", "512",
            ),
            (models("gpt-oss-20b"), chat(), json_response(responses_body())),
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(calls[0].args[2], "https://example.test/custom/v1/models")
        self.assertEqual(calls[2].args[2], "https://example.test/custom/v1/responses")
        for call in calls:
            self.assertEqual(call.kwargs["timeout"], 3.5)
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer override")
            self.assertIs(call.kwargs["allow_redirects"], False)
        self.assertEqual(calls[1].kwargs["json"]["model"], "chosen")
        self.assertEqual(calls[1].kwargs["json"]["messages"], [{"role": "user", "content": "Say hello."}])
        self.assertEqual(calls[1].kwargs["json"]["max_tokens"], 512)
        self.assertEqual(calls[2].kwargs["json"]["input"], "Say hello.")
        self.assertEqual(calls[2].kwargs["json"]["max_output_tokens"], 512)

    def test_loopback_can_be_unauthenticated(self) -> None:
        for host in ("127.0.0.1", "[::1]", "localhost"):
            with self.subTest(host=host):
                code, output, calls = self.run_client(
                    ("--api-url", f"http://{host}:8010/v1", "--no-stream"),
                    (models("gpt-oss-20b"), chat()), key=None,
                )
                self.assertEqual(code, 0, output)
                for call in calls:
                    self.assertNotIn("Authorization", call.kwargs["headers"])

    def test_key_may_be_sent_over_loopback_http(self) -> None:
        code, output, _ = self.run_client(
            ("--api-url", "http://127.0.0.1:8010/v1", "--no-stream"),
            (models("gpt-oss-20b"), chat()),
        )
        self.assertEqual(code, 0, output)

    def test_remote_without_key_fails_before_network(self) -> None:
        result = self.run_client(key=None)
        self.assert_failure(result)
        self.assertIn("PERMITLIFY_AI_API_KEY", result[1])
        self.assertEqual(result[2], [])

    def test_unsafe_or_credential_bearing_urls_fail_without_echo(self) -> None:
        for url in (
            "http://example.test/v1", "http://127.0.0.1.example.test/v1",
            f"https://user:{TEST_KEY}@example.test/v1",
            f"https://example.test/v1?key={TEST_KEY}",
            f"https://example.test/v1#{TEST_KEY}",
            "ftp://example.test/v1", "https:///v1", "https://example.test:bad/v1",
            "https://example.test\\@127.0.0.1/v1", "https://example.test/\nv1",
        ):
            with self.subTest(url=url):
                result = self.run_client(("--api-url", url))
                self.assert_failure(result)
                self.assertNotIn(url, result[1])
                self.assertEqual(result[2], [])

    def test_invalid_limits_fail_before_network(self) -> None:
        for flag, value in (
            ("--timeout", "0"), ("--timeout", "-1"), ("--timeout", "nan"),
            ("--timeout", "inf"), ("--timeout", "-inf"), ("--timeout", "oops"),
            ("--max-tokens", "0"), ("--max-tokens", "-1"), ("--max-tokens", "1.5"),
        ):
            with self.subTest(flag=flag, value=value):
                result = self.run_client((flag, value))
                self.assert_failure(result)
                self.assertEqual(result[2], [])

    def test_bad_key_and_empty_prompt_are_not_sent(self) -> None:
        for key in ("bad\r\nkey", "bad key", "\u2603"):
            with self.subTest(key=key):
                result = self.run_client(key=key)
                self.assert_failure(result)
                self.assertNotIn(key, result[1])
                self.assertEqual(result[2], [])
        result = self.run_client(("--prompt", "   "))
        self.assert_failure(result)
        self.assertEqual(result[2], [])

    def test_cli_errors_do_not_echo_unrecognized_secrets(self) -> None:
        result = self.run_client(("--unknown", TEST_KEY))
        self.assert_failure(result)
        self.assertEqual(result[2], [])

    def test_http_errors_and_redirects_fail_without_dumping_body(self) -> None:
        for status in (204, 301, 302, 307, 308, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                response = json_response({"error": TEST_KEY}, status)
                response.headers["Location"] = f"https://other.test/?key={TEST_KEY}"
                result = self.run_client(replies=(response,))
                self.assert_failure(result)
                self.assertEqual(len(result[2]), 1)
                self.assertIs(result[2][0].kwargs["allow_redirects"], False)
                self.assertNotIn("other.test", result[1])
                if status != 204:
                    self.assertIn(str(status), result[1])

    def test_connection_and_timeout_exceptions_fail_without_dumping_urls(self) -> None:
        for error in (requests.ConnectionError, requests.Timeout, requests.RequestException):
            with self.subTest(error=error):
                result = self.run_client(replies=(error(f"https://user:{TEST_KEY}@other.test"),))
                self.assert_failure(result)
                self.assertNotIn("other.test", result[1])

    def test_models_must_be_valid_nonempty_json(self) -> None:
        for body in ([], None, {}, {"data": []}, {"data": [None]}, {"data": [{"id": ""}]},
                     {"data": [{"id": 17}]}, {"data": [{"id": "good"}], "error": {"message": TEST_KEY}}):
            with self.subTest(body=body):
                result = self.run_client(replies=(json_response(body),))
                self.assert_failure(result)
                self.assertEqual(len(result[2]), 1)
        invalid = json_response({})
        invalid._content = f"<html>{TEST_KEY}</html>".encode()
        self.assert_failure(self.run_client(replies=(invalid,)))

    def test_chat_requires_nonempty_final_content_and_normal_completion(self) -> None:
        for content, finish in ((None, "stop"), ("", "stop"), (" \n", "stop"),
                                ([], "stop"), (4, "stop"), ("Paris", None),
                                ("Paris", "length"), ("Paris", "content_filter")):
            with self.subTest(content=content, finish=finish):
                result = self.run_client(("--no-stream",), (models("m"), chat(content, finish)))
                self.assert_failure(result)

    def test_chat_malformed_shapes_or_reasoning_only_fail(self) -> None:
        for body in (
            {}, {"choices": []}, {"choices": [None]}, {"choices": [{}]},
            {"choices": [{"message": "Paris", "finish_reason": "stop"}]},
            {"choices": [{"message": {"reasoning_content": "Paris"}, "finish_reason": "stop"}]},
            {"error": {"message": TEST_KEY}},
        ):
            with self.subTest(body=body):
                self.assert_failure(self.run_client(("--no-stream",), (models("m"), json_response(body))))

    def test_chat_http_or_json_failure_stops_further_checks(self) -> None:
        invalid = json_response({})
        invalid._content = b"not JSON"
        for response in (json_response({"error": TEST_KEY}, 500), invalid):
            with self.subTest(response=response):
                result = self.run_client(replies=(models("m"), response))
                self.assert_failure(result)
                self.assertEqual(len(result[2]), 2)

    def test_nonstandard_json_numbers_and_invalid_encoding_fail(self) -> None:
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                response = json_response({"data": [{"id": "m"}], "extra": number})
                self.assert_failure(self.run_client(("--no-stream",), (response, chat())))
                response = stream_response(sse(
                    {**chunk("Paris"), "extra": number}, chunk(finish="stop"), "[DONE]"
                ))
                self.assert_failure(self.run_client(replies=(models("m"), chat(), response)))
        response = json_response({})
        response._content = b'{"data": [{"id": "invalid-\xff"}]}'
        self.assert_failure(self.run_client(("--no-stream",), (response, chat())))

    def test_chat_and_stream_reject_wrong_choice_or_role(self) -> None:
        for index in (1, "0", False):
            with self.subTest(index=index):
                body = chat().json()
                body["choices"][0]["index"] = index
                self.assert_failure(self.run_client(("--no-stream",), (models("m"), json_response(body))))
                event = chunk("Paris")
                event["choices"][0]["index"] = index
                self.assert_failure(self.run_client(replies=(
                    models("m"), chat(), stream_response(sse(event, chunk(finish="stop"), "[DONE]"))
                )))
        self.assert_failure(self.run_client(replies=(
            models("m"), chat(), stream_response(sse(chunk("Paris", role="user"), chunk(finish="stop"), "[DONE]"))
        )))

    def test_stream_handles_multiline_utf8_comments_reasoning_and_usage(self) -> None:
        body = (
            b": heartbeat\r\n\r\n"
            + sse(chunk(reasoning_content="do not count this"))
            + b'event: message\r\nid: 1\r\nretry: 1000\r\ndata: {"choices":\r\ndata: [{"index": 0, "delta": {"content": "caf\xc3\xa9"}, "finish_reason": null}]}\r\n\r\n'
            + sse(chunk(finish="stop"), {"choices": [], "usage": {"completion_tokens": 4}}, "[DONE]")
        )
        code, output, _ = self.run_client(replies=(models("m"), chat(), stream_response(body)))
        self.assertEqual(code, 0, output)
        self.assertNotIn("do not count this", output)

    def test_stream_stops_reading_after_done(self) -> None:
        response = stream_response(sse(chunk("Paris"), chunk(finish="stop"), "[DONE]") + b"data: not JSON\n\n")
        code, output, _ = self.run_client(replies=(models("m"), chat(), response))
        self.assertEqual(code, 0, output)
        self.assertTrue(response.raw.closed)

    def test_stream_rejects_malformed_error_and_incomplete_events(self) -> None:
        cases = (
            b"", b"data: not JSON\n\n", b"data: \xff\n\n", b"garbage\n\n",
            b"event: error\ndata: {}\n\n", sse({"error": {"message": TEST_KEY}}),
            sse([]), sse({}), sse({"choices": []}), sse({"choices": [None]}),
            sse({"choices": [{"delta": []}]}), sse(chunk(["Paris"])),
            sse(chunk("Paris"), "[DONE]"), sse(chunk("Paris"), chunk(finish="stop")),
            sse(chunk("Paris"), chunk(finish="length"), "[DONE]"),
            sse(chunk(reasoning_content="Paris"), chunk(finish="stop"), "[DONE]"),
            sse(chunk(" \n"), chunk(finish="stop"), "[DONE]"),
            sse(chunk("Paris"), chunk(finish="stop"), chunk("extra"), "[DONE]"),
            sse(chunk("Paris"), chunk(finish="stop")) + b"data: [DONE]\n",
        )
        for body in cases:
            with self.subTest(body=body):
                response = stream_response(body)
                self.assert_failure(self.run_client(replies=(models("m"), chat(), response)))
                self.assertTrue(response.raw.closed)

    def test_stream_http_error_or_wrong_content_type_fails(self) -> None:
        for response in (json_response({"error": TEST_KEY}, 502), json_response({"answer": "Paris"})):
            with self.subTest(response=response):
                self.assert_failure(self.run_client(replies=(models("m"), chat(), response)))

    def test_midstream_network_failure_is_nonzero_and_closes_response(self) -> None:
        response = stream_response()
        with patch.object(response, "iter_content", side_effect=requests.ConnectionError(TEST_KEY)):
            self.assert_failure(self.run_client(replies=(models("m"), chat(), response)))
        self.assertTrue(response.raw.closed)

    def test_continuous_stream_has_a_deadline(self) -> None:
        response = stream_response(b": heartbeat\n\n" + sse(chunk("Paris"), chunk(finish="stop"), "[DONE]"))
        with patch("time.monotonic", side_effect=[0.0] + [121.0] * 30):
            self.assert_failure(self.run_client(replies=(models("m"), chat(), response)))
        self.assertTrue(response.raw.closed)

    def test_optional_responses_requires_completed_assistant_output(self) -> None:
        for body in (
            responses_body(""), responses_body(None), responses_body([]),
            responses_body(status="incomplete"), responses_body(status="failed"),
            {"status": "completed", "output": []},
            {"status": "completed", "output": [None]},
            {"status": "completed", "output": [{"type": "reasoning", "summary": ["Paris"]}]},
            {"status": "completed", "output": [{"type": "message", "role": "user", "content": [{"type": "output_text", "text": "Paris"}]}]},
            {**responses_body(), "error": {"message": TEST_KEY}},
        ):
            with self.subTest(body=body):
                self.assert_failure(self.run_client(
                    ("--no-stream", "--responses"), (models("m"), chat(), json_response(body))
                ))

    def test_optional_responses_backend_error_is_not_silently_skipped(self) -> None:
        result = self.run_client(
            ("--no-stream", "--responses"),
            (models("m"), chat(), json_response({"error": "unsupported"}, 404)),
        )
        self.assert_failure(result)
        self.assertIn("404", result[1])

    def test_optional_responses_rejects_incomplete_message(self) -> None:
        body = responses_body()
        body["output"][1]["status"] = "incomplete"
        self.assert_failure(self.run_client(
            ("--no-stream", "--responses"), (models("m"), chat(), json_response(body))
        ))

    def test_success_never_dumps_credential_bearing_body_or_model_id(self) -> None:
        code, output, _ = self.run_client(
            ("--no-stream", "--responses"),
            (models(TEST_KEY), chat(TEST_KEY), json_response(responses_body(TEST_KEY))),
        )
        self.assertEqual(code, 0, output)
        self.assertNotIn(TEST_KEY, output)

    def test_import_is_inert_even_with_non_reconfigurable_windows_stdout(self) -> None:
        with (
            patch("sys.platform", "win32"),
            contextlib.redirect_stdout(io.StringIO()),
            patch("builtins.input") as prompt,
            patch("requests.sessions.Session.request") as transport,
        ):
            namespace = runpy.run_path(str(CLIENT), run_name="imported_client")
        self.assertTrue(callable(namespace.get("main")))
        prompt.assert_not_called()
        transport.assert_not_called()

    def test_windows_main_supports_stdout_without_reconfigure(self) -> None:
        code, output, _ = self.run_client(
            ("--no-stream",), (models("m"), chat()), platform="win32"
        )
        self.assertEqual(code, 0, output)

    def test_help_is_safe_and_documents_the_contract(self) -> None:
        code, output, calls = self.run_client(("--help",), key=None)
        self.assertEqual(code, 0, output)
        for text in (API_URL, "PERMITLIFY_AI_API_KEY", "--timeout", "--responses", "--no-stream", "loopback", "HTTPS"):
            self.assertIn(text, output)
        self.assertNotIn(TEST_KEY, output)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
