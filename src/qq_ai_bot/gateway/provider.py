"""Provider-neutral gateway metadata and connection profiling contracts."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, final


def _provider_token(value: str, *, name: str) -> str:
    token = value.strip().casefold()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", token) is None:
        raise ValueError(f"{name} must be a canonical provider token")
    return token


def _capability_token(value: str) -> str:
    token = value.strip().casefold()
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", token) is None:
        raise ValueError("capability must be a canonical token")
    return token


def _external_account_id(value: object) -> str:
    external = str(value).strip()
    if not external or len(external) > 128:
        raise ValueError("gateway external account id must be non-empty")
    return external


@final
@dataclass(frozen=True, slots=True)
class GatewayConnectionProfile:
    """Provider-owned, secret-free facts used to register one live handle."""

    provider_id: str
    platform: str
    external_account_id: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _provider_token(self.provider_id, name="provider_id"),
        )
        object.__setattr__(self, "platform", _provider_token(self.platform, name="platform"))
        object.__setattr__(
            self,
            "external_account_id",
            _external_account_id(self.external_account_id),
        )
        normalized = frozenset(_capability_token(item) for item in self.capabilities)
        object.__setattr__(self, "capabilities", normalized)


class GatewayProvider(Protocol):
    """One gateway implementation below the canonical QQ/platform boundary."""

    @property
    def provider_id(self) -> str: ...

    def describe_connection(self, handle: object) -> GatewayConnectionProfile: ...


@final
class GatewayProviderCatalog:
    """Immutable provider catalog; multi-provider selection is always explicit."""

    def __init__(self, providers: Iterable[GatewayProvider]) -> None:
        registered: dict[str, GatewayProvider] = {}
        for provider in providers:
            provider_id = _provider_token(provider.provider_id, name="provider_id")
            if provider_id in registered:
                raise ValueError("duplicate gateway provider id")
            registered[provider_id] = provider
        if not registered:
            raise ValueError("at least one gateway provider is required")
        self._providers = registered

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def describe_connection(
        self,
        handle: object,
        *,
        provider_id: str | None = None,
    ) -> GatewayConnectionProfile:
        if provider_id is None:
            if len(self._providers) != 1:
                raise ValueError(
                    "provider_id is required when multiple gateway providers are registered"
                )
            selected = next(iter(self._providers))
        else:
            selected = _provider_token(provider_id, name="provider_id")
        provider = self._providers.get(selected)
        if provider is None:
            raise ValueError("gateway provider is not registered")
        profile = provider.describe_connection(handle)
        if profile.provider_id != selected:
            raise ValueError("gateway provider returned a mismatched provider id")
        return profile
