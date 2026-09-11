"""Charge confirmed-turn output against the same budget as Agent send tools."""

from qq_ai_bot.domain.messages import OutboundMessage, OutboundSendReceipt
from qq_ai_bot.sandbox.progress import TaskProgress
from qq_ai_bot.services.reply_sequence import OutboundSender


class BudgetSender:
    def __init__(self, sender: OutboundSender, progress: TaskProgress) -> None:
        self.sender, self.progress = sender, progress

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        await self.progress.reserve_message()
        return await self.sender.send(message)
