import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
from bot import create_bot


OWNER_ID = 123456789


class Ironbrew1CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = create_bot(OWNER_ID)

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_command_is_registered_and_documented(self):
        expected = {"help", "get", "deobf", "luraph", "ironbrew", "ironbrew1", "lb", "iv", "mv", "mvv2"}
        registered = {command.name for command in self.bot.commands}
        self.assertTrue(expected <= registered, expected - registered)
        ctx = SimpleNamespace(send=AsyncMock())
        await self.bot.get_command("help").callback(ctx)
        help_text = ctx.send.await_args.args[0]
        self.assertIn(".ironbrew1", help_text)
        self.assertIn("restringidos al propietario", help_text)

    async def test_non_owner_is_silently_rejected(self):
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=OWNER_ID + 1),
            message=SimpleNamespace(attachments=[]),
            send=AsyncMock(),
        )
        await self.bot.get_command("ironbrew1").callback(
            ctx, url="https://example.com/protected.lua"
        )
        ctx.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
