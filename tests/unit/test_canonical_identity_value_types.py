"""C1 unit tests for canonical identity value types."""

from __future__ import annotations

import ast
import dataclasses
import os
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid1, uuid3, uuid4, uuid5

import pytest

from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import (
    AuthorKind,
    BindingId,
    ConnectionGeneration,
    ConversationGeneration,
    ConversationId,
    IdentityBindingId,
    PersonId,
    PresenceId,
    PrincipalId,
    RequestId,
    RouteGeneration,
    SpaceBindingId,
    SpaceId,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
IDENTITY_PATH = SRC_ROOT / "qq_ai_bot" / "domain" / "identity.py"
CONTROL_PATH = SRC_ROOT / "qq_ai_bot" / "domain" / "control.py"

ID_TYPES = (
    PersonId,
    SpaceId,
    IdentityBindingId,
    SpaceBindingId,
    PresenceId,
    ConversationId,
    PrincipalId,
    RequestId,
)
GENERATION_TYPES = (
    ConversationGeneration,
    RouteGeneration,
    ConnectionGeneration,
)
FORBIDDEN_AUTHOR_KINDS = (
    "command",
    "plugin",
    "automation",
    "scheduled_task",
    "migration",
    "user",
    "bot",
)
CANONICAL_UUID4_TEXT = "550e8400-e29b-41d4-a716-446655440000"
PROJECT_LAYER_PREFIXES = (
    "qq_ai_bot.admin",
    "qq_ai_bot.application",
    "qq_ai_bot.automation",
    "qq_ai_bot.capabilities",
    "qq_ai_bot.cli",
    "qq_ai_bot.conversation",
    "qq_ai_bot.control_plane",
    "qq_ai_bot.health",
    "qq_ai_bot.mcp",
    "qq_ai_bot.memory",
    "qq_ai_bot.persistence",
    "qq_ai_bot.plugin_host",
    "qq_ai_bot.plugins",
    "qq_ai_bot.runtime",
    "qq_ai_bot.services",
    "qq_ai_bot.speech",
    "nonebot",
    "nonebot_adapter_onebot",
    "sqlalchemy",
    "alembic",
    "pydantic",
    "pydantic_settings",
    "httpx",
    "yuki_plugin_sdk",
)


@dataclasses.dataclass(frozen=True, slots=True)
class _StubPrincipal:
    principal_id: PrincipalId


class _HarnessSource(StrEnum):
    QQ = "qq"
    CLI = "cli"
    FUTURE_WEB = "future_web"
    SYSTEM = "system"


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.modules: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.modules.append(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.modules.append("." * node.level + (node.module or ""))
            return
        self.modules.append(node.module or "")


def _imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    collector = _ImportCollector()
    collector.visit(tree)
    return tuple(collector.modules)


def _root(module: str) -> str:
    return module.lstrip(".").split(".", 1)[0]


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _import_in_subprocess(code: str) -> str:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(item for item in (str(SRC_ROOT), existing) if item)
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _assert_frozen_value_object(value: object, field: str, replacement: object) -> None:
    assert dataclasses.is_dataclass(value)
    assert not hasattr(value, "__dict__")
    assert getattr(type(value), "__final__", False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, replacement)
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        value.unexpected_attribute = "blocked"  # type: ignore[attr-defined]


@pytest.fixture(params=ID_TYPES, ids=lambda cls: cls.__name__)
def id_type(request: pytest.FixtureRequest) -> type:
    return request.param


@pytest.fixture(params=GENERATION_TYPES, ids=lambda cls: cls.__name__)
def generation_type(request: pytest.FixtureRequest) -> type:
    return request.param


class TestUuid4Identities:
    def test_new_parse_and_canonical_round_trip(self, id_type: type) -> None:
        created = id_type.new()
        assert type(created) is id_type
        assert type(created.value) is UUID
        assert created.value.version == 4
        assert created.text == str(created.value)
        assert str(created) == created.text
        assert len(created.text) == 36
        assert created.text == created.text.lower()
        assert created.text[14] == "4"
        assert id_type.parse(created.text) == created
        assert id_type.parse(created.value) == created
        assert id_type.parse(created.text.upper()) == created
        assert id_type.parse(CANONICAL_UUID4_TEXT).text == CANONICAL_UUID4_TEXT

    def test_parse_rejects_invalid_and_non_text36_strings(self, id_type: type) -> None:
        sample = id_type.new().text
        rejected = (
            "",
            "not-a-uuid",
            "550e8400-e29b-41d4-a716-44665544000",
            "123",
            sample.replace("-", ""),
            f"{{{sample}}}",
            f"urn:uuid:{sample}",
            f" {sample}",
            f"{sample} ",
            f" {sample} ",
            sample.replace("-", "", 1),
        )
        for raw in rejected:
            with pytest.raises(ValueError, match=r"TEXT\(36\)"):
                id_type.parse(raw)

    def test_parse_rejects_non_uuid4(self, id_type: type) -> None:
        non_v4 = (
            uuid1(),
            uuid3(UUID(int=0), "yuki"),
            uuid5(UUID(int=0), "yuki"),
            UUID(int=0),
            UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        )
        for raw in non_v4:
            with pytest.raises(ValueError, match="UUID4"):
                id_type.parse(raw)
            with pytest.raises(ValueError, match="UUID4"):
                id_type.parse(str(raw))
            with pytest.raises(ValueError, match="UUID4"):
                id_type(raw)

    def test_parse_rejects_wrong_python_types(self, id_type: type) -> None:
        for raw in (None, 1, 1.0, True, b"550e8400-e29b-41d4-a716-446655440000"):
            with pytest.raises(TypeError, match=r"str or uuid\.UUID"):
                id_type.parse(raw)

    def test_identities_are_nominal_slotted_frozen_and_hashable(self, id_type: type) -> None:
        first = id_type.new()
        second = id_type.parse(first.text)
        assert first == second
        assert hash(first) == hash(second)
        assert len({first, second}) == 1
        assert {first: "ok"}[second] == "ok"
        _assert_frozen_value_object(first, "value", uuid4())
        for other in ID_TYPES:
            if other is id_type:
                continue
            foreign = other.new()
            assert type(first) is not type(foreign)
            assert not isinstance(first, other)
            assert first != foreign

    def test_parse_rejects_other_identity_objects_without_str_coercion(self, id_type: type) -> None:
        foreign = next(other.new() for other in ID_TYPES if other is not id_type)
        with pytest.raises(TypeError, match="another identity"):
            id_type.parse(foreign)
        with pytest.raises(TypeError, match="another identity"):
            id_type.parse(id_type.new())

    def test_same_uuid_text_does_not_unify_nominal_types(self) -> None:
        text = PersonId.new().text
        person = PersonId.parse(text)
        presence = PresenceId.parse(text)
        assert person != presence
        assert person.value == presence.value
        assert len({person, presence}) == 2

    def test_binding_union_keeps_runtime_distinction(self) -> None:
        identity_binding: BindingId = IdentityBindingId.new()
        space_binding: BindingId = SpaceBindingId.new()
        assert type(identity_binding) is IdentityBindingId
        assert type(space_binding) is SpaceBindingId
        assert not isinstance(identity_binding, SpaceBindingId)
        assert not isinstance(space_binding, IdentityBindingId)
        with pytest.raises(TypeError, match="another identity"):
            SpaceBindingId.parse(identity_binding)
        with pytest.raises(TypeError, match="another identity"):
            IdentityBindingId.parse(space_binding)


class TestAuthorKind:
    def test_exact_four_states(self) -> None:
        assert [kind.value for kind in AuthorKind] == [
            "person",
            "yuki",
            "external_bot",
            "system",
        ]
        assert set(AuthorKind) == {
            AuthorKind.PERSON,
            AuthorKind.YUKI,
            AuthorKind.EXTERNAL_BOT,
            AuthorKind.SYSTEM,
        }
        assert len(AuthorKind) == 4

    @pytest.mark.parametrize("value", FORBIDDEN_AUTHOR_KINDS)
    def test_origin_labels_are_not_author_kinds(self, value: str) -> None:
        with pytest.raises(ValueError):
            AuthorKind(value)


class TestGenerations:
    def test_accepts_non_negative_ints_and_increments(self, generation_type: type) -> None:
        zero = generation_type(0)
        one = generation_type(1)
        assert zero.value == 0
        assert one.value == 1
        assert zero.incremented() == one
        assert type(zero.incremented()) is generation_type
        assert zero.incremented().incremented() == generation_type(2)
        assert zero != one

    def test_rejects_bool_negative_and_non_integers(self, generation_type: type) -> None:
        with pytest.raises(TypeError, match="must be an int"):
            generation_type(True)
        with pytest.raises(TypeError, match="must be an int"):
            generation_type(False)
        with pytest.raises(TypeError, match="must be an int"):
            generation_type(1.0)
        with pytest.raises(TypeError, match="must be an int"):
            generation_type("1")
        with pytest.raises(ValueError, match="non-negative"):
            generation_type(-1)

    def test_generations_are_nominal_slotted_frozen_and_not_interchangeable(self) -> None:
        conversation = ConversationGeneration(3)
        route = RouteGeneration(3)
        connection = ConnectionGeneration(3)
        assert conversation != route != connection
        assert conversation != connection
        assert type(conversation.incremented()) is ConversationGeneration
        assert conversation.incremented() != RouteGeneration(4)
        with pytest.raises(TypeError, match="must be an int"):
            ConversationGeneration(route)
        with pytest.raises(TypeError, match="must be an int"):
            RouteGeneration(conversation)
        _assert_frozen_value_object(conversation, "value", 4)
        _assert_frozen_value_object(route, "value", 4)
        _assert_frozen_value_object(connection, "value", 4)
        assert hash(ConversationGeneration(3)) == hash(ConversationGeneration(3))
        assert len({conversation, route, connection}) == 3


class TestDecisionContext:
    def test_is_immutable_and_does_not_interpret_source(self) -> None:
        principal = _StubPrincipal(PrincipalId.new())
        target = PersonId.new()
        request_id = RequestId.new()
        correlation_id = RequestId.new()
        context = DecisionContext(
            request_id=request_id,
            principal=principal,
            source="  keep-source  ",
            canonical_target=target,
            reason="inspect",
            correlation_id=correlation_id,
        )
        assert context.request_id == request_id
        assert context.correlation_id == correlation_id
        assert context.principal is principal
        assert context.source == "  keep-source  "
        assert context.canonical_target == target
        assert context.reason == "inspect"
        assert not hasattr(context, "__dict__")
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.source = "qq"  # type: ignore[misc]
        with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
            context.unexpected_attribute = "blocked"  # type: ignore[attr-defined]
        replaced = dataclasses.replace(context, reason="audit")
        assert replaced.reason == "audit"
        assert context.reason == "inspect"

    def test_accepts_custom_strenum_source_without_coercion(self) -> None:
        context = DecisionContext(
            request_id=RequestId.new(),
            principal=_StubPrincipal(PrincipalId.new()),
            source=_HarnessSource.FUTURE_WEB,
            canonical_target=SpaceId.new(),
            reason="preview",
        )
        assert context.source is _HarnessSource.FUTURE_WEB
        assert type(context.source) is _HarnessSource
        assert context.source == _HarnessSource.FUTURE_WEB
        assert context.source.value == "future_web"

    def test_rejects_invalid_envelope_fields(self) -> None:
        principal = _StubPrincipal(PrincipalId.new())
        target = SpaceId.new()
        with pytest.raises(TypeError, match="request_id"):
            DecisionContext(
                request_id="req",  # type: ignore[arg-type]
                principal=principal,
                source=_HarnessSource.CLI,
                canonical_target=target,
            )
        with pytest.raises(TypeError, match="principal_id"):
            DecisionContext(
                request_id=RequestId.new(),
                principal=object(),  # type: ignore[arg-type]
                source=_HarnessSource.CLI,
                canonical_target=target,
            )
        with pytest.raises(ValueError, match="canonical_target"):
            DecisionContext(
                request_id=RequestId.new(),
                principal=principal,
                source=_HarnessSource.SYSTEM,
                canonical_target=None,
            )
        with pytest.raises(TypeError, match="correlation_id"):
            DecisionContext(
                request_id=RequestId.new(),
                principal=principal,
                source=_HarnessSource.QQ,
                canonical_target=target,
                correlation_id=PrincipalId.new(),  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError, match="reason"):
            DecisionContext(
                request_id=RequestId.new(),
                principal=principal,
                source=_HarnessSource.CLI,
                canonical_target=target,
                reason=1,  # type: ignore[arg-type]
            )


class TestImportCycleAndBoundaries:
    def test_identity_module_imports_are_stdlib_only(self) -> None:
        imports = _imported_modules(IDENTITY_PATH)
        assert imports
        for module in imports:
            assert _root(module) in sys.stdlib_module_names
            assert not module.startswith("qq_ai_bot")

    def test_control_module_imports_only_identity(self) -> None:
        imports = _imported_modules(CONTROL_PATH)
        assert "qq_ai_bot.domain.identity" in imports
        for module in imports:
            if module == "qq_ai_bot.domain.identity":
                continue
            assert module != "qq_ai_bot.domain"
            assert not module.startswith("qq_ai_bot.domain.")
            assert _root(module) in sys.stdlib_module_names
            assert not any(_matches(module, prefix) for prefix in PROJECT_LAYER_PREFIXES)

    def test_fresh_process_package_import_does_not_cycle(self) -> None:
        output = _import_in_subprocess(
            "from qq_ai_bot.domain.identity import PersonId as Direct;"
            "from qq_ai_bot.domain import DecisionContext, PersonId as Exported;"
            "from qq_ai_bot.domain.control import DecisionContext as Control;"
            "print(Direct is Exported, Control is DecisionContext, Direct.new().text)"
        )
        identity, context_ok, text = output.strip().split(" ", 2)
        assert identity == "True"
        assert context_ok == "True"
        assert PersonId.parse(text).text == text


class TestConversationScopeBehavior:
    def test_existing_scope_behavior_is_unchanged(self) -> None:
        group = ConversationScope.group("bot-1", "group-1")
        same_group = ConversationScope.group("bot-1", "group-1")
        other_bot = ConversationScope.group("bot-2", "group-1")
        private = ConversationScope.private("bot-1", "peer-1")
        assert group == same_group
        assert group.key == "bot:bot-1:group:group-1"
        assert other_bot.key != group.key
        assert not hasattr(group, "user_id")
        assert group.scope_type is ScopeType.GROUP
        assert private.key == "bot:bot-1:private:peer-1"
        assert private.scope_type is ScopeType.PRIVATE
