"""Tests for agent_runtime.config — agent-config.yaml schema and validation.

Covers: Pydantic validation, defaults, YAML round-trip, model profiles,
scorer weights, polling intervals, field validation.
"""

import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Construction and defaults
# ---------------------------------------------------------------------------

class TestAgentConfigDefaults:
    def test_default_config_has_all_fields(self):
        from agent_runtime.config import generate_default_config
        cfg = generate_default_config()
        assert cfg.agent_id == "my-agent"
        assert cfg.display_name == "My Agent"
        assert cfg.watch_tags == []
        assert cfg.active_model_profile == "medium"

    def test_default_scorer_weights(self):
        from agent_runtime.config import generate_default_config
        cfg = generate_default_config()
        assert cfg.scorer.directed_to_me == 10.0
        assert cfg.scorer.contradiction_of_mine == 8.0
        assert cfg.scorer.request_to_me == 9.0
        assert cfg.scorer.watched_tag_match == 3.0
        assert cfg.scorer.recency_halflife_hours == 24.0

    def test_default_polling_intervals(self):
        from agent_runtime.config import generate_default_config
        cfg = generate_default_config()
        assert cfg.polling.active_seconds == 10.0
        assert cfg.polling.idle_seconds == 60.0
        assert cfg.polling.active_to_idle_after == 300.0

    def test_three_model_profiles(self):
        from agent_runtime.config import generate_default_config
        cfg = generate_default_config()
        assert set(cfg.model_profiles.keys()) == {"small", "medium", "large"}
        assert cfg.model_profiles["small"].context_window == 4096
        assert cfg.model_profiles["medium"].context_window == 16384
        assert cfg.model_profiles["large"].context_window == 32768

    def test_get_active_profile(self):
        from agent_runtime.config import generate_default_config
        cfg = generate_default_config()
        profile = cfg.get_active_profile()
        assert profile.name == "medium"
        assert profile.context_window == 16384

    def test_stage_budgets_sum_to_context_window(self):
        from agent_runtime.config import generate_default_config
        cfg = generate_default_config()
        for name, profile in cfg.model_profiles.items():
            total = (
                profile.stage1_core_memory + profile.stage2_notifications +
                profile.stage3_history + profile.stage4_fetched +
                profile.stage5_instructions
            )
            assert total <= profile.context_window, (
                f"Profile {name}: stage budgets ({total}) exceed context_window ({profile.context_window})"
            )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestConfigValidation:
    def test_invalid_model_profile_name(self):
        from agent_runtime.config import ModelProfile
        with pytest.raises(ValueError, match="small/medium/large"):
            ModelProfile(
                name="huge", context_window=65536,
                stage1_core_memory=1024, stage2_notifications=1024,
                stage3_history=1024, stage4_fetched=1024,
                stage5_instructions=1024,
            )

    def test_invalid_active_model_profile(self):
        from agent_runtime.config import AgentConfig
        with pytest.raises(ValueError, match="small/medium/large"):
            AgentConfig(
                agent_id="test", display_name="Test",
                active_model_profile="huge",
            )

    def test_polling_active_too_low(self):
        from agent_runtime.config import PollingIntervals
        with pytest.raises(ValueError):
            PollingIntervals(active_seconds=0.5)

    def test_polling_idle_too_low(self):
        from agent_runtime.config import PollingIntervals
        with pytest.raises(ValueError):
            PollingIntervals(idle_seconds=2.0)

    def test_custom_scorer_weights(self):
        from agent_runtime.config import AgentConfig, ScorerWeights
        cfg = AgentConfig(
            agent_id="custom", display_name="Custom",
            scorer=ScorerWeights(directed_to_me=20.0, watched_tag_match=5.0),
        )
        assert cfg.scorer.directed_to_me == 20.0
        assert cfg.scorer.watched_tag_match == 5.0
        # Unset fields keep defaults
        assert cfg.scorer.contradiction_of_mine == 8.0


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------

class TestYAMLRoundTrip:
    def test_write_and_read_yaml(self):
        from agent_runtime.config import generate_default_config
        cfg = generate_default_config(agent_id="yaml-test", display_name="YAML Test")
        cfg.watch_tags = ["research", "ml"]

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = Path(f.name)

        try:
            cfg.to_yaml(path)
            loaded = cfg.from_yaml(path)
            assert loaded.agent_id == "yaml-test"
            assert loaded.display_name == "YAML Test"
            assert loaded.watch_tags == ["research", "ml"]
            assert loaded.scorer.directed_to_me == 10.0
            assert loaded.active_model_profile == "medium"
        finally:
            path.unlink(missing_ok=True)

    def test_load_default_config_file(self):
        """Load the shipped agent-config.default.yaml."""
        from agent_runtime.config import AgentConfig
        path = Path(__file__).parent.parent / "agent-config.default.yaml"
        if path.exists():
            cfg = AgentConfig.from_yaml(path)
            assert cfg.agent_id == "my-agent"
            assert "research" in cfg.watch_tags
            assert cfg.scorer.directed_to_me == 10.0
