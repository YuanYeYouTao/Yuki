"""Platform possession proof before automatic Person creation or chat admission."""

from urllib.parse import urlencode
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage
from yuki.gateway.models import ConnectionSnapshot
from yuki.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from yuki.identity.canonical_repository import find_presence
from yuki.identity.db_models import PresenceModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.database import write_failure_committed
from yuki.services.policies import message_command_text


class ClientLinkCommands:
    def __init__(
        self,
        identities: ClientIdentityService,
        registry: GatewayConnectionRegistry,
        public_url: str = "",
    ) -> None:
        self.identities, self.registry = identities, registry
        self.public_url = public_url

    def is_request(self, message: InboundMessage) -> bool:
        parts = message_command_text(message).strip().split(maxsplit=2)
        return len(parts) >= 2 and parts[0].casefold() == "/ai" and parts[1].casefold() == "link"

    async def _require_proof(
        self,
        session: AsyncSession,
        snapshot: ConnectionSnapshot,
        bot: object,
        message: InboundMessage,
    ) -> None:
        current = self.registry.resolve_by_handle(bot).snapshot
        presence = await session.get(PresenceModel, snapshot.presence_id)
        if (
            current != snapshot
            or presence is None
            or not presence.enabled
            or message.platform != snapshot.platform
            or presence.platform != snapshot.platform
            or presence.external_account_id != snapshot.external_account_id
            or (message.bot_user_id and message.bot_user_id != snapshot.external_account_id)
            or message.sender.is_bot
            or message.sender.user_id == snapshot.external_account_id
        ):
            raise ClientError("unauthenticated")

    async def execute(self, bot: object, message: InboundMessage) -> str:
        if message.scope_type is not ScopeType.PRIVATE:
            return "请在私聊中使用 /ai link。"
        parts = message_command_text(message).strip().split(maxsplit=2)
        code = parts[2].strip() if len(parts) == 3 else ""
        if len(code.split()) > 1:
            return "用法：/ai link，或 /ai link <绑定码>。"
        request_id = str(uuid4())
        try:
            while True:
                connection = self.registry.resolve_by_handle(bot).snapshot
                async with self.identities.database.read_session() as reader:
                    await reader.execute(text("BEGIN"))
                    presence = await find_presence(
                        reader, connection.external_account_id, platform=connection.platform
                    )
                    if presence is None or not presence.enabled:
                        raise ClientError("unauthenticated")
                    if connection.presence_id is None:
                        self.registry.bind_presence(
                            platform=connection.platform,
                            external_account_id=connection.external_account_id,
                            presence_id=presence.id,
                        )
                    snapshot = self.registry.resolve_by_handle(bot).snapshot
                    await self._require_proof(reader, snapshot, bot, message)
                    if code:
                        prepared_claim = await self.identities.prepare_account_claim(
                            code,
                            platform=snapshot.platform,
                            account=message.sender.user_id,
                            session=reader,
                            request_id=request_id,
                        )

                try:
                    async with self.identities.database.immediate_session() as writer:
                        await self._require_proof(writer, snapshot, bot, message)
                        if code:
                            result = await self.identities.claim_account(
                                code,
                                platform=snapshot.platform,
                                account=message.sender.user_id,
                                display_name=message.sender.nickname,
                                message_id=message.native_message_id or "",
                                session=writer,
                                prepared=prepared_claim,
                            )
                        else:
                            result = await self.identities.issue_for_account(
                                platform=snapshot.platform,
                                account=message.sender.user_id,
                                message_id=message.native_message_id or "",
                                session=writer,
                            )
                except CanonicalIdentityError as exc:
                    if (
                        exc.category != "person_merge_preparation_changed"
                        or write_failure_committed(exc) is not False
                    ):
                        raise
                    continue
                break
            if not code:
                reply = f"绑定码：{result['code']}（10 分钟内有效，使用一次后失效）。"
                if self.public_url and result.get("code"):
                    reply += "\n绑定链接：yuki://pair?" + urlencode(
                        {"server": self.public_url, "code": result["code"]}
                    )
                return reply
            if result["merged"]:
                return "身份已合并，原入口、会话和执行记录继续保留。"
            return "账号已绑定到目标身份。"
        except RegistryClosed:
            return "当前连接尚未完成身份认证，无法绑定。"
        except ClientError:
            return "绑定码或当前身份不可用；码只可使用一次，10 分钟后过期。"
