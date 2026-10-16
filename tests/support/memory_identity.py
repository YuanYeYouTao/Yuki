"""Real explicit account transfer, separate from same-person link/merge."""

from uuid import uuid4

from tests.unit.test_client_identity import code_for

from qq_ai_bot.identity.canonical_repository import find_identity_binding


async def transfer_account(database, account: str, target_account: str = "1002"):
    api, target, _issued = await code_for(database, target_account)
    async with database.sessions() as reader:
        binding = await find_identity_binding(reader, account, platform="qq")
        binding_id = binding.id
    return await api.transfer_for_operator(binding_id, target, str(uuid4()), str(uuid4()))
