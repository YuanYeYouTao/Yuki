"""Disposable browser sessions. No credentials, grants, or business state in cookies."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import ControlQueryError
from qq_ai_bot.domain.identity import PrincipalId


@dataclass(frozen=True, slots=True)
class BrowserSession:
    principal_id: PrincipalId
    credential_digest: bytes
    csrf: str
    expires: float


class BrowserSessions:
    def __init__(self, access: ControlOperatorAccess, *, lifetime: int) -> None:
        self.access = access
        self.lifetime = lifetime
        self.sessions: dict[str, BrowserSession] = {}
        self.attempts: dict[str, list[float]] = {}

    async def login(self, credential: object, peer: str) -> tuple[str, BrowserSession]:
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}
        self.attempts = {
            k: [t for t in v if t > now - 60]
            for k, v in self.attempts.items()
            if any(t > now - 60 for t in v)
        }
        if len(self.attempts) >= 1024 and peer not in self.attempts:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        attempts = self.attempts.setdefault(peer, [])
        if len(attempts) >= 10 or len(self.sessions) >= 128:
            raise ControlQueryError(Problem(ProblemCode.OPERATION_UNAVAILABLE))
        attempts.append(now)
        principal = await self.access.authenticate(credential, source=PrincipalSource.FUTURE_WEB)
        if type(credential) is not str:
            raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
        token = secrets.token_urlsafe(32)
        session = BrowserSession(
            principal.principal_id,
            hashlib.sha256(credential.encode()).digest(),
            secrets.token_urlsafe(32),
            now + self.lifetime,
        )
        self.sessions[token] = session
        return token, session

    async def resolve(self, token: str | None) -> tuple[BrowserSession, ControlPrincipal]:
        session = self.sessions.get(token or "")
        if session is None or session.expires <= time.monotonic():
            self.sessions.pop(token or "", None)
            raise ControlQueryError(Problem(ProblemCode.UNAUTHENTICATED))
        try:
            principal = await self.access.resolve_session(
                session.principal_id, session.credential_digest
            )
        except ControlQueryError:
            self.sessions.pop(token or "", None)
            raise
        return session, principal

    def revoke(self, token: str | None) -> None:
        self.sessions.pop(token or "", None)
