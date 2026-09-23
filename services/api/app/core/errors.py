"""Typed error hierarchy. Every failure the platform can produce maps to one of these,
so API handlers never leak internals and callers can fail closed deterministically."""
from __future__ import annotations


class VaultlaError(Exception):
    """Base class. `code` is stable and safe to expose; `detail` is safe to expose;
    internal context goes in `__cause__` and is only logged, never returned."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, detail: str = "An internal error occurred.") -> None:
        super().__init__(detail)
        self.detail = detail


class ValidationFailed(VaultlaError):
    code, http_status = "validation_failed", 422


class NotAuthorized(VaultlaError):
    code, http_status = "not_authorized", 403


class NotFound(VaultlaError):
    code, http_status = "not_found", 404


class Conflict(VaultlaError):
    code, http_status = "conflict", 409


class QuotaExceeded(VaultlaError):
    code, http_status = "quota_exceeded", 402


class VaultStillSealed(VaultlaError):
    """Raised when key release is requested before the trigger has matured."""
    code, http_status = "vault_sealed", 423


class IntegrityError(VaultlaError):
    """Stored/uploaded ciphertext does not match what the client declared."""
    code, http_status = "integrity_failure", 422


class TimeConsensusError(VaultlaError):
    """Trusted time could not be established. Chronos MUST fail closed on this."""
    code, http_status = "time_unavailable", 503


class TimeRegressionError(TimeConsensusError):
    """Trusted time moved backwards versus the last recorded value (replay/spoof)."""
    code = "time_regression"


class ExternalServiceError(VaultlaError):
    code, http_status = "upstream_unavailable", 503
