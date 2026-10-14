import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import bot as bot_module
from bot import redact_http_headers_for_log, redact_url_for_log


class HttpAuditRedactionTests(unittest.TestCase):
    def test_url_log_redacts_userinfo_query_values_and_token_path(self):
        safe = redact_url_for_log(
            "https://user:password@example.com/api/token/very-long-secret-value-1234567890"
            "?access_token=query-secret&limit=10#fragment"
        )
        self.assertIn("https://example.com/api/token/[REDACTED]", safe)
        self.assertIn("limit=%5BREDACTED%5D", safe)
        for secret in ("user", "password", "very-long-secret-value", "query-secret", "fragment"):
            self.assertNotIn(secret, safe)

    def test_header_log_redacts_sensitive_values_and_omits_unknown_values(self):
        safe = redact_http_headers_for_log({
            "Content-Type": "application/json",
            "X-API-Key": "api-key-secret",
            "Set-Cookie": "session=session-secret",
            "X-Custom-Private": "custom-secret",
        })
        rendered = str(safe)
        self.assertIn("application/json", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertIn("[OMITTED]", rendered)
        for secret in ("api-key-secret", "session-secret", "custom-secret"):
            self.assertNotIn(secret, rendered)


class FakeContent:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, _size):
        yield self.body


class FakeResponse:
    def __init__(self, body):
        self.status = 200
        self.headers = {
            "Content-Type": "application/json",
            "X-Request-ID": "request-123",
            "X-API-Key": "api-key-secret",
            "Set-Cookie": "session=session-secret",
            "X-Custom-Private": "custom-secret",
        }
        self.content_length = len(body)
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, _url, **_kwargs):
        return self.response


class GetAuditLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_method_status_headers_and_timing_without_secrets_or_body(self):
        body = b'{"message":"private-response-body"}'
        response = FakeResponse(body)
        session = FakeSession(response)
        app = bot_module.create_bot(123)
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=123),
            guild=SimpleNamespace(filesize_limit=10 * 1024 * 1024),
            sent=[],
        )

        async def send(message, **_kwargs):
            ctx.sent.append(message)

        ctx.send = send

        def make_session(*_args, **_kwargs):
            return session

        try:
            with patch.object(bot_module, "PublicOnlyResolver", return_value=object()), \
                 patch.object(bot_module.aiohttp, "TCPConnector", return_value=object()), \
                 patch.object(bot_module.aiohttp, "ClientSession", side_effect=make_session), \
                 self.assertLogs("bot.http", level="INFO") as captured:
                await app.get_command("get").callback(
                    ctx, "https://api.example.com/data?access_token=query-secret&format=json"
                )
            rendered = "\n".join(captured.output)
            self.assertIn("get_request method=GET", rendered)
            self.assertIn("get_response status=200", rendered)
            self.assertIn("get_body_complete status=200", rendered)
            self.assertIn("request-123", rendered)
            self.assertIn("[REDACTED]", rendered)
            self.assertIn("[OMITTED]", rendered)
            for secret in ("query-secret", "api-key-secret", "session-secret", "custom-secret", "private-response-body"):
                self.assertNotIn(secret, rendered)
        finally:
            await app.close()


if __name__ == "__main__":
    unittest.main()
