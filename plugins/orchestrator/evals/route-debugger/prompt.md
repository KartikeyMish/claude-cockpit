---
description: Delegates an intermittent, unexplained failure to debugger.
tags: [routing]
runs: 3
max_turns: 12
timeout_seconds: 600
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

test/queue.test.js fails roughly 1 run in 20 in CI with 'duplicate job', and in production we occasionally see the same order processed twice. Nobody can reproduce it locally. Find the actual root cause and prove it before proposing a fix.
