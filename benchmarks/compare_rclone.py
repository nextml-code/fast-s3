"""Compare fast_s3.Fetcher against rclone on the same list of keys.

Credentials are read from the same environment variables as the notebooks
(STORAGE_ENDPOINT, STORAGE_ACCESS_KEY_ID, STORAGE_SECRET_ACCESS_KEY,
STORAGE_REGION_NAME, BUCKET_NAME), optionally from a .env file.

Example:
    uv run benchmarks/compare_rclone.py --keys-file keys.txt --concurrency 64 256 \
        --rclone-remote wasabi --rclone-transfers 64 256
"""

import argparse
import os
import resource
import shutil
import subprocess
import tempfile
import time

from fast_s3 import Fetcher, Status


def cpu_seconds():
    a = resource.getrusage(resource.RUSAGE_SELF)
    return a.ru_utime + a.ru_stime


def run_fetcher(keys, cfg, concurrency, ordered, buffer_size=None):
    buffer_size = buffer_size or max(1024, 4 * concurrency)
    fetcher = Fetcher(
        paths=keys,
        **cfg,
        concurrency=concurrency,
        buffer_size=buffer_size,
        ordered=ordered,
    )
    t0, c0 = time.perf_counter(), cpu_seconds()
    n_ok = n_bytes = 0
    for file in fetcher:
        if file.status == Status.succeeded:
            n_ok += 1
            n_bytes += len(file.content)
    wall = time.perf_counter() - t0
    print(
        f"fast_s3 concurrency={concurrency:4d} buffer={buffer_size:5d} ordered={ordered!s:5s}: {wall:7.2f}s "
        f"{n_ok / wall:8.0f} files/s {n_bytes / wall / 1e6:7.1f} MB/s "
        f"cpu={cpu_seconds() - c0:.1f}s failed={len(keys) - n_ok}"
    )


def run_rclone(keys, remote, bucket, transfers):
    out = tempfile.mkdtemp(prefix="fast-s3-rclone-")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("\n".join(keys))
        files_from = f.name
    cmd = [
        "rclone",
        "copy",
        f"{remote}:{bucket}",
        out,
        "--files-from",
        files_from,
        "--no-traverse",
        "--no-check-dest",
        "--s3-no-check-bucket",
        "--transfers",
        str(transfers),
        "--checkers",
        str(transfers),
    ]
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True)
    wall = time.perf_counter() - t0
    n = sum(len(files) for _, _, files in os.walk(out))
    print(
        f"rclone  transfers={transfers:4d}                : {wall:7.2f}s "
        f"{n / wall:8.0f} files/s (includes writing to disk)"
    )
    shutil.rmtree(out)
    os.unlink(files_from)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--keys-file", required=True, help="text file with one object key per line"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="only use the first N keys"
    )
    parser.add_argument("--concurrency", type=int, nargs="+", default=[64, 256])
    parser.add_argument("--ordered", action="store_true")
    parser.add_argument(
        "--buffer-size", type=int, default=None, help="default max(1024, 4*concurrency)"
    )
    parser.add_argument("--rclone-remote", default=None)
    parser.add_argument("--rclone-transfers", type=int, nargs="+", default=[64, 256])
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
        bucket_name=os.environ["BUCKET_NAME"],
    )
    keys = [line.strip() for line in open(args.keys_file) if line.strip()][: args.limit]
    print(f"{len(keys)} keys from bucket {cfg['bucket_name']}")

    for concurrency in args.concurrency:
        run_fetcher(keys, cfg, concurrency, args.ordered, args.buffer_size)
    if args.rclone_remote:
        for transfers in args.rclone_transfers:
            run_rclone(keys, args.rclone_remote, cfg["bucket_name"], transfers)


if __name__ == "__main__":
    main()
