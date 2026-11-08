"""Stable, credential-free connection errors for owner APIs and worker envelopes."""

from __future__ import annotations


class ConnectionError(Exception):
    """An intentional refusal; messages must never contain provider response text."""

    def __init__(
        self, code: str, message: str, status: int = 400, *, outcome_unknown: bool = False, provider_status: int | None = None
    ) -> None:
        self.code = code
        self.status = status
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "outcome_unknown": self.outcome_unknown}
