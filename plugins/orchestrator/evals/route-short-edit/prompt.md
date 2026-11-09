---
description: A one-line edit in a known file goes to an editing tier, and the edit lands.
tags: [routing, short]
runs: 3
max_turns: 12
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

In src/auth/token.js the 15-minute token lifetime is a magic number. Pull it into a named constant TOKEN_TTL_MS at the top of the file and use it.
