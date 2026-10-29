from yuki.config import Settings


def test_reload_is_explicit_and_empty_soul_is_not_replaced_by_a_preset(tmp_path):
    path = tmp_path / "SOUL.md"
    path.write_text("最初的{{name}}", encoding="utf-8")
    settings = Settings(_env_file=None, soul_file=path)
    settings.apply_character_name("Alice", revision=3)
    path.write_bytes(b"")
    assert "最初的Alice" in settings.character_prompt
    settings.apply_soul_document(settings.read_soul_document())
    assert settings.soul == ""
    assert "Alice" in settings.character_prompt
    assert "【SOUL】" not in settings.character_prompt


def test_name_reference_is_not_recursively_interpreted(tmp_path):
    path = tmp_path / "SOUL.md"
    path.write_text("名字：{{name}}", encoding="utf-8")
    settings = Settings(_env_file=None, soul_file=path)
    settings.apply_character_name("{{name}}", revision=1)
    assert "名字：{{name}}" in settings.character_prompt


def test_an_older_committed_profile_cannot_replace_a_newer_projection(tmp_path):
    path = tmp_path / "SOUL.md"
    path.write_bytes(b"")
    settings = Settings(_env_file=None, soul_file=path)
    settings.apply_character_name("当前名字", revision=5)
    settings.apply_character_name("迟到的旧提交", revision=4)
    assert settings.character_identity.display_name == "当前名字"
    assert settings.character_identity.aliases == ("当前名字",)
