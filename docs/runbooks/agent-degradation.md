# Agent degradation

1. Identify the affected Agent, component, intent and immutable version from aggregate metrics.
2. Follow a sampled Trace through Run, RAG, Skill, MCP and provider spans; do not copy message or Prompt bodies.
3. Check provider latency, Tool error class, queue lag, token budget and recent version publications.
4. Disable the smallest affected Agent/Skill/Tool version with the governed kill switch; keep prohibited writes closed.
5. If fallback cannot provide grounded read-only help, transfer eligible conversations to a human.
6. Add confirmed failures to the candidate evaluation queue only after de-identification and human review.
