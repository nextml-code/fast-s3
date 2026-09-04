"""Compare fast_s3.Uploader against rclone for uploading many small files.

Writes N random files to a temp dir, uploads them under a unique prefix with
fast_s3 and with ``rclone copy``, then (with --cleanup) deletes exactly the
objects it created. The target bucket must be given explicitly so that this is
never pointed at a production bucket by accident.

Credentials come from the same environment variables / .env as compare_rclone.py.

Example:
    uv run benchmarks/compare_rclone_upload.py --bucket my-dev-bucket --n 5000 \
        --concurrency 64 256 --rclone-remote wasabi --rclone-transfers 64 256 --cleanup
"""

import argparse
import asyncio
import os
import resource
import shutil
import subprocess
import tempfile
import time
import uuid

from fast_s3 import AsyncS3Client, Uploader


def cpu_seconds():
    a = resource.getrusage(resource.RUSAGE_SELF)
    return a.ru_utime + a.ru_stime


def make_files(directory, n, size):
    paths = []
    for i in range(n):
        path = os.path.join(directory, f"{i}.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        paths.append(path)
    return paths


def run_uploader(paths, keys, cfg, concurrency):
    t0, c0 = time.perf_counter(), cpu_seconds()
    with Uploader(**cfg, concurrency=concurrency) as uploader:
        uploader.queue_upload(paths, keys)
        results = uploader.await_upload()
    wall = time.perf_counter() - t0
    failed = [r.exception for r in results if r.status.value != "done"]
    n_bytes = sum(os.path.getsize(p) for p in paths)
    print(
        f"fast_s3 concurrency={concurrency:4d}: {wall:7.2f}s {len(paths) / wall:8.0f} files/s "
        f"{n_bytes / wall / 1e6:7.1f} MB/s cpu={cpu_seconds() - c0:.1f}s failed={len(failed)}"
        + (f" first error: {failed[0]!r}" if failed else "")
    )


def run_rclone(directory, remote, bucket, prefix, transfers):
    cmd = [
        "rclone",
        "copy",
        directory,
        f"{remote}:{bucket}/{prefix}",
        "--no-check-dest",
        "--s3-no-check-bucket",
        "--s3-no-head",
        "--transfers",
        str(transfers),
        "--checkers",
        str(transfers),
    ]
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True)
    wall = time.perf_counter() - t0
    n = len(os.listdir(directory))
    print(
        f"rclone  transfers={transfers:4d}  : {wall:7.2f}s {n / wall:8.0f} files/s (--s3-no-head)"
    )


async def delete_keys(cfg, keys, concurrency=256):
    sem = asyncio.Semaphore(concurrency)
    async with AsyncS3Client(
        cfg["endpoint_url"],
        cfg["aws_access_key_id"],
        cfg["aws_secret_access_key"],
        cfg["region_name"],
        cfg["bucket_name"],
        max_connections=concurrency,
    ) as client:

        async def one(key):
            async with sem:
                await client.delete_object(key)

        await asyncio.gather(*(one(k) for k in keys))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="bucket to upload into (NOT a production bucket)",
    )
    parser.add_argument("--n", type=int, default=5000)
    parser.add_argument("--size", type=int, default=27000, help="bytes per file")
    parser.add_argument("--prefix", default="fast-s3-benchmark")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[64, 256])
    parser.add_argument("--rclone-remote", default=None)
    parser.add_argument("--rclone-transfers", type=int, nargs="+", default=[64, 256])
    parser.add_argument(
        "--cleanup", action="store_true", help="delete the uploaded objects afterwards"
    )
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    if os.path.exists(args.env_file):
        from dotenv import load_dotenv

        load_dotenv(args.env_file)
    cfg = dict(
        endpoint_url=os.environ["STORAGE_ENDPOINT"],
        aws_access_key_id=os.environ["STORAGE_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["STORAGE_SECRET_ACCESS_KEY"],
        region_name=os.environ["STORAGE_REGION_NAME"],
        bucket_name=args.bucket,
    )
    run_id = uuid.uuid4().hex[:8]
    directory = tempfile.mkdtemp(prefix="fast-s3-upload-bench-")
    uploaded = []
    try:
        paths = make_files(directory, args.n, args.size)
        print(
            f"{args.n} files of {args.size} bytes -> {args.bucket}/{args.prefix}/{run_id}/"
        )
        for concurrency in args.concurrency:
            keys = [
                f"{args.prefix}/{run_id}/fast_s3-{concurrency}/{i}.bin"
                for i in range(args.n)
            ]
            run_uploader(paths, keys, cfg, concurrency)
            uploaded.extend(keys)
        if args.rclone_remote:
            for transfers in args.rclone_transfers:
                prefix = f"{args.prefix}/{run_id}/rclone-{transfers}"
                run_rclone(
                    directory, args.rclone_remote, args.bucket, prefix, transfers
                )
                uploaded.extend(f"{prefix}/{i}.bin" for i in range(args.n))
    finally:
        shutil.rmtree(directory)
        if args.cleanup and uploaded:
            t0 = time.perf_counter()
            asyncio.run(delete_keys(cfg, uploaded))
            print(f"deleted {len(uploaded)} objects in {time.perf_counter() - t0:.1f}s")
        elif uploaded:
            print(
                f"left {len(uploaded)} objects under {args.bucket}/{args.prefix}/{run_id}/ (use --cleanup)"
            )


if __name__ == "__main__":
    main()
