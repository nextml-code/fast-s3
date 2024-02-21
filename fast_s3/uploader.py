from enum import Enum
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel
from s3transfer.futures import TransferFuture

from .transfer_manager import transfer_manager


class Status(str, Enum):
    done = "done"
    error = "error"


class Result(BaseModel, arbitrary_types_allowed=True):
    status: Status
    exception: Optional[Exception] = None


class Uploader:
    def __init__(
        self,
        endpoint_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
        bucket_name: str,
        n_workers=32,
        **transfer_manager_kwargs,
    ):
        self.transfer_manager = transfer_manager(
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
            n_workers=n_workers,
            **transfer_manager_kwargs,
        )
        self.bucket_name = bucket_name
        self.futures: List[TransferFuture] = []

    def queue_upload(
        self,
        source: List[Union[str, bytes]],
        destination: List[Union[str, Path]],
    ):
        if len(source) != len(destination):
            raise ValueError(
                "The number of source files and destination paths must be equal."
            )
        for file, path in zip(source, destination):
            self.futures.append(
                self.transfer_manager.upload(
                    fileobj=file,
                    bucket=self.bucket_name,
                    key=str(path),
                )
            )

    def await_upload(self):
        results = []
        for future in self.futures:
            try:
                future.result()
                results.append(Result(status=Status.done))
            except Exception as e:
                results.append(Result(status=Status.error, exception=e))
        self.futures = []
        return results

    def close(self):
        self.transfer_manager.shutdown()
