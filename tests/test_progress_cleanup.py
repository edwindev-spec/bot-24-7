import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import bot as bot_module


class FakeMessage:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class ProgressCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_progress_message_after_task_sent_it(self):
        message = FakeMessage()

        async def sent_message():
            return message

        task = asyncio.create_task(sent_message())
        await task
        await bot_module.clear_progress(task)
        self.assertTrue(message.deleted)

    async def test_cancels_pending_progress_without_deleting_anything(self):
        async def wait_forever():
            await asyncio.sleep(3600)

        task = asyncio.create_task(wait_forever())
        await bot_module.clear_progress(task)
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
