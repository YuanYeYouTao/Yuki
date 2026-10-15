"""A retired seed profile keeps its real overrides through hot loading and editing."""

import json

import pytest
from tests.conftest import make_settings
from tests.support.semantic_participation_host_helpers import _host

from qq_ai_bot.admin.config_files import ConfigFileError, ConfigFileService


async def test_old_profile_hot_load_and_versioned_save_preserve_overrides(database, tmp_path):
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
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    host, _ = await _host(database, tmp_path, observer=False)
    service = ConfigFileService(
        make_settings(database.url, semantic_participation_model_config_file=path),
        autonomy_parameters=host.control_model_parameters,
    )
    try:
        host._refresh_model_parameters()
        loaded = host.control_model_parameters()
        assert host._model_config_error is None
        for key, value in legacy.items():
            if key not in {
                "recall_source_decay_seconds",
                "contact_source_decay_seconds",
                "independent_source_factor",
            }:
                assert getattr(loaded, key) == value
        initial = await service.read("autonomous_model")
        assert initial["valid"] and initial["matches_loaded"]
        assert "recall_source_decay_seconds" not in initial["document"]
        assert "contact_source_decay_seconds" not in initial["parameter_schema"]["properties"]
        document = {**initial["document"], "continuation_review_span": 2}
        revision = await service.save(
            "autonomous_model", initial["revision"], {"document": document}
        )
        assert revision != initial["revision"]
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == document
        assert saved["source_interval_seconds"] == 45
        host._refresh_model_parameters()
        assert host.control_model_parameters().continuation_review_span == 2
        assert (await service.read("autonomous_model"))["matches_loaded"]
        with pytest.raises(ConfigFileError, match="version_conflict"):
            await service.save(
                "autonomous_model", initial["revision"], {"document": initial["document"]}
            )

        # Retiring these three names does not make arbitrary misspellings valid.
        path.write_text(json.dumps({**saved, "continuation_review_spam": 5}), encoding="utf-8")
        host._refresh_model_parameters()
        assert host._model_config_error == "ValidationError"
        assert host.control_model_parameters().continuation_review_span == 2
        assert not (await service.read("autonomous_model"))["valid"]
    finally:
        await host.close()
