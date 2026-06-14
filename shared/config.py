"""Load Band agent credentials from agent_config.yaml and LLM keys from .env.

Each agent's main.py calls `load_agent("triage")` and gets back the (uuid,
api_key, model) it needs. Env keys (OPENAI_API_KEY, ANTHROPIC_API_KEY) are
loaded into os.environ as a side effect so SDK clients pick them up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "agent_config.yaml"


@dataclass(frozen=True)
class AgentCreds:
    name: str
    framework: str
    agent_id: str
    api_key: str
    account: str  # "primary" | "secondary"
    handle: str = ""  # Band handle, e.g. "@merolavtech/triage" (for mentions)


def _load_env() -> None:
    # .env.local takes precedence over .env so real secrets stay out of .env.
    for fname in (".env", ".env.local"):
        path = REPO_ROOT / fname
        if path.exists():
            load_dotenv(path, override=True)


def load_agent(name: str, config_path: Path | None = None) -> AgentCreds:
    _load_env()
    path = config_path or Path(os.getenv("AGENT_CONFIG_PATH", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"agent_config.yaml not found at {path}. Copy "
            "agent_config.yaml.example and fill in Band UUIDs + API keys."
        )
    with path.open() as f:
        cfg = yaml.safe_load(f)
    agents = cfg.get("agents", {})
    if name not in agents:
        raise KeyError(f"agent '{name}' not in {path}. Have: {list(agents)}")
    a = agents[name]
    for required in ("agent_id", "api_key", "framework"):
        if not a.get(required) or "REPLACE" in str(a[required]):
            raise ValueError(
                f"agent '{name}' is missing {required} (or still has the "
                "REPLACE_WITH_... placeholder). Register the External Agent "
                "on app.band.ai and fill in agent_config.yaml."
            )
    return AgentCreds(
        name=name,
        framework=a["framework"],
        agent_id=a["agent_id"],
        api_key=a["api_key"],
        account=a.get("account", "primary"),
        handle=a.get("handle", ""),
    )


def default_room_id(config_path: Path | None = None) -> str | None:
    """Optional pre-created room ID (set if Phase 0 found Triage can't create
    rooms, or for the smoke test before Triage gets that capability)."""
    path = config_path or Path(os.getenv("AGENT_CONFIG_PATH", DEFAULT_CONFIG_PATH))
    if not path.exists():
        return None
    with path.open() as f:
        cfg = yaml.safe_load(f)
    room = (cfg.get("room") or {}).get("default_room_id") or None
    return room
