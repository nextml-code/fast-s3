import asyncio
import logging
import warnings
from concurrent.futures import Future
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Union

from pydantic import BaseModel

from ._client import AsyncS3Client
from ._loop import LoopThread

logger = logging.getLogger("fast_s3")

Source = Union[str, Path, bytes, bytearray, memoryview, Any]


class Status(str, Enum):
    done = "done"
    error = "error"


class Result(BaseModel, arbitrary_types_allowed=True):
    status: Status
    exception: Optional[Exception] = None


def _read_source(source: Source) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, str):
            data = data.encode()
        return data
    raise TypeError(f"unsupported source type {type(source).__name__}")


class Uploader:
    """Upload many small objects concurrently.

    Sources may be bytes, a file path (``str``/``Path``), or a file-like object
    with ``read()``. Files are read lazily, at most ``concurrency`` at a time.
    """

    def __init__(
        self,
        endpoint_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
        bucket_name: str,
        concurrency: int = 256,
        n_retries: int = 3,
        backoff_factor: float = 0.5,
        addressing_style: str = "auto",
        verify_ssl: bool = True,
        n_workers: Optional[int] = None,
    ):
        if n_workers is not None:
            warnings.warn(
                "n_workers is deprecated, use concurrency",
                DeprecationWarning,
                stacklevel=2,
            )
            concurrency = n_workers
        self.bucket_name = bucket_name
        self.concurrency = max(1, concurrency)
        self.futures: List[Future] = []
        self._loop_thread = LoopThread()
        self._loop_thread.start()
        self._client = AsyncS3Client(
            endpoint_url=endpoint_url,
            access_key=aws_access_key_id,
            secret_key=aws_secret_access_key,
            region=region_name,
            bucket=bucket_name,
            max_connections=self.concurrency,
            n_retries=n_retries,
            backoff_factor=backoff_factor,
            addressing_style=addressing_style,
            verify_ssl=verify_ssl,
        )
        self._limiter: asyncio.Semaphore = self._loop_thread.run(self._open())

    async def _open(self) -> asyncio.Semaphore:
        await self._client.__aenter__()
        return asyncio.Semaphore(self.concurrency)

    async def _upload_one(
        self, source: Source, key: str, content_type: Optional[str]
    ) -> None:
        async with self._limiter:
            if isinstance(source, (bytes, bytearray, memoryview)):
                data = bytes(source)
            else:
                data = await asyncio.get_running_loop().run_in_executor(
                    None, _read_source, source
                )
            await self._client.put_object(key, data, content_type=content_type)

    def queue_upload(
        self,
        source: List[Source],
        destination: List[Union[str, Path]],
        content_type: Optional[str] = None,
    ) -> List[Future]:
        if len(source) != len(destination):
            raise ValueError(
                "The number of source files and destination paths must be equal."
            )
        futures = [
            self._loop_thread.submit(self._upload_one(src, str(dst), content_type))
            for src, dst in zip(source, destination)
        ]
        self.futures.extend(futures)
        return futures

    def await_upload(self) -> List[Result]:
        results = []
        for future in self.futures:
            try:
                future.result()
                results.append(Result(status=Status.done))
            except Exception as e:
                logger.warning("Upload failed: %r", e)
                results.append(Result(status=Status.error, exception=e))
        self.futures = []
        return results

    def close(self) -> None:
        if self._loop_thread is None:
            return
        for future in self.futures:
            future.cancel()
        try:
            self._loop_thread.run(self._client.__aexit__(None, None, None), timeout=30)
        finally:
            self._loop_thread.stop()
            self._loop_thread = None  # type: ignore[assignment]

    def __enter__(self) -> "Uploader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
