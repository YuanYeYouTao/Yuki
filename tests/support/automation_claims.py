"""Collect streaming claims only in tests that inspect a complete polling pass."""


async def collect_due(repository, **options):
    return tuple([row async for row in repository.claim_due(**options)])
