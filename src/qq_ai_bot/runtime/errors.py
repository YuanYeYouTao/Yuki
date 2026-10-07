"""Errors raised by the runtime turn domain."""

from __future__ import annotations


class RuntimeDomainError(Exception):
    """Base class for all runtime turn domain errors."""


class InvalidTurnTriggerError(RuntimeDomainError):
    """A turn trigger was constructed with inconsistent fields."""


class InvalidTurnContextError(RuntimeDomainError):
    """A turn context violated a construction invariant."""
