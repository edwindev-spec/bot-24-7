import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
from bot import PublicOnlyResolver, validate_public_http_url


class FakeResolver:
    def __init__(self, addresses):
        self.addresses = addresses
        self.closed = False

    async def resolve(self, host, port, family):
        return [
            {"hostname": host, "host": address, "port": port,
             "family": family, "proto": 0, "flags": 0}
            for address in self.addresses
        ]

    async def close(self):
        self.closed = True


class PublicOnlyResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_public_ipv4(self):
        resolver = FakeResolver(["8.8.8.8"])
        checked = PublicOnlyResolver(resolver)
        records = await checked.resolve("example.test", 443, socket.AF_INET)
        self.assertEqual(records[0]["host"], "8.8.8.8")
        await checked.close()
        self.assertTrue(resolver.closed)

    async def test_rejects_loopback_dns_answer(self):
        checked = PublicOnlyResolver(FakeResolver(["127.0.0.1"]))
        with self.assertRaises(OSError):
            await checked.resolve("attacker.example", 80, socket.AF_INET)

    async def test_rejects_mixed_public_and_private_answers(self):
        # Reject the entire answer set; never let the connector choose a private sibling.
        checked = PublicOnlyResolver(FakeResolver(["8.8.8.8", "10.0.0.7"]))
        with self.assertRaises(OSError):
            await checked.resolve("rebinding.example", 443, socket.AF_INET)

    async def test_rejects_link_local_ipv6_dns_answer(self):
        checked = PublicOnlyResolver(FakeResolver(["fe80::1"]))
        with self.assertRaises(OSError):
            await checked.resolve("metadata.example", 80, socket.AF_INET6)

    async def test_rejects_nat64_address_translating_to_metadata_ip(self):
        # 64:ff9b::/96 encodes IPv4 in its low 32 bits; this one maps to 169.254.169.254.
        checked = PublicOnlyResolver(FakeResolver(["64:ff9b::a9fe:a9fe"]))
        with self.assertRaises(OSError):
            await checked.resolve("metadata.example", 80, socket.AF_INET6)


class PublicURLValidationTests(unittest.TestCase):
    def test_accepts_public_https(self):
        parsed = validate_public_http_url("https://example.com/file.lua")
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "example.com")

    def test_rejects_private_literal_ip(self):
        with self.assertRaises(ValueError):
            validate_public_http_url("http://192.168.1.2/file")

    def test_rejects_credentials_and_invalid_port(self):
        for url in ("https://user:pass@example.com/", "https://example.com:99999/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_public_http_url(url)

    def test_https_redirect_cannot_downgrade(self):
        with self.assertRaises(ValueError):
            validate_public_http_url("http://example.com/file", require_https=True)


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_started = False

    async def iter_chunked(self, _size):
        self.read_started = True
        for chunk in self.chunks:
            yield chunk

    async def read(self, size=-1):
        self.read_started = True
        data = b"".join(self.chunks)
        return data if size < 0 else data[:size]


class FakeResponse:
    def __init__(self, status=200, headers=None, length=3, chunks=(b"abc",)):
        self.status = status
        self.headers = headers or {}
        self.content_length = length
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requested = []
        self.session_kwargs = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, **_kwargs):
        self.requested.append(url)
        return self.responses.pop(0)


class GetHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_describes_api_json_support(self):
        from types import SimpleNamespace
        import bot as bot_module

        app = bot_module.create_bot(123)
        ctx = SimpleNamespace(sent=[])

        async def send(message, **_kwargs):
            ctx.sent.append(message)

        ctx.send = send
        await app.get_command("help").callback(ctx)
        self.assertIn("APIs HTTP(S) públicas", ctx.sent[0])
        self.assertIn("raw.json", ctx.sent[0])
        await app.close()

    async def run_get(self, url, responses):
        from unittest.mock import patch
        from types import SimpleNamespace
        import bot as bot_module

        session = FakeSession(responses)
        app = bot_module.create_bot(123)
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=123),
            guild=SimpleNamespace(filesize_limit=10 * 1024 * 1024),
            sent=[],
        )

        async def send(message, **kwargs):
            file = kwargs.get("file")
            if file is not None:
                position = file.fp.tell()
                kwargs["_file_data"] = file.fp.read()
                file.fp.seek(position)
            ctx.sent.append((message, kwargs))

        def make_session(*_args, **kwargs):
            session.session_kwargs = kwargs
            return session

        ctx.send = send
        with patch.object(bot_module, "PublicOnlyResolver", return_value=object()), \
             patch.object(bot_module.aiohttp, "TCPConnector", return_value=object()), \
             patch.object(bot_module.aiohttp, "ClientSession", side_effect=make_session):
            await app.get_command("get").callback(ctx, url)
        await app.close()
        return ctx, session

    async def test_success_returns_raw_txt(self):
        ctx, session = await self.run_get(
            "https://example.com/script.lua", [FakeResponse(length=3, chunks=(b"abc",))]
        )
        self.assertEqual(session.requested, ["https://example.com/script.lua"])
        message, kwargs = ctx.sent[-1]
        self.assertIn("3 bytes", message)
        self.assertEqual(kwargs["file"].filename, "raw.txt")

    async def test_json_api_is_pretty_printed_and_previewed(self):
        body = b'{"ok":true,"items":[1,2]}'
        ctx, session = await self.run_get(
            "https://api.example.com/data",
            [FakeResponse(length=len(body), chunks=(body,))],
        )
        message, kwargs = ctx.sent[-1]
        self.assertEqual(kwargs["file"].filename, "raw.json")
        self.assertEqual(kwargs["_file_data"], b'{\n  "ok": true,\n  "items": [\n    1,\n    2\n  ]\n}\n')
        self.assertIn("```json", message)
        self.assertIn('"items": [', message)
        self.assertTrue(session.session_kwargs["headers"]["Accept"].startswith("application/json"))
        self.assertFalse(session.session_kwargs["trust_env"])

    async def test_redirect_to_private_literal_is_blocked_before_request(self):
        ctx, session = await self.run_get(
            "https://example.com/start",
            [FakeResponse(status=302, headers={"Location": "http://127.0.0.1/admin"}, length=0, chunks=())],
        )
        self.assertEqual(session.requested, ["https://example.com/start"])
        self.assertIn("no permitida", ctx.sent[-1][0])

    async def test_https_upgrade_cannot_be_followed_by_downgrade(self):
        ctx, session = await self.run_get(
            "http://example.com/start",
            [
                FakeResponse(status=302, headers={"Location": "https://example.com/secure"}, length=0, chunks=()),
                FakeResponse(status=302, headers={"Location": "http://example.com/downgrade"}, length=0, chunks=()),
            ],
        )
        self.assertEqual(session.requested, ["http://example.com/start", "https://example.com/secure"])
        self.assertIn("no permitida", ctx.sent[-1][0])

    async def test_oversized_content_length_is_rejected_without_reading_body(self):
        from bot import MAX_INPUT
        response = FakeResponse(length=MAX_INPUT + 1, chunks=(b"must-not-be-read",))
        ctx, _session = await self.run_get("https://example.com/large", [response])
        self.assertFalse(response.content.read_started)
        self.assertIn("supera el límite", ctx.sent[-1][0])

    async def test_403_explains_that_api_owner_must_allow_access(self):
        response = FakeResponse(status=403, headers={"Content-Type": "text/html"}, length=7, chunks=(b"Blocked",))
        ctx, _session = await self.run_get("https://api.example.com/private", [response])
        self.assertIn("HTTP 403", ctx.sent[-1][0])
        self.assertIn("no puede saltarse ese control", ctx.sent[-1][0])

    async def test_401_explains_public_get_has_no_auth_token(self):
        response = FakeResponse(status=401, headers={"Content-Type": "application/json"}, length=24,
                                chunks=(b'{"message":"Need token"}',))
        ctx, _session = await self.run_get("https://api.example.com/private", [response])
        self.assertIn("requiere autenticación", ctx.sent[-1][0])
        self.assertIn("no añade tokens", ctx.sent[-1][0])

    async def test_429_reports_retry_after(self):
        response = FakeResponse(status=429, headers={"Retry-After": "30"}, length=0, chunks=())
        ctx, _session = await self.run_get("https://api.example.com/limited", [response])
        self.assertIn("HTTP 429", ctx.sent[-1][0])
        self.assertIn("30", ctx.sent[-1][0])

    async def test_204_reports_no_content(self):
        response = FakeResponse(status=204, length=0, chunks=())
        ctx, _session = await self.run_get("https://api.example.com/empty", [response])
        self.assertIn("204 No Content", ctx.sent[-1][0])

    async def test_json_error_detail_is_markdown_escaped(self):
        body = b'{"detail":"bad `input`"}'
        response = FakeResponse(status=400, headers={"Content-Type": "application/problem+json"},
                                length=len(body), chunks=(body,))
        ctx, _session = await self.run_get("https://api.example.com/bad-request", [response])
        self.assertIn("Detalle de la API", ctx.sent[-1][0])
        self.assertIn("bad", ctx.sent[-1][0])
        self.assertIn("\\`", ctx.sent[-1][0])


if __name__ == "__main__":
    unittest.main()
