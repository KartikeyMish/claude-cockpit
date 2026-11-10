---
description: Two independent changes in two separate services go to two agents launched together, one per service.
tags: [routing, parallel]
runs: 3
max_turns: 15
timeout_seconds: 900
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

Two changes, one in each service. billing/ and notify/ are separate services that share no code and deploy on their own.

1. billing: `formatAmount` in billing/src/format.js always prints USD. Add a `currency` parameter that defaults to "USD", use it in the output, and cover the new parameter in billing/test/format.test.js.
2. notify: the retry limit in notify/src/send.js is hard-coded to 3. Read it from the `NOTIFY_MAX_RETRIES` environment variable, falling back to 3 when it is unset or not a positive integer, and cover both cases in notify/test/send.test.js.
