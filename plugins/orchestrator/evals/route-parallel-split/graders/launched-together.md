---
# Counted, not judged: both Agent calls must sit in the same assistant message (launched together).
# The trace streams each tool_use block as its own assistant event that keeps the message id, and a
# tool_result can land between siblings, so the check is "two Agent tool_use events share a msg id".
# two-editing-agents.md covers the count and subagent types; this covers the timing.
type: regex
target: trace
pattern: '"id":"(msg_[A-Za-z0-9]+)","type":"message","role":"assistant","content":\[\{"type":"tool_use","id":"toolu_[A-Za-z0-9]+","name":"Agent"[\s\S]*?"id":"\1","type":"message","role":"assistant","content":\[\{"type":"tool_use","id":"toolu_[A-Za-z0-9]+","name":"Agent"'
weight: 2
---
