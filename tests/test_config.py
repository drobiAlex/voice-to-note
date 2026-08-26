from pathlib import Path

from voice_to_note.config import SETTINGS, home, read_config_file, resolve, source


def test_home_defaults_to_the_macos_application_support_directory():
    assert home({}) == Path.home() / "Library" / "Application Support" / "vtn"


def test_home_uses_vtn_home_when_set():
    assert home({"VTN_HOME": "/tmp/somewhere"}) == Path("/tmp/somewhere")


def test_env_var_beats_the_config_file():
    assert resolve("num_speakers", -1, int, {"VTN_NUM_SPEAKERS": "3"}, {"num_speakers": 2}) == 3


def test_config_file_beats_the_default():
    assert resolve("num_speakers", -1, int, {}, {"num_speakers": 2}) == 2


def test_default_applies_when_nothing_is_set():
    assert resolve("num_speakers", -1, int, {}, {}) == -1


def test_env_values_are_cast_to_the_setting_type():
    assert resolve("diar_threshold", 0.5, float, {"VTN_DIAR_THRESHOLD": "0.7"}, {}) == 0.7


def test_config_file_values_are_cast_to_the_setting_type():
    assert resolve("diar_threshold", 0.5, float, {}, {"diar_threshold": "0.7"}) == 0.7


def test_a_missing_config_file_leaves_the_defaults(tmp_path):
    assert read_config_file(tmp_path / "vtn.toml") == {}


def test_unknown_keys_in_the_config_file_are_ignored(tmp_path):
    path = tmp_path / "vtn.toml"
    path.write_text('whisper_model = "small"\nnot_a_setting = 42\n')
    settings = read_config_file(path)
    assert resolve("whisper_model", "large-v3-turbo", str, {}, settings) == "small"
    assert resolve("num_speakers", -1, int, {}, settings) == -1


def test_every_setting_resolves_to_its_known_default():
    assert {key: setting.default for key, setting in SETTINGS.items()} == {
        "whisper_model": "large-v3-turbo",
        "emb_model": "nemo_en_titanet_large.onnx",
        "whisper_threads": "auto",
        "whisper_beam_size": 5,
        "num_speakers": -1,
        "overlap_stages": "off",
        "diar_threshold": 0.5,
        "match_threshold": 0.5,
        "claude_model": "sonnet",
        "refine_model": "haiku",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "qwen3:8b",
        "llm_backends": "claude,ollama",
        "codex_model": "",
        "gemini_model": "",
        "claude_timeout_s": 600,
        "ollama_timeout_s": 1800,
        "codex_timeout_s": 600,
        "gemini_timeout_s": 600,
        "refine_workers": 4,
        "refine_window": 20,
        "whisper_repo_url": "https://github.com/ggml-org/whisper.cpp",
        "my_name": "Alex",
        "tui_theme": "textual-dark",
    }


def test_source_is_env_when_the_environment_variable_is_set():
    assert source("num_speakers", {"VTN_NUM_SPEAKERS": "3"}, {"num_speakers": 2}) == "env"


def test_source_is_file_when_only_the_config_file_has_it():
    assert source("num_speakers", {}, {"num_speakers": 2}) == "file"


def test_source_is_default_when_neither_is_set():
    assert source("num_speakers", {}, {}) == "default"
