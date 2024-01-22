import io
from enum import Enum
from pathlib import Path
from typing import Optional, Union

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
    exception: Optional[Exception] = None

    class Config:
        arbitrary_types_allowed = True

    def with_status(self, status: Status, exception: Optional[Exception] = None):
        attributes = self.dict()
        attributes.update(status=status, exception=exception)
        return File(**attributes)
