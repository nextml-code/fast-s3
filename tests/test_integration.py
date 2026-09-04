"""End-to-end tests against a real S3-compatible endpoint (see conftest.py)."""

import io
import time
from concurrent.futures import ProcessPoolExecutor

import pytest

from fast_s3 import Fetcher, S3Error, Status, Uploader


def _upper(data: bytes) -> bytes:
    return data.upper()


@pytest.fixture
def uploaded(s3_config, prefix):
    keys = [f"{prefix}/{i}.bin" for i in range(300)]
    payloads = [f"content-{i}".encode() * 100 for i in range(300)]
    with Uploader(**s3_config, concurrency=64) as uploader:
        uploader.queue_upload(payloads, keys)
        results = uploader.await_upload()
    assert all(r.status.value == "done" for r in results), [
        r.exception for r in results if r.exception
    ]
    return keys, payloads


def test_fetch_unordered(s3_config, uploaded):
    keys, payloads = uploaded
    fetcher = Fetcher(paths=keys, **s3_config, concurrency=64, buffer_size=128)
    assert len(fetcher) == len(keys)
    got = {file.path: file.content for file in fetcher}
    assert got == dict(zip(keys, payloads))


def test_fetch_ordered_small_buffer(s3_config, uploaded):
    keys, payloads = uploaded
    fetcher = Fetcher(
        paths=keys, **s3_config, concurrency=8, buffer_size=8, ordered=True
    )
    files = list(fetcher)
    assert [f.path for f in files] == keys
    assert [f.content for f in files] == payloads
    assert all(f.status == Status.succeeded for f in files)


def test_missing_key_is_reported_not_raised(s3_config, uploaded):
    keys, payloads = uploaded
    paths = [keys[0], f"{keys[0]}.does-not-exist", keys[1]]
    files = list(Fetcher(paths=paths, **s3_config, ordered=True))
    assert [f.status for f in files] == [
        Status.succeeded,
        Status.failed,
        Status.succeeded,
    ]
    assert isinstance(files[1].exception, S3Error) and files[1].exception.status == 404
    assert files[1].content is None


def test_callback_thread_and_process(s3_config, uploaded):
    keys, payloads = uploaded
    files = list(Fetcher(paths=keys[:20], **s3_config, callback=_upper, ordered=True))
    assert [f.content for f in files] == [p.upper() for p in payloads[:20]]
    with ProcessPoolExecutor(2) as pool:
        files = list(
            Fetcher(
                paths=keys[:20],
                **s3_config,
                callback=_upper,
                ordered=True,
                callback_executor=pool,
            )
        )
    assert [f.content for f in files] == [p.upper() for p in payloads[:20]]


def test_early_break_does_not_hang(s3_config, uploaded):
    keys, _ = uploaded
    fetcher = Fetcher(paths=keys, **s3_config, concurrency=4, buffer_size=4)
    t = time.time()
    for file in fetcher:
        break
    assert time.time() - t < 10


def test_upload_from_path_and_fileobj(s3_config, prefix, tmp_path):
    p = tmp_path / "file.txt"
    p.write_bytes(b"from-path")
    with Uploader(**s3_config) as uploader:
        uploader.queue_upload(
            [str(p), io.BytesIO(b"from-fileobj"), b"from-bytes"],
            [f"{prefix}/a", f"{prefix}/b", f"{prefix}/c"],
            content_type="text/plain",
        )
        assert [r.status.value for r in uploader.await_upload()] == ["done"] * 3
    files = list(
        Fetcher(
            paths=[f"{prefix}/a", f"{prefix}/b", f"{prefix}/c"],
            **s3_config,
            ordered=True,
        )
    )
    assert [f.content for f in files] == [b"from-path", b"from-fileobj", b"from-bytes"]


def test_upload_error_is_reported(s3_config, prefix):
    with Uploader(**s3_config) as uploader:
        uploader.queue_upload([b"ok", object()], [f"{prefix}/ok", f"{prefix}/bad"])
        results = uploader.await_upload()
    assert [r.status.value for r in results] == ["done", "error"]
    assert isinstance(results[1].exception, TypeError)
