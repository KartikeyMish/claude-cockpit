---
description: A one-file lookup is answered inline, without spawning any agent.
tags: [routing]
runs: 3
max_turns: 6
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

What version is set in package.json?
