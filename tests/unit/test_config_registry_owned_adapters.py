"""Each registry owns derived Settings validation without changing scalar rules."""

from dataclasses import replace
from typing import Annotated

import pytest
from pydantic import Field
from pydantic.fields import FieldInfo

from yuki.admin.config_registry import ConfigRegistry
from yuki.config import Settings


def test_custom_registry_owns_its_annotation_and_new_registry_reads_new_metadata(monkeypatch):
    template = ConfigRegistry().get("asr.timeout_seconds")
    custom = replace(template, key="custom.timeout", aliases=(), settings_fields=("registry_test",))
    monkeypatch.setitem(
        Settings.model_fields, "registry_test", FieldInfo(annotation=Annotated[int, Field(ge=4)])
    )
    first = ConfigRegistry((custom,))
    assert first.convert(custom, "5") == 5
    monkeypatch.setitem(
        Settings.model_fields, "registry_test", FieldInfo(annotation=Annotated[int, Field(ge=7)])
    )
    second = ConfigRegistry((custom,))
    assert first.convert(custom, 5) == 5
    with pytest.raises(ValueError):
        second.convert(custom, 5)
    assert second.convert(custom, 7) == 7


def test_custom_scalar_limits_still_precede_settings_validation():
    template = ConfigRegistry().get("asr.timeout_seconds")
    spec = replace(template, key="custom.timeout", aliases=(), minimum=1, maximum=10)
    registry = ConfigRegistry((spec,))
    for value in (True, 0, 11, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            registry.convert(spec, value)
    assert registry.convert(spec, "7") == 7
