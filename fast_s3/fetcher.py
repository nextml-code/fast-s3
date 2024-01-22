import io
from pathlib import Path
from typing import List, Union

from botocore.exceptions import ClientError
from s3transfer.exceptions import RetriesExceededError

from .file import File, Status
from .transfer_manager import transfer_manager


class Fetcher:
    def __init__(
        self,
        paths: List[Union[str, Path]],
        endpoint_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
        bucket_name: str,
        ordered=True,
        buffer_size=1024,
        n_workers=32,
        **transfer_manager_kwargs,
    ):
        self.paths = paths
        self.ordered = ordered
        self.buffer_size = buffer_size
        self.transfer_manager = transfer_manager(
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
            n_workers=n_workers,
            **transfer_manager_kwargs,
        )
        self.bucket_name = bucket_name
        self.files: List[File] = []
        self.current_path_index = 0

    def __len__(self):
        return len(self.paths)

    def __iter__(self):
        for _ in range(self.buffer_size):
            self.queue_download_()

        if self.ordered:
            for _ in range(len(self)):
                yield self.process_index(0)
        else:
            for _ in range(len(self)):
                for index, file in enumerate(self.files):
                    if file.future.done():
                        break
                else:
                    index = 0
                yield self.process_index(index)

    def process_index(self, index):
        file = self.files.pop(index)
        self.queue_download_()
        try:
            file.future.result()
            return file.with_status(Status.done)
        except (ClientError, RetriesExceededError) as e:
            return file.with_status(Status.error, exception=e)

    def queue_download_(self):
        if self.current_path_index < len(self):
            buffer = io.BytesIO()
            path = self.paths[self.current_path_index]
            self.files.append(
                File(
                    buffer=buffer,
                    future=self.transfer_manager.download(
                        fileobj=buffer,
                        bucket=self.bucket_name,
                        key=str(path),
                    ),
                    path=path,
                )
            )
            self.current_path_index += 1

    def close(self):
        self.transfer_manager.shutdown()
