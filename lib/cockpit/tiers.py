"""Tier metadata read from the agent files, the single source of truth.

The dashboard, doctor and tests use this, so changing a model or effort in
plugins/orchestrator/agents/*.md updates everything that shows it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[2] / "plugins" / "orchestrator" / "agents"
ORDER = ("scout", "routine-worker", "senior-engineer", "debugger", "architect")
GLYPHS = {"scout": "◇", "routine-worker": "▪", "senior-engineer": "◆", "debugger": "✦", "architect": "▲"}
LANES = {
    "scout": "lookups, maps, docs · read-only",
    "routine-worker": "clear specs: configs, docs, tests",
    "senior-engineer": "interacting parts, migrations, security",
    "debugger": "flaky bugs, races, incidents",
    "architect": "hard-to-undo decisions · read-only",
}
# agent `color` frontmatter -> (256-colour, 8-colour curses index)
COLOURS = {"cyan": (117, 6), "green": (150, 2), "blue": (111, 4), "red": (211, 1),
           "purple": (183, 5), "yellow": (223, 3), "orange": (216, 3), "pink": (218, 5)}
# Env vars that silently override every tier's model or effort.
OVERRIDE_VARS = ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_SUBAGENT_EFFORT",
                 "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE")


@dataclass(frozen=True)
class Tier:
    name: str
    model: str
    effort: str
    colour: str
    glyph: str
    lane: str

    @property
    def runs_on(self) -> str:
        return f"{self.model} · {self.effort}"


def frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "#")):
            fields[key.strip()] = value.strip()
    return fields


def load_tiers(agents_dir: Path = AGENTS_DIR) -> list[Tier]:
    tiers = []
    for path in sorted(agents_dir.glob("*.md")):
        try:
            fields = frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = fields.get("name") or path.stem
        tiers.append(Tier(
            name=name, model=fields.get("model", "inherit"), effort=fields.get("effort", "default"),
            colour=fields.get("color", "cyan"), glyph=GLYPHS.get(name, "●"),
            lane=LANES.get(name, (fields.get("description") or "")[:48]),
        ))
    rank = {name: i for i, name in enumerate(ORDER)}
    return sorted(tiers, key=lambda t: (rank.get(t.name, len(ORDER)), t.name))
