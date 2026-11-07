---
description: Delegates a read-only codebase map to the scout tier.
tags: [routing]
runs: 3
max_turns: 12
timeout_seconds: 600
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

Before I touch anything: give me a map of how authentication works in this repo. Every place a token is created, verified or refreshed, and every route or middleware that depends on it, with file:line references. Don't change anything.
