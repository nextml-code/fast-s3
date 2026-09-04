"""Retry behaviour, tested against a local aiohttp server (no S3 needed)."""

import asyncio

import pytest
from aiohttp import web

from fast_s3 import AsyncS3Client, S3Error
from fast_s3._loop import LoopThread

ERROR_XML = "<Error><Code>{code}</Code><Message>{message}</Message></Error>"


class FakeS3:
    """Serves /bucket/<key>; behaviour per key is scripted as a list of statuses."""

    def __init__(self):
        self.script = {}
        self.delays = {}
        self.calls = {}
        self.loop_thread = LoopThread()

    async def handle(self, request):
        key = request.match_info["key"]
        self.calls[key] = self.calls.get(key, 0) + 1
        delays = self.delays.get(key)
        if delays:
            await asyncio.sleep(delays[min(self.calls[key], len(delays)) - 1])
        statuses = self.script.get(key, [200])
        status = statuses[min(self.calls[key], len(statuses)) - 1]
        if status == 200:
            return web.Response(body=b"payload:" + key.encode())
        return web.Response(
            status=status, text=ERROR_XML.format(code=f"E{status}", message="scripted")
        )

    async def _start(self):
        app = web.Application()
        app.router.add_route("*", "/bucket/{key:.*}", self.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runner = runner
        return runner.addresses[0][1]

    def start(self):
        self.loop_thread.start()
        self.port = self.loop_thread.run(self._start())
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.loop_thread.run(self.runner.cleanup())
        self.loop_thread.stop()


@pytest.fixture
def fake_s3():
    server = FakeS3()
    server.url = server.start()
    yield server
    server.stop()


def make_client(url, **kwargs):
    return AsyncS3Client(
        url, "k", "s", "us-east-1", "bucket", backoff_factor=0.01, **kwargs
    )


def run(coro):
    return asyncio.run(coro)


def test_retries_then_succeeds(fake_s3):
    fake_s3.script["a"] = [503, 500, 200]

    async def go():
        async with make_client(fake_s3.url, n_retries=3) as client:
            return await client.get_object("a")

    assert run(go()) == b"payload:a"
    assert fake_s3.calls["a"] == 3


def test_gives_up_after_n_retries(fake_s3):
    fake_s3.script["a"] = [503] * 10

    async def go():
        async with make_client(fake_s3.url, n_retries=2) as client:
            return await client.get_object("a")

    with pytest.raises(S3Error) as info:
        run(go())
    assert info.value.status == 503
    assert fake_s3.calls["a"] == 3


def test_404_is_not_retried(fake_s3):
    fake_s3.script["missing"] = [404]

    async def go():
        async with make_client(fake_s3.url, n_retries=3) as client:
            return await client.get_object("missing")

    with pytest.raises(S3Error) as info:
        run(go())
    assert info.value.status == 404
    assert info.value.code == "E404"
    assert fake_s3.calls["missing"] == 1


def test_connection_error_is_retried_and_raised():
    async def go():
        async with make_client("http://127.0.0.1:1", n_retries=1) as client:
            return await client.get_object("a")

    with pytest.raises(Exception) as info:
        run(go())
    assert not isinstance(info.value, S3Error)


def test_addressing_style_selection():
    assert make_client("http://127.0.0.1:9000").addressing_style == "path"
    assert make_client("https://s3.example.com").addressing_style == "virtual"
    assert (
        AsyncS3Client(
            "https://s3.example.com", "k", "s", "r", "My.Bucket"
        ).addressing_style
        == "path"
    )
    virtual = AsyncS3Client("https://s3.example.com", "k", "s", "r", "bucket")
    assert virtual._host == "bucket.s3.example.com" and virtual._path("x/y") == "/x/y"
    path = make_client("https://s3.example.com", addressing_style="path")
    assert path._path("x y") == "/bucket/x%20y"


def test_hedged_request_wins_over_straggler(fake_s3):
    fake_s3.delays["slow"] = [
        3.0,
        0.0,
    ]  # first call hangs 3s, second answers immediately

    async def go():
        async with make_client(fake_s3.url, hedge=0.2) as client:
            t = asyncio.get_running_loop().time()
            body = await client.get_object("slow")
            return body, asyncio.get_running_loop().time() - t, client.n_hedged

    body, elapsed, n_hedged = run(go())
    assert body == b"payload:slow"
    assert elapsed < 1.5
    assert n_hedged == 1
    assert fake_s3.calls["slow"] == 2


def test_hedge_disabled_waits(fake_s3):
    fake_s3.delays["slow"] = [0.5, 0.0]

    async def go():
        async with make_client(fake_s3.url, hedge=False) as client:
            return await client.get_object("slow")

    assert run(go()) == b"payload:slow"
    assert fake_s3.calls["slow"] == 1
