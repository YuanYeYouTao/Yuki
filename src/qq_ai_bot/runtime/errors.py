"""Errors raised by the runtime turn domain."""

from __future__ import annotations


class RuntimeDomainError(Exception):
    """Base class for all runtime turn domain errors."""


class InvalidTurnTriggerError(RuntimeDomainError):
    """A turn trigger was constructed with inconsistent fields."""


class InvalidTurnContextError(RuntimeDomainError):
    """A turn context violated a construction invariant."""


class UntrustedFinalizationError(RuntimeDomainError):
    """Terminal finalization metadata came from an untrusted source."""


class ProviderRegistryFrozenError(RuntimeDomainError):
    """Registration attempted after the provider registry was frozen."""


class ProviderRegistryNotFrozenError(RuntimeDomainError):
    """Lookup attempted before the provider registry was frozen."""
