"""Agent configuration schema — Section 9, 10.3, 11.3 of the spec.

Defines agent-config.yaml structure with Pydantic validation.
Scorer weights, polling intervals, model profiles, watch tags.
"""

from __future__ import annotations

import yaml
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Model capability profiles (Section 11.3)
# ---------------------------------------------------------------------------

class ModelProfile(BaseModel):
    """Token budget for a model capability tier."""
    name: str = Field(description="Profile name: small, medium, or large")
    context_window: int = Field(description="Total context window in tokens")
    stage1_core_memory: int = Field(description="Tokens reserved for core memory / identity")
    stage2_notifications: int = Field(description="Tokens for notification batch")
    stage3_history: int = Field(description="Tokens for relevant history")
    stage4_fetched: int = Field(description="Tokens for fetched content")
    stage5_instructions: int = Field(description="Tokens for instructions / skill")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if v not in ("small", "medium", "large"):
            raise ValueError(f"Model profile name must be small/medium/large, got {v!r}")
        return v


# ---------------------------------------------------------------------------
# Scorer weights (Section 10.3)
# ---------------------------------------------------------------------------

class ScorerWeights(BaseModel):
    """Priority scoring weights for notification triage."""
    directed_to_me: float = Field(default=10.0, description="Weight for entries directed at this agent")
    contradiction_of_mine: float = Field(default=8.0, description="Weight for contradictions of own entries")
    request_to_me: float = Field(default=9.0, description="Weight for requests directed at this agent")
    watched_tag_match: float = Field(default=3.0, description="Weight for entries matching watched tags")
    high_confidence: float = Field(default=1.5, description="Weight multiplier for high-confidence entries")
    low_confidence: float = Field(default=0.5, description="Weight multiplier for low-confidence entries")
    recency_halflife_hours: float = Field(default=24.0, description="Half-life for time decay in hours")
    synthesis_proposed: float = Field(default=4.0, description="Weight for proposed syntheses")
    question_in_domain: float = Field(default=3.5, description="Weight for questions in watched tags")


# ---------------------------------------------------------------------------
# Polling intervals (Section 8)
# ---------------------------------------------------------------------------

class PollingIntervals(BaseModel):
    """Adaptive polling configuration."""
    active_seconds: float = Field(default=10.0, ge=1.0, description="Polling interval when active (seconds)")
    idle_seconds: float = Field(default=60.0, ge=5.0, description="Polling interval when idle (seconds)")
    active_to_idle_after: float = Field(
        default=300.0, ge=30.0,
        description="Seconds of no activity before switching to idle polling"
    )


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    """Complete agent configuration — maps to agent-config.yaml."""
    agent_id: str = Field(description="Unique agent identifier")
    display_name: str = Field(description="Human-readable agent name")
    watch_tags: list[str] = Field(default_factory=list, description="Tags this agent monitors")
    scorer: ScorerWeights = Field(default_factory=ScorerWeights)
    polling: PollingIntervals = Field(default_factory=PollingIntervals)
    active_model_profile: str = Field(
        default="medium",
        description="Which model profile to use: small, medium, or large"
    )
    model_profiles: dict[str, ModelProfile] = Field(
        default_factory=lambda: {
            "small": ModelProfile(
                name="small", context_window=4096,
                stage1_core_memory=512, stage2_notifications=1024,
                stage3_history=1024, stage4_fetched=1024,
                stage5_instructions=512,
            ),
            "medium": ModelProfile(
                name="medium", context_window=16384,
                stage1_core_memory=1024, stage2_notifications=4096,
                stage3_history=4096, stage4_fetched=5120,
                stage5_instructions=2048,
            ),
            "large": ModelProfile(
                name="large", context_window=32768,
                stage1_core_memory=2048, stage2_notifications=8192,
                stage3_history=8192, stage4_fetched=10240,
                stage5_instructions=4096,
            ),
        },
        description="Available model capability profiles"
    )
    bbs_db_path: Optional[str] = Field(default=None, description="Path to BBS SQLite database")
    working_memory_db_path: Optional[str] = Field(
        default=None, description="Path to working memory SQLite database"
    )

    @field_validator("active_model_profile")
    @classmethod
    def validate_active_profile(cls, v: str) -> str:
        if v not in ("small", "medium", "large"):
            raise ValueError(f"active_model_profile must be small/medium/large, got {v!r}")
        return v

    def get_active_profile(self) -> ModelProfile:
        """Return the currently active model profile."""
        return self.model_profiles[self.active_model_profile]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentConfig":
        """Load config from a YAML file."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def to_yaml(self, path: str | Path) -> None:
        """Write config to a YAML file."""
        with open(path, "w") as f:
            yaml.dump(self.model_dump(), f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Default config generator
# ---------------------------------------------------------------------------

def generate_default_config(agent_id: str = "my-agent", display_name: str = "My Agent") -> AgentConfig:
    """Create a default AgentConfig with all spec defaults."""
    return AgentConfig(agent_id=agent_id, display_name=display_name)
