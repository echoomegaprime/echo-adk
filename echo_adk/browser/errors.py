"""Public browser errors contain fixed messages, never provider stderr or secrets."""

from __future__ import annotations


class BrowserError(Exception):
    def __init__(self, code: str, status: int, message: str, *, unknown_outcome: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.unknown_outcome = unknown_outcome

    def as_dict(self) -> dict:
        return {"code": self.code, "status": self.status, "message": self.message, "unknown_outcome": self.unknown_outcome}
