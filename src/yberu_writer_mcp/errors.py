"""Structured application errors shared by the tool layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WriterError(Exception):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.details:
            result["details"] = self.details
        return result
