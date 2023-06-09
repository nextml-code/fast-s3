import io
from enum import Enum
from pathlib import Path
from typing import Union

from pydantic import BaseModel
from s3transfer.futures import TransferFuture


class Status(str, Enum):
    pending = "pending"
    done = "done"
    error = "error"


class File(BaseModel):
    buffer: io.BytesIO
    future: TransferFuture
    path: Union[str, Path]
    status: Status = Status.pending

    class Config:
        arbitrary_types_allowed = True

    def with_status(self, status: Status):
        attributes = self.dict()
        attributes.update(status=status)
        return File(**attributes)
