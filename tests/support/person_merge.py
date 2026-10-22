"""Seed an atomic Person merge inside a caller-owned fixture transaction.

Runtime callers prepare before writer admission. Test setup already owns its
transaction and deliberately uses that same session for both phases.
"""

from yuki.identity.person_identity import merge_persons as apply_person_merge
from yuki.identity.person_identity import prepare_person_merge


async def merge_persons(session, source, target, **kwargs):
    prepared = await prepare_person_merge(session, source, target)
    return await apply_person_merge(session, source, target, prepared=prepared, **kwargs)
