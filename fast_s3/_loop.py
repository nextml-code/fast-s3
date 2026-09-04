"""Run an asyncio event loop in a background thread, driven from sync code."""

import asyncio
import threading
from typing import Any, Coroutine, Optional


class LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="fast-s3-loop", daemon=True
        )
        self._thread.start()
        self._started.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._started.set)
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self.loop.close()

    def run(
        self, coro: Coroutine[Any, Any, Any], timeout: Optional[float] = None
    ) -> Any:
        """Run ``coro`` on the loop and block until it finishes."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def submit(self, coro: Coroutine[Any, Any, Any]):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self._thread is None:
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self._thread = None
