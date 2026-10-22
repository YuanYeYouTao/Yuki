"""Connector credentials identify a device, never an App Conversation."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.credentials import DeviceCredentials, DevicePrincipal
from yuki.persistence.database import Database


class ConnectorIdentityService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.credentials = DeviceCredentials(database)

    async def authenticate(self, token: str) -> DevicePrincipal:
        async with self.database.read_session() as session:
            return await self.credentials.authenticate(session, token, purpose="connector")

    async def require_device(self, session: AsyncSession, device_id: str) -> DevicePrincipal:
        return await self.credentials.require_device(session, device_id, purpose="connector")

    async def pair(self, code: str, device_label: str) -> dict[str, str]:
        async with self.database.immediate_session() as session:
            now = datetime.now(UTC)
            issued = await self.credentials.code(session, code, now, purpose="connector")
            return await self.credentials.pair(session, issued, device_label, now=now)
