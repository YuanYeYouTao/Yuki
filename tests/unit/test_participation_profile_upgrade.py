"""Retired observation/seed keys preserve real hot-loaded model parameters."""

import json

from tests.support.semantic_participation_host_helpers import _host

from yuki.services.participation_parameters import AutonomyParameters


async def test_old_profile_hot_load_and_explicit_file_update_preserve_overrides(database, tmp_path):
    path = tmp_path / "autonomous-model.json"
    legacy = {
        "source_interval_seconds": 45.0,
        "intrinsic_interval_seconds": 600.0,
        "work_fast_weight": 1.8,
        "work_slow_weight": 0.25,
        "exposure_weight": 0.55,
        "reception_weight": 0.5,
        "no_reply_weight": 0.8,
        "recall_source_decay_seconds": 3600,
        "contact_source_decay_seconds": 1800,
        "independent_source_factor": 0.35,
        "continuation_review_span": 4,
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    host, _ = await _host(database, tmp_path, observer=False)
    try:
        host._refresh_model_parameters()
        loaded = host.control_model_parameters()
        assert host._model_config_error is None
        for key, value in legacy.items():
            if key not in {
                "recall_source_decay_seconds",
                "contact_source_decay_seconds",
                "independent_source_factor",
                "continuation_review_span",
            }:
                assert getattr(loaded, key) == value
        document = loaded.model_dump(mode="json")
        assert "recall_source_decay_seconds" not in document
        assert "continuation_review_span" not in document
        assert (
            "contact_source_decay_seconds"
            not in AutonomyParameters.model_json_schema()["properties"]
        )
        document["source_interval_seconds"] = 60
        pending = path.with_suffix(".next")
        pending.write_text(json.dumps(document), encoding="utf-8")
        pending.replace(path)
        assert host.control_model_parameters().source_interval_seconds == 45
        host._refresh_model_parameters()
        assert host.control_model_parameters().source_interval_seconds == 60
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == document

        # Clearing retired fields does not make arbitrary misspellings valid.
        path.write_text(json.dumps({**saved, "continuation_review_spam": 5}), encoding="utf-8")
        host._refresh_model_parameters()
        assert host._model_config_error == "ValidationError"
        assert host.control_model_parameters().source_interval_seconds == 60
    finally:
        await host.close()
