---
description: Even a one-file lookup opens with an agent (scout) instead of inline reads, and still gets answered.
tags: [routing, short]
runs: 3
max_turns: 8
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

What version is set in package.json?
