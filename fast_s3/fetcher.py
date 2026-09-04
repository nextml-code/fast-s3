import asyncio
import logging
import os
import queue
import warnings
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Union

from ._client import AsyncS3Client
from ._loop import LoopThread
from .file import File, Status

logger = logging.getLogger("fast_s3")


class _Crash:
    def __init__(self, exception: BaseException):
        self.exception = exception


class Fetcher:
    """Download many objects concurrently and iterate over them as they arrive.

    All downloads run on a single asyncio event loop in a background thread,
    with up to ``concurrency`` HTTP requests in flight. At most ``buffer_size``
    files are downloaded ahead of the consumer (in flight or waiting to be
    yielded), in both ordered and unordered mode, which bounds memory use.

    In ordered mode a single slow object blocks everything behind it, and
    downloads stop once ``buffer_size`` files are waiting behind it. Some S3
    services occasionally serve an object in seconds rather than milliseconds,
    so ordered mode defaults to a much larger window (32 x concurrency, versus
    4 x concurrency unordered). Raise ``buffer_size`` further to trade memory
    for throughput, or use unordered mode when order does not matter.

    Slow requests are hedged (see ``AsyncS3Client``) so that a single straggler
    does not stall ordered iteration; set ``hedge=False`` to disable.

    ``callback`` is applied to the raw bytes of each file before it is
    yielded. It runs in ``callback_executor`` (a thread pool by default; pass a
    ``ProcessPoolExecutor`` for CPU heavy pure-Python callbacks).
    """

    def __init__(
        self,
        paths: List[Union[str, Path]],
        endpoint_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
        bucket_name: str,
        buffer_size: Optional[int] = None,
        concurrency: int = 256,
        n_retries: int = 3,
        backoff_factor: float = 0.5,
        verbose: bool = False,
        callback: Optional[Callable] = None,
        ordered: bool = False,
        callback_executor: Optional[Executor] = None,
        addressing_style: str = "auto",
        verify_ssl: bool = True,
        hedge: Union[bool, float] = True,
        n_workers: Optional[int] = None,
    ):
        if n_workers is not None:
            warnings.warn(
                "n_workers is deprecated, use concurrency",
                DeprecationWarning,
                stacklevel=2,
            )
            concurrency = n_workers
        self.paths = list(paths)
        self.endpoint_url = endpoint_url
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.region_name = region_name
        self.bucket_name = bucket_name
        self.concurrency = max(1, concurrency)
        if buffer_size is None:
            buffer_size = 32 * self.concurrency if ordered else 4 * self.concurrency
        self.buffer_size = max(1, buffer_size)
        if self.buffer_size < self.concurrency:
            warnings.warn(
                f"buffer_size={self.buffer_size} < concurrency={self.concurrency}: "
                "effective concurrency is limited by buffer_size",
                stacklevel=2,
            )
        self.n_retries = n_retries
        self.backoff_factor = backoff_factor
        self.callback = callback
        self.ordered = ordered
        self.addressing_style = addressing_style
        self.verify_ssl = verify_ssl
        self.hedge = hedge
        self._executor = callback_executor
        self._own_executor = callback_executor is None and callback is not None
        if verbose:
            _enable_verbose_logging()

    def __len__(self) -> int:
        return len(self.paths)

    def __iter__(self) -> Generator[File, None, None]:
        out: "queue.Queue" = queue.Queue()
        loop_thread = LoopThread()
        loop_thread.start()
        slots: Optional[asyncio.Semaphore] = None
        executor = self._executor
        if self._own_executor:
            executor = ThreadPoolExecutor(
                min(32, (os.cpu_count() or 4) + 4), thread_name_prefix="fast-s3-cb"
            )

        async def main() -> None:
            nonlocal slots
            slots = asyncio.Semaphore(self.buffer_size)
            limiter = asyncio.Semaphore(self.concurrency)
            loop = asyncio.get_running_loop()
            pending: Dict[int, File] = {}
            next_index = 0

            def deliver(index: int, file: File) -> None:
                nonlocal next_index
                if not self.ordered:
                    out.put(file)
                    return
                pending[index] = file
                while next_index in pending:
                    out.put(pending.pop(next_index))
                    next_index += 1

            async def fetch_one(
                client: AsyncS3Client, index: int, path: Union[str, Path]
            ) -> None:
                try:
                    async with limiter:
                        data = await client.get_object(str(path))
                    if self.callback is not None:
                        content = await loop.run_in_executor(
                            executor, self.callback, data
                        )
                    else:
                        content = data
                    file = File(content=content, path=path, status=Status.succeeded)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("Failed to download %s: %r", path, e)
                    file = File(
                        content=None, path=path, status=Status.failed, exception=e
                    )
                deliver(index, file)

            async with AsyncS3Client(
                endpoint_url=self.endpoint_url,
                access_key=self.aws_access_key_id,
                secret_key=self.aws_secret_access_key,
                region=self.region_name,
                bucket=self.bucket_name,
                max_connections=self.concurrency,
                n_retries=self.n_retries,
                backoff_factor=self.backoff_factor,
                addressing_style=self.addressing_style,
                verify_ssl=self.verify_ssl,
                hedge=self.hedge,
            ) as client:
                tasks = []
                for index, path in enumerate(self.paths):
                    await slots.acquire()
                    tasks.append(asyncio.create_task(fetch_one(client, index, path)))
                    tasks = [t for t in tasks if not t.done()]
                await asyncio.gather(*tasks)

        def runner_done(future) -> None:
            if not future.cancelled() and future.exception() is not None:
                out.put(_Crash(future.exception()))

        runner = loop_thread.submit(main())
        runner.add_done_callback(runner_done)
        try:
            for _ in range(len(self.paths)):
                item = out.get()
                if isinstance(item, _Crash):
                    raise item.exception
                loop_thread.loop.call_soon_threadsafe(slots.release)
                yield item
        finally:
            runner.cancel()
            loop_thread.stop()
            if self._own_executor and executor is not None:
                executor.shutdown(wait=False)

    def close(self) -> None:
        """Kept for backwards compatibility; resources are released when iteration ends."""


def _enable_verbose_logging() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
