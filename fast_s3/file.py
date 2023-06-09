import io
from pathlib import Path
from typing import Union

from pydantic import BaseModel
from s3transfer.futures import TransferFuture


class File(BaseModel):
    buffer: io.BytesIO
    future: TransferFuture
    path: Union[str, Path]

    class Config:
        arbitrary_types_allowed = True
