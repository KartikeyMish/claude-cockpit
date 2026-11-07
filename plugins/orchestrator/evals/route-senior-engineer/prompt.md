---
description: Delegates a migration that spans interacting components to senior-engineer.
tags: [routing]
runs: 3
max_turns: 12
timeout_seconds: 600
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

Add an order `status` column (pending, processed, failed) with a zero-downtime migration and a backfill from processed_at. Both the HTTP API and the background worker read and write orders, and they deploy separately, so old and new versions will run side by side during rollout.
