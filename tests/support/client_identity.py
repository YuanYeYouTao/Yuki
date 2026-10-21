"""Real isolated identity wiring; callers can share their actual route Registry."""

from tests.support.gateway import builtin_registry

from yuki.client_api.identity import ClientIdentityService
from yuki.identity.routing import PresenceRouter
from yuki.persistence.database import Database


def client_identity_service(
    database: Database, router: PresenceRouter | None = None
) -> ClientIdentityService:
    return ClientIdentityService(database, router or PresenceRouter(database, builtin_registry()))
