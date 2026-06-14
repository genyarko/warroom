"""Triage agent — LangGraph adapter. Phase 1 walking-skeleton.

Run locally:
    uv run python -m agents.triage.main

Or via docker-compose (preferred for the demo):
    docker compose up triage
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from thenvoi import Agent, AdapterFeatures
from thenvoi.adapters import LangGraphAdapter

from agents.triage.tools import langchain_tools
from shared.band_logging import LoggingPreprocessor, attach_sdk_logging
from shared.config import load_agent
from shared.logging import get_logger, log_event


AGENT_NAME = "triage"
PROMPT_PATH = Path(__file__).with_name("prompt.md")


async def main() -> None:
    log = get_logger(AGENT_NAME)
    attach_sdk_logging(AGENT_NAME)
    log_event(log, "boot", "loading config")

    creds = load_agent(AGENT_NAME)
    log_event(log, "config", f"framework={creds.framework} account={creds.account}")

    model = os.getenv("TRIAGE_MODEL", "gpt-4o")
    custom_section = PROMPT_PATH.read_text(encoding="utf-8")

    adapter = LangGraphAdapter(
        llm=ChatOpenAI(model=model),
        checkpointer=InMemorySaver(),
        custom_section=custom_section,
        # Domain tools: classify_alert, lookup_asset (Phase 3).
        additional_tools=langchain_tools(),
        # Allowlist (Phase 4) — PLATFORM tools only. include_tools filters the
        # built-in thenvoi_* tools; the domain tools in additional_tools above
        # are merged unconditionally by the adapter and are NOT affected (so
        # they must NOT be listed here, or the SDK logs "unknown tool" warnings).
        # Triage needs messaging + recruitment: it adds the specialists
        # classify_alert recommends into the (pre-created) incident room.
        # Deliberately NO remove_participant and NO create_chatroom — Triage
        # recruits into the existing room (see shared/protocol.md §E.2).
        features=AdapterFeatures(include_tools=[
            "thenvoi_send_message",
            "thenvoi_lookup_peers",
            "thenvoi_add_participant",
            "thenvoi_get_participants",
        ]),
    )
    agent = Agent.create(
        adapter=adapter,
        agent_id=creds.agent_id,
        api_key=creds.api_key,
        preprocessor=LoggingPreprocessor(log),
    )

    log_event(log, "connect", "running Band agent loop (Ctrl-C to stop)")
    await agent.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
