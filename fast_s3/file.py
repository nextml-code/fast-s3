from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel


class Status(str, Enum):
    succeeded = "succeeded"
    failed = "failed"


class File(BaseModel, arbitrary_types_allowed=True):
    content: Any
    path: Union[str, Path]
    status: Status
    exception: Optional[Exception] = None

    def with_status(self, status: Status, exception: Optional[Exception] = None):
        attributes = dict(self)
        attributes.update(status=status, exception=exception)
        return File(**attributes)
