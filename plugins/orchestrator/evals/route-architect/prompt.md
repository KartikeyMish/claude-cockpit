---
description: Delegates a hard-to-reverse design decision to architect.
tags: [routing]
runs: 3
max_turns: 12
timeout_seconds: 600
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

We need to decide whether to replace the in-process job queue with a Postgres-backed queue (pg-boss) or move to a separate queue service (e.g. SQS). Recommend one, with the trade-offs and a phased migration plan.
