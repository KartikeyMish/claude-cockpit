---
description: When the user explicitly says "do it yourself", the request is handled inline with no agents.
tags: [routing, negative]
runs: 3
max_turns: 6
allowed_tools: [Agent, Read, Glob, Grep, Edit, Write]
env:
  EVAL_COCKPIT_ROUTING: "on"
---

Quick one, do it yourself: what version is set in package.json?
