"""Local App effects commit through the canonical ledger transaction."""

from qq_ai_bot.gateway.social import TransportMessage, TransportReceipt, TransportTarget


class AppSocialOperations:
    file_caption_separate = False

    def plan_send(self, message: TransportMessage) -> tuple[TransportMessage, ...]:
        return (message,)

    async def send(
        self, handle: object, target: TransportTarget, message: TransportMessage
    ) -> TransportReceipt:
        return TransportReceipt(None)

    async def recall(self, handle: object, target: TransportTarget, reference: str) -> None:
        raise ValueError("capability_unavailable")

    async def is_member(self, handle: object, space: str, account: str) -> bool:
        return False
