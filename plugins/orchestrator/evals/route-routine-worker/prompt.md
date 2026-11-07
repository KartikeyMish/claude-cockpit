---
description: Delegates clear-spec docs/config work to routine-worker.
tags: [routing]
runs: 3
max_turns: 12
timeout_seconds: 600
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

Every handler under src/modules/ needs a one-line JSDoc comment above `handle` describing what it returns, and every module that exports a helper needs a node:test unit test next to it (handler.test.js) covering that helper. Follow the existing style.
