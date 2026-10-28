"""Local App effects commit through the canonical ledger transaction."""

from yuki.domain.conversations import ConversationScope
from yuki.gateway.social import (
    TransportMention,
    TransportMessage,
    TransportReceipt,
    TransportTarget,
)
from yuki.social.models import SocialError


class AppSocialOperations:
    def native_reference_namespace(self, *, account_id: str, scope: ConversationScope) -> None:
        return None

    def native_mention(self, text: str, offset: int) -> TransportMention | None:
        return None

    def plan_send(self, message: TransportMessage) -> tuple[TransportMessage, ...]:
        return (message,)

    async def send(
        self, handle: object, target: TransportTarget, message: TransportMessage
    ) -> TransportReceipt:
        raise SocialError("capability_unavailable")

    async def recall(self, handle: object, target: TransportTarget, reference: str) -> None:
        raise SocialError("capability_unavailable")

    async def is_member(self, handle: object, space: str, account: str) -> bool:
        return False
