"""Small asynchronous S3 client built directly on aiohttp.

It deliberately implements only what fast_s3 needs (GET/PUT/HEAD/DELETE of a
single object) so that the Python overhead per request stays around 0.1 ms,
which lets a single process keep hundreds of requests in flight.
"""

import asyncio
import hashlib
import ipaddress
import logging
import random
import re
import time
from typing import Dict, Optional, Tuple, Union
from urllib.parse import urlparse

import aiohttp

from ._signer import EMPTY_PAYLOAD_SHA256, SigV4Signer, uri_encode_path

logger = logging.getLogger("fast_s3")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_DNS_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class S3Error(Exception):
    """Raised when S3 answers with an error status."""

    def __init__(self, status: int, code: str, message: str, key: str):
        super().__init__(f"{status} {code}: {message} (key={key!r})")
        self.status = status
        self.code = code
        self.message = message
        self.key = key


def _parse_error(body: bytes) -> Tuple[str, str]:
    code = re.search(rb"<Code>(.*?)</Code>", body, re.S)
    message = re.search(rb"<Message>(.*?)</Message>", body, re.S)
    return (
        code.group(1).decode(errors="replace") if code else "Unknown",
        (
            message.group(1).decode(errors="replace")
            if message
            else body[:200].decode(errors="replace")
        ),
    )


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class AsyncS3Client:
    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
        bucket: str,
        max_connections: int = 256,
        n_retries: int = 3,
        backoff_factor: float = 0.5,
        addressing_style: str = "auto",
        verify_ssl: bool = True,
        connect_timeout: float = 30.0,
        read_timeout: float = 60.0,
        hedge: Union[bool, float] = True,
    ):
        """
        ``hedge`` controls hedged (duplicate) GET/HEAD requests, which cut the
        latency tail that otherwise stalls ordered iteration: ``True`` hedges a
        request once it has taken 5x the running average (at least 0.5 s), a
        float hedges after that many seconds, ``False`` disables hedging.
        """
        parsed = urlparse(endpoint_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"endpoint_url must be http(s)://host[:port], got {endpoint_url!r}"
            )
        if addressing_style not in ("auto", "path", "virtual"):
            raise ValueError("addressing_style must be 'auto', 'path' or 'virtual'")
        if addressing_style == "auto":
            dns_compatible = bool(_DNS_BUCKET.match(bucket))
            addressing_style = (
                "virtual"
                if dns_compatible
                and not _is_ip(parsed.hostname or "")
                and parsed.hostname != "localhost"
                else "path"
            )
        if addressing_style == "virtual":
            self._host = f"{bucket}.{parsed.netloc}"
            self._base_url = f"{parsed.scheme}://{self._host}"
            self._path_prefix = ""
        else:
            self._host = parsed.netloc
            self._base_url = f"{parsed.scheme}://{self._host}"
            self._path_prefix = "/" + uri_encode_path(bucket)
        self.addressing_style = addressing_style
        self.bucket = bucket
        self.n_retries = n_retries
        self.backoff_factor = backoff_factor
        self.max_connections = max_connections
        self.verify_ssl = verify_ssl
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._signer = SigV4Signer(access_key, secret_key, region)
        self._session: Optional[aiohttp.ClientSession] = None
        self.hedge = hedge
        self._latency_avg: Optional[float] = None
        self.n_hedged = 0

    async def __aenter__(self) -> "AsyncS3Client":
        connector = aiohttp.TCPConnector(
            limit=self.max_connections,
            ttl_dns_cache=300,
            ssl=True if self.verify_ssl else False,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(
                total=None,
                sock_connect=self.connect_timeout,
                sock_read=self.read_timeout,
            ),
            auto_decompress=False,
            skip_auto_headers=("Accept-Encoding",),
            headers={"User-Agent": "fast-s3"},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _path(self, key: str) -> str:
        return self._path_prefix + "/" + uri_encode_path(key)

    async def _attempt(
        self,
        method: str,
        path: str,
        key: str,
        data: Optional[bytes],
        headers: Optional[Dict[str, str]],
        payload_hash: str,
        expected: Tuple[int, ...],
    ) -> Tuple[int, Dict[str, str], bytes]:
        """One HTTP attempt. Raises S3Error for error statuses, aiohttp errors on transport failure."""
        assert (
            self._session is not None
        ), "client must be used as an async context manager"
        signed = self._signer.sign(method, self._host, path, headers, payload_hash)
        started = time.monotonic()
        async with self._session.request(
            method, self._base_url + path, headers=signed, data=data
        ) as response:
            body = await response.read()
            if response.status in expected:
                elapsed = time.monotonic() - started
                self._latency_avg = (
                    elapsed
                    if self._latency_avg is None
                    else 0.95 * self._latency_avg + 0.05 * elapsed
                )
                return response.status, dict(response.headers), body
            code, message = _parse_error(body)
            raise S3Error(response.status, code, message, key)

    def _hedge_after(self) -> Optional[float]:
        if self.hedge is False or self.hedge is None:
            return None
        if self.hedge is True:
            return (
                max(0.5, 5 * self._latency_avg)
                if self._latency_avg is not None
                else None
            )
        return float(self.hedge)

    async def _hedged_attempt(self, *args) -> Tuple[int, Dict[str, str], bytes]:
        """Run one attempt; if it is slow, race a duplicate and take the first success."""
        first = asyncio.ensure_future(self._attempt(*args))
        done, _ = await asyncio.wait({first}, timeout=self._hedge_after())
        if done:
            return first.result()
        self.n_hedged += 1
        second = asyncio.ensure_future(self._attempt(*args))
        pending = {first, second}
        error: Optional[BaseException] = None
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if task.exception() is None:
                        return task.result()
                    error = task.exception()
            assert error is not None
            raise error
        finally:
            for task in pending:
                task.cancel()

    async def _request(
        self,
        method: str,
        key: str,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        expected: Tuple[int, ...] = (200,),
    ) -> Tuple[int, Dict[str, str], bytes]:
        path = self._path(key)
        payload_hash = (
            hashlib.sha256(data).hexdigest()
            if data is not None
            else EMPTY_PAYLOAD_SHA256
        )
        args = (method, path, key, data, headers, payload_hash, expected)
        hedging = (
            method in ("GET", "HEAD")
            and self.hedge is not False
            and self.hedge is not None
        )
        run = self._hedged_attempt if hedging else self._attempt
        attempts = 1 + self.n_retries
        for attempt in range(attempts):
            try:
                return await run(*args)
            except S3Error as e:
                error: Exception = e
                if e.status not in RETRYABLE_STATUS:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                error = e
            if attempt + 1 == attempts:
                raise error
            wait = self.backoff_factor * (2**attempt) + random.uniform(0, 1)
            logger.info("Retrying %s %s in %.2fs due to: %r", method, key, wait, error)
            await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    async def get_object(self, key: str) -> bytes:
        _, _, body = await self._request("GET", key)
        return body

    async def head_object(self, key: str) -> Dict[str, str]:
        _, headers, _ = await self._request("HEAD", key)
        return headers

    async def put_object(
        self, key: str, data: bytes, content_type: Optional[str] = None
    ) -> Dict[str, str]:
        headers = {"content-type": content_type} if content_type else None
        _, response_headers, _ = await self._request(
            "PUT", key, data=bytes(data), headers=headers
        )
        return response_headers

    async def delete_object(self, key: str) -> None:
        await self._request("DELETE", key, expected=(204, 200))
