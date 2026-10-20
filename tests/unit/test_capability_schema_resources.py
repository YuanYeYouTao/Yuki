"""Schema resources are local; annotation objects remain ordinary data."""

import io
import json
import socket
import urllib.request

import pytest

from yuki.capabilities.catalog import UnifiedToolCatalogEntry
from yuki.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from yuki.capabilities.validation import JsonSchemaCapabilityValidator

REMOTE = "https://schema.invalid/reference.json"


def capability_entry(schema):
    return UnifiedToolCatalogEntry(
        descriptor=CapabilityDescriptor(
            canonical_name="resource_probe",
            model_name="resource_probe",
            group="probe.read",
            namespace="probe.read",
            input_schema=schema,
            output_schema={"type": "object"},
            effect=CapabilityEffect.READ_STATE,
            risk=CapabilityRisk.READ,
            trust_source=CapabilityTrustSource.CORE,
            required_permissions=frozenset(),
            uses_external_data=False,
            cancellable=True,
            idempotency=CapabilityIdempotency.IDEMPOTENT,
        )
    )


@pytest.fixture
def resource_retrievals(monkeypatch):
    calls = []

    def retrieve(request):
        calls.append(request.full_url)
        return io.BytesIO(b"{}")

    def unexpected_network(*args, **kwargs):
        raise AssertionError("Schema validation must not use real network resources")

    monkeypatch.setattr(urllib.request, "urlopen", retrieve)
    monkeypatch.setattr(socket, "create_connection", unexpected_network)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected_network)
    return calls


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        ({"$ref": REMOTE}, {}),
        ({"patternProperties": {"^a$": {"$ref": REMOTE}}}, {"a": 1}),
        ({"dependentSchemas": {"a": {"$ref": REMOTE}}}, {"a": 1}),
        ({"if": {"$ref": REMOTE}}, {}),
        ({"if": True, "then": {"$ref": REMOTE}}, {}),
        ({"if": False, "else": {"$ref": REMOTE}}, {}),
        ({"unevaluatedProperties": {"$ref": REMOTE}}, {"a": {}}),
    ],
    ids=["root", "pattern-properties", "dependent-schema", "if", "then", "else", "unevaluated"],
)
def test_every_schema_resource_position_rejects_remote_retrieval(
    schema, payload, resource_retrievals
):
    validator = JsonSchemaCapabilityValidator()
    validator.admit((capability_entry(schema),))
    result = validator.validate("resource_probe", json.dumps(payload))
    assert resource_retrievals == []
    assert result.ok is False


def test_defs_inside_an_example_is_data_not_a_schema_resource(resource_retrievals):
    schema = {"type": "object", "examples": [{"$defs": {"N": {"$ref": REMOTE}}}]}
    validator = JsonSchemaCapabilityValidator()
    assert validator.admit((capability_entry(schema),)) == ()
    assert validator.validate("resource_probe", "{}").ok
    assert resource_retrievals == []


@pytest.mark.parametrize("annotation", ["examples", "const"])
def test_ordinary_reference_shaped_data_is_not_retrieved(annotation, resource_retrievals):
    data = {"$ref": REMOTE}
    schema = {"type": "object", annotation: [data] if annotation == "examples" else data}
    validator = JsonSchemaCapabilityValidator()
    assert validator.admit((capability_entry(schema),)) == ()
    assert validator.validate("resource_probe", json.dumps(data)).ok
    assert resource_retrievals == []


def test_local_defs_are_resolved_and_still_enforce_the_declared_type(resource_retrievals):
    schema = {
        "type": "object",
        "$defs": {"Count": {"type": "integer"}},
        "properties": {"count": {"$ref": "#/$defs/Count"}},
        "required": ["count"],
    }
    validator = JsonSchemaCapabilityValidator()
    assert validator.admit((capability_entry(schema),)) == ()
    assert validator.validate("resource_probe", '{"count":3}').ok
    assert not validator.validate("resource_probe", '{"count":"three"}').ok
    assert resource_retrievals == []


def test_nested_schema_resources_keep_their_own_defs_scope(resource_retrievals):
    schema = {
        "type": "object",
        "$defs": {"Value": {"type": "string"}},
        "properties": {
            "item": {
                "$id": "urn:local:inner",
                "type": "object",
                "$defs": {"Value": {"type": "integer"}},
                "properties": {"value": {"$ref": "#/$defs/Value"}},
            },
            "root_value": {"$ref": "#/$defs/Value"},
        },
    }
    validator = JsonSchemaCapabilityValidator()
    assert validator.admit((capability_entry(schema),)) == ()
    assert validator.validate("resource_probe", '{"item":{"value":1},"root_value":"a"}').ok
    assert not validator.validate("resource_probe", '{"item":{"value":"a"}}').ok
    assert not validator.validate("resource_probe", '{"root_value":1}').ok
    assert resource_retrievals == []
