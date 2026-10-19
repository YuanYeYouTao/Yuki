"""One source of authorization for confirmed Work result events and personal files."""

from typing import Any

from sqlalchemy import func, or_, select

from yuki.client_api.works import work_access
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.person_identity import person_family_condition
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_recovery_schema import deliveries
from yuki.runtime.work_schema_v1 import effects, work
from yuki.social.db_models import SocialOperationModel


def result_for_work(identity: Any) -> Any:
    return (
        select(SocialOperationModel.id)
        .where(
            SocialOperationModel.event_id == ChatEventModel.id,
            SocialOperationModel.status == "succeeded",
            or_(
                select(deliveries.c.id)
                .where(deliveries.c.work_id == identity, deliveries.c.id == SocialOperationModel.id)
                .correlate(SocialOperationModel, work)
                .exists(),
                select(effects.c.effect_key)
                .where(
                    effects.c.work_id == identity,
                    func.json_extract(effects.c.receipt_json, "$.invocation.original_domain_ref")
                    == "social:" + SocialOperationModel.id,
                )
                .correlate(SocialOperationModel, work)
                .exists(),
            ),
        )
        .correlate(ChatEventModel, work)
        .exists()
    )


def confirmed_outbound() -> Any:
    return (
        ChatEventModel.direction == "outbound",
        ChatEventModel.author_kind == "yuki",
        ChatEventModel.suppression_status == "keeper",
        select(SocialOperationModel.id)
        .where(
            SocialOperationModel.event_id == ChatEventModel.id,
            SocialOperationModel.status == "succeeded",
        )
        .exists(),
    )


def visible_result_work(person_id: str) -> Any:
    return (
        select(work.c.id)
        .join(CanonicalConversationModel, CanonicalConversationModel.id == work.c.conversation_id)
        .where(work_access(person_id), result_for_work(work.c.id))
        .correlate(ChatEventModel)
    )


def visible_outbound(person_id: str) -> Any:
    personal = (
        select(CanonicalConversationModel.id)
        .where(
            CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
            CanonicalConversationModel.kind == "private",
            person_family_condition(CanonicalConversationModel.person_id, person_id),
        )
        .exists()
    )
    return or_(personal, visible_result_work(person_id).exists())
