import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import bot as bot_module
from bot import MoonVeilDeobfuscator, ProcessingError


class MoonVeilCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_mv_command_is_registered_and_help_states_static_mode(self):
        app = bot_module.create_bot(123)
        ctx = SimpleNamespace(sent=[])

        async def send(message, **_kwargs):
            ctx.sent.append(message)

        ctx.send = send
        try:
            self.assertIsNotNone(app.get_command("mv"))
            await app.get_command("help").callback(ctx)
            self.assertIn("MoonVeil V1.4.5", ctx.sent[0])
            self.assertIn("no ejecuta el script", ctx.sent[0])
            self.assertIn("servicios externos", ctx.sent[0])
        finally:
            await app.close()

    async def test_static_pipeline_does_not_execute_unrecognized_input(self):
        engine = MoonVeilDeobfuscator()
        marker = Path(tempfile.gettempdir()) / "moonveil-input-must-not-execute"
        marker.unlink(missing_ok=True)
        try:
            await engine.prepare()
            with tempfile.TemporaryDirectory(prefix="mv-static-test-") as folder:
                source = Path(folder) / "input.lua"
                source.write_text(
                    'os.execute("touch ' + str(marker) + '")\nreturn 1\n',
                    encoding="utf-8",
                )
                with self.assertRaises(ProcessingError) as caught:
                    await engine.run(source, timeout=20)
                self.assertIn("no encontró el payload Base64", str(caught.exception))
            self.assertFalse(marker.exists(), "MoonVeil mode must not execute the supplied Lua source")
        finally:
            await engine.close()


if __name__ == "__main__":
    unittest.main()
