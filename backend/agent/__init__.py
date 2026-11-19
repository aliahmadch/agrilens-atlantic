"""AgriLens Field Resilience Agent.

An additive agentic layer over the existing AgriLens analysis services. The agent
observes a field, validates evidence freshness and agreement, diagnoses risk with
the existing moisture/drought/crop-health/irrigation engines (used as tools),
proposes actions that pass deterministic safety policy, requests farmer approval
for consequential actions, and evaluates outcomes before re-planning.

No existing pipeline is modified: every tool is a thin wrapper over a service that
the portal already uses.
"""
