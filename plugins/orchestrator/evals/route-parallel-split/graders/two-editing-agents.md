---
# One editing agent per service: a single agent doing both changes scores 1 launch and fails here.
type: tool_used
tool: Agent
input_match: '"subagent_type"\s*:\s*"orchestrator:(routine-worker|senior-engineer)"'
min: 2
weight: 2
---
