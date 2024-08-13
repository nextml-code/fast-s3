import multiprocessing
import random
import time
import warnings
from pathlib import Path
from queue import Empty
from typing import Callable, Generator, List, Tuple, Union

import boto3
import botocore.exceptions

from .file import File, Status


class Fetcher:
    def __init__(
        self,
        paths: List[Union[str, Path]],
        endpoint_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
        bucket_name: str,
        buffer_size: int = 1024,
        n_workers: int = 32,
        worker_batch_size: int = 128,
        n_retries: int = 3,
        backoff_factor: float = 0.5,
        verbose: bool = False,
        callback: Callable = lambda x: x,
        ordered: bool = False,
    ):
        self.paths = multiprocessing.Manager().list(list(enumerate(paths))[::-1])
        self.bucket_name = bucket_name
        self.endpoint_url = endpoint_url
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.region_name = region_name
        self.n_workers = n_workers
        self.buffer_size = min(buffer_size, len(paths))
        self.worker_batch_size = worker_batch_size
        self.n_retries = n_retries
        self.backoff_factor = backoff_factor
        self.verbose = verbose
        self.ordered = ordered
        self.callback = callback

        if ordered:
            # TODO: fix this issue
            warnings.warn(
                "buffer_size is ignored when ordered=True which can cause out of memory"
            )
            self.results = multiprocessing.Manager().dict()
            self.result_order = multiprocessing.Manager().list(range(len(paths)))
        else:
            self.file_queue = multiprocessing.Queue(maxsize=buffer_size)

    def _create_s3_client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.region_name,
        )

    def download_batch(self, batch: List[Tuple[int, Union[Path, str]]]):
        client = self._create_s3_client()
        for index, path in batch:
            for attempt in range(self.n_retries):
                try:
                    file = File(
                        content=self.callback(
                            client.get_object(Bucket=self.bucket_name, Key=str(path))[
                                "Body"
                            ].read()
                        ),
                        path=path,
                        status=Status.succeeded,
                    )
                    break
                except (
                    botocore.exceptions.EndpointConnectionError,
                    botocore.exceptions.NoCredentialsError,
                    botocore.exceptions.PartialCredentialsError,
                    botocore.exceptions.SSLError,
                    botocore.exceptions.ClientError,
                    botocore.exceptions.BotoCoreError,
                    ConnectionError,
                ) as e:
                    wait_time = self.backoff_factor * (2**attempt) + random.uniform(
                        0, 1
                    )
                    if self.verbose:
                        print(
                            f"Retrying {path} due to: {e}. Waiting {wait_time:.2f} seconds before retrying..."
                        )
                    time.sleep(wait_time)
                    file = File(
                        content=None, path=path, status=Status.failed, exception=e
                    )
            else:
                if self.verbose:
                    print(f"Failed to download {path} after {self.n_retries} retries")
            if self.ordered:
                self.results[index] = file
            else:
                self.file_queue.put(file)

    def _worker(self):
        while len(self.paths) > 0:
            batch = []
            for _ in range(min(self.worker_batch_size, len(self.paths))):
                try:
                    index, path = self.paths.pop()
                    batch.append((index, path))
                except IndexError:
                    break
            if len(batch) > 0:
                self.download_batch(batch)

    def __iter__(self) -> Generator[File, None, None]:
        workers = []
        for _ in range(self.n_workers):
            worker_process = multiprocessing.Process(target=self._worker)
            worker_process.start()
            workers.append(worker_process)

        if self.ordered:
            for i in self.result_order:
                while any(p.is_alive() for p in workers) and i not in self.results:
                    continue  # wait for the item to appear
                yield self.results.pop(i)
        else:
            while any(p.is_alive() for p in workers) or not self.file_queue.empty():
                try:
                    yield self.file_queue.get(timeout=1)
                except Empty:
                    pass

        for worker in workers:
            worker.join()

    def __len__(self):
        return len(self.paths)
