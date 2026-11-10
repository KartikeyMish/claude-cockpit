---
# Judged, not counted: `claude plugin eval` graders (tool_used, regex, llm) expose neither which
# assistant message a tool call belongs to nor its timing, so "launched together" can't be checked
# deterministically. two-editing-agents.md covers the count; this covers the timing.
type: llm
weight: 2
---

Pass only if the billing change and the notify change were handed to two separate agents that ran in parallel: both Agent tool calls appear in the same assistant message, or the second Agent call was made before the first agent's result came back. Fail if a single agent did both changes, if the main session made either change itself, or if the second agent was launched only after the first one had reported back.
