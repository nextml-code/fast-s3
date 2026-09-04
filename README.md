# fast-s3

Download or upload very many small files to S3-compatible storage (AWS S3, Wasabi,
MinIO, ...) from Python at rclone-like speed.

Transfers of small objects are bound by round-trip latency, so throughput is
essentially `concurrent requests / latency`. fast-s3 keeps hundreds of requests
in flight from a single process by talking to S3 directly over
[aiohttp](https://docs.aiohttp.org) with its own Signature V4 signing, which
costs about 0.1 ms of CPU per object instead of the 2-4 ms that boto3 needs.

## Setup

```
uv sync
```

## Usage

Download files

```python
from PIL import Image
import io
from fast_s3 import Fetcher, Status

large_list_of_keys = [...]

fetcher = Fetcher(
    paths=large_list_of_keys,
    endpoint_url="https://s3.my-path-to-s3",
    aws_access_key_id="my-key-id",
    aws_secret_access_key="my-secret-key",
    region_name="my-region",
    bucket_name="my-bucket",
    ordered=True,       # yield files in the same order as paths
    concurrency=256,    # requests in flight
    buffer_size=8192,   # max files downloaded ahead of the consumer (bounds memory)
    callback=lambda data: Image.open(io.BytesIO(data)),  # optional, runs in a thread pool
)

for file in fetcher:
    if file.status == Status.succeeded:
        file.content.save(file.path)
    else:
        print(file.path, file.exception)
```

`callback` receives the raw bytes of each object. Pass
`callback_executor=concurrent.futures.ProcessPoolExecutor(...)` if it is CPU
heavy pure-Python code that does not release the GIL.

Upload files

```python
from fast_s3 import Uploader

with Uploader(
    endpoint_url="https://s3.my-path-to-s3",
    aws_access_key_id="my-key-id",
    aws_secret_access_key="my-secret-key",
    region_name="my-region",
    bucket_name="my-bucket",
    concurrency=256,
) as uploader:
    uploader.queue_upload(
        source=large_list_of_files,   # bytes, file paths, or file-like objects
        destination=large_list_of_keys,
    )
    results = uploader.await_upload()
```

Failed requests are retried with exponential backoff (`n_retries`,
`backoff_factor`) on connection errors and 429/5xx responses. Errors are
reported per file (`file.status == Status.failed`, `result.status == "error"`),
never raised from the iteration.

### Options

- `concurrency`: number of concurrent HTTP requests (default 256). Raise it for
  high-latency endpoints; throughput grows almost linearly until you saturate
  your network link or the CPU (a few thousand files per second).
- `buffer_size`: how many files may be downloaded ahead of the consumer, in
  flight or waiting. Defaults to 4 x concurrency, or 32 x concurrency when
  `ordered=True`. Ordered iteration stalls whenever one slow object has
  `buffer_size` finished files queued behind it, and some S3 services serve
  the occasional object in seconds instead of milliseconds, so a larger window
  buys throughput at the cost of memory (window x file size).
- `hedge`: duplicate a GET that takes 5x longer than average (`True`, the
  default), after a fixed number of seconds (a float), or never (`False`).
- `addressing_style`: `"auto"` (virtual-hosted when the bucket name allows it,
  like boto3), `"path"` or `"virtual"`. Use `"path"` for endpoints without
  wildcard DNS.
- `verify_ssl`: set to `False` for self-signed endpoints.
- `verbose`: log retries and failures to stderr.

`AsyncS3Client` (`get_object`, `put_object`, `head_object`, `delete_object`) is
also exported for use directly from asyncio code.

## Benchmarking

`benchmarks/compare_rclone.py` downloads the same keys with `Fetcher` and with
`rclone copy` and prints files per second for each:

```
uv run benchmarks/compare_rclone.py --keys-file keys.txt \
    --concurrency 64 256 512 --rclone-remote wasabi --rclone-transfers 64 256
```

`benchmarks/compare_rclone_upload.py` does the same for uploads into a bucket
you name explicitly, and can delete what it uploaded afterwards:

```
uv run benchmarks/compare_rclone_upload.py --bucket my-dev-bucket --n 5000 \
    --concurrency 64 256 --rclone-remote wasabi --rclone-transfers 64 256 --cleanup
```

Both read credentials from `STORAGE_ENDPOINT`, `STORAGE_ACCESS_KEY_ID`,
`STORAGE_SECRET_ACCESS_KEY`, `STORAGE_REGION_NAME` and `BUCKET_NAME`
(a `.env` file is loaded if present).

## Tests

```
uv run pytest
```

Integration tests need an S3 endpoint and are skipped unless
`FAST_S3_TEST_ENDPOINT`, `FAST_S3_TEST_KEY`, `FAST_S3_TEST_SECRET` and
`FAST_S3_TEST_BUCKET` are set (a local MinIO works well).
