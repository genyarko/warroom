# WarRoom

Multi-agent cybersecurity incident response through Band.
**Track 3 — Regulated & High-Stakes Workflows · Band of Agents Hackathon**

A security alert fires. A **Triage** agent (LangGraph) classifies it, creates
a Band room, and recruits three specialists: **Threat Intel** (OpenAI SDK),
**Compliance** (Pydantic AI, lives on a second Band account, holds veto power
and the regulatory clock), and an **Incident Commander** (Anthropic) that
must collect explicit @mention sign-offs before executing any action. When
Intel and Compliance deadlock on the demo incident (active ransomware vs PII
host that's also forensic evidence), the Commander escalates to a human CISO
who rules in one message. The transcript *is* the audit trail.

**Status:** Phase 1 skeleton in place (Triage + Commander). Phase 0 platform
verification still needed before the skeleton can actually talk to Band —
see [`shared/protocol.md`](./shared/protocol.md) for the Phase 0 checklist
and [`PHASE_1_SMOKE_TEST.md`](./PHASE_1_SMOKE_TEST.md) for the Phase 1 exit
criterion. Full plan: [`warroom-implementation-plan.md`](./warroom-implementation-plan.md).

## Quickstart (once Phase 0 is complete)

```bash
cp .env.example .env                          # fill in OpenAI + Anthropic keys
cp agent_config.yaml.example agent_config.yaml # paste Band UUIDs + API keys
docker compose up                              # boot all four agents
python injector/inject_alert.py INC-C-ransomware-pii
```

Nothing past `cp .env.example` works yet — this README is a contract for what
Phase 8 has to deliver.
