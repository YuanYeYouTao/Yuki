"""Server-configured operator authentication for CLI and future Web adapters."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ControlQueryError
from qq_ai_bot.domain.identity import PersonId, PrincipalId
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.database import Database


class OperatorDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    principal_id: str
    token_env: str
    person_id: str | None = None
    enabled: bool = True
    roles: tuple[str, ...] = ("operator",)
    capabilities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_declaration(self) -> OperatorDeclaration:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", self.token_env) is None:
            raise ValueError("invalid credential environment reference")
        # Existing protocol capability validation is the only grant catalog.
        self.principal(PrincipalSource.CLI)
        return self

    def principal(self, source: PrincipalSource) -> ControlPrincipal:
        return ControlPrincipal(
            principal_id=PrincipalId.parse(self.principal_id),
            person_id=PersonId.parse(self.person_id) if self.person_id is not None else None,
            source=source,
            roles=self.roles,
            granted_capabilities=self.capabilities,
            authenticated=True,
            active=self.enabled,
        )


class _OperatorFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operators: tuple[OperatorDeclaration, ...] = Field(default=(), max_length=64)


class ControlOperatorAccess:
    """No implicit admin. Credentials and grants come only from deployment configuration."""

    def __init__(self, database: Database, path: Path | None) -> None:
        self._database = database
        if path is None:
            self._operators: tuple[OperatorDeclaration, ...] = ()
            return
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            # TOML arrays become lists; normalize collection shape before strict validation.
            for item in payload.get("operators", ()):
                for key in ("roles", "capabilities"):
                    if key in item:
                        item[key] = tuple(item[key])
            operators = _OperatorFile.model_validate(payload).operators
            identities = [PrincipalId.parse(item.principal_id).text for item in operators]
            if len(set(identities)) != len(operators):
                raise ValueError("duplicate operator")
            if len({item.token_env for item in operators}) != len(operators):
                raise ValueError("duplicate credential reference")
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise ValueError("invalid control operator configuration") from exc
        self._operators = operators

    async def resolve_session(
        self, principal_id: PrincipalId, credential_digest: bytes
    ) -> ControlPrincipal:
        """Recheck server-owned grants and credential rotation on each browser request."""
        matches = [
            declaration
            for declaration in self._operators
            if PrincipalId.parse(declaration.principal_id) == principal_id
        ]
        if len(matches) != 1 or not matches[0].enabled:
            raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
        declaration = matches[0]
        secret = os.environ.get(declaration.token_env, "")
        if (
            not secret.isascii()
            or not 32 <= len(secret) <= 4096
            or not hmac.compare_digest(hashlib.sha256(secret.encode()).digest(), credential_digest)
        ):
            raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
        principal = declaration.principal(PrincipalSource.FUTURE_WEB)
        if principal.person_id is not None:
            async with self._database.sessions() as session:
                if await session.get(CanonicalPersonModel, principal.person_id.text) is None:
                    raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
        return principal

    async def authenticate(
        self,
        credential: object,
        *,
        source: PrincipalSource,
    ) -> ControlPrincipal:
        if type(source) is not PrincipalSource or source not in {
            PrincipalSource.CLI,
            PrincipalSource.FUTURE_WEB,
        }:
            raise ValueError("operator authentication only supports CLI/Web sources")
        matches = []
        if type(credential) is str and credential.isascii() and 32 <= len(credential) <= 4096:
            candidate = hashlib.sha256(credential.encode("utf-8")).digest()
            for operator in self._operators:
                secret = os.environ.get(operator.token_env, "")
                if (
                    secret.isascii()
                    and 32 <= len(secret) <= 4096
                    and hmac.compare_digest(
                        candidate,
                        hashlib.sha256(secret.encode("utf-8")).digest(),
                    )
                ):
                    matches.append(operator)
        if len(matches) != 1:
            raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
        operator = matches[0]
        if not operator.enabled:
            raise ControlQueryError(Problem(ProblemCode.PRECONDITION_FAILED))
        principal = operator.principal(source)
        if principal.person_id is not None:
            async with self._database.sessions() as session:
                if await session.get(CanonicalPersonModel, principal.person_id.text) is None:
                    raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
        return principal
