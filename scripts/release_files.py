"""The files a claude-cockpit release reads and rewrites. Pure functions, stdlib only.

Used by release.py; nothing here runs a command or touches the network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

CASK = Path("Casks") / "claude-cockpit.rb"
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?[ \t]*$", re.M)
JSON_VERSION = re.compile(r'("version"\s*:\s*")([^"]*)(")')
INIT_VERSION = re.compile(r'^(VERSION = ")([^"]*)(")$', re.M)
POLICY_TITLE = re.compile(r"^(# Orchestrator routing \(claude-cockpit policy v)(\d+\.\d+)(\))$", re.M)
CASK_VERSION = re.compile(r'^([ \t]*version ")([^"]*)(")$', re.M)
CASK_SHA = re.compile(r'^([ \t]*sha256 ")([0-9a-f]{64})(")$', re.M)
SHA256 = re.compile(r"\b[0-9a-f]{64}\b")


class ReleaseError(Exception):
    """A problem that stops a release, with a message fit for the terminal."""


# ---------------------------------------------------------------- versions

def parse_semver(text: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(text.strip())
    if not match:
        raise ReleaseError(f"{text!r} is not a version like 1.2.3")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_newer(candidate: str, current: str) -> bool:
    return parse_semver(candidate) > parse_semver(current)


def major_minor(version: str) -> str:
    major, minor, _ = parse_semver(version)
    return f"{major}.{minor}"


def replace_one(pattern: re.Pattern, text: str, value: str, what: str) -> str:
    """Replace the middle group of PATTERN's single match with VALUE."""
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ReleaseError(f"expected exactly one {what}, found {len(matches)}")
    return pattern.sub(lambda m: m.group(1) + value + m.group(3), text, count=1)


def bump_json(text: str, version: str, what: str) -> str:
    bumped = replace_one(JSON_VERSION, text, version, f'"version" in {what}')
    json.loads(bumped)  # still valid JSON
    return bumped


def bump_init(text: str, version: str) -> str:
    return replace_one(INIT_VERSION, text, version, "VERSION line in lib/cockpit/__init__.py")


def bump_policy_title(text: str, version: str) -> str:
    return replace_one(POLICY_TITLE, text, major_minor(version), "routing-policy title")


def bump_cask(text: str, version: str, sha256: str) -> str:
    """Set the cask's version and sha256 (the all-zero placeholder included)."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ReleaseError(f"{sha256!r} is not a sha256")
    return replace_one(CASK_SHA, replace_one(CASK_VERSION, text, version, "cask version"), sha256, "cask sha256")


def cask_state(text: str) -> tuple[str | None, str | None]:
    """(version, sha256) as the cask declares them."""
    version, sha = CASK_VERSION.search(text), CASK_SHA.search(text)
    return (version.group(2) if version else None, sha.group(2) if sha else None)


def asset_names(version: str) -> tuple[str, str]:
    tarball = f"claude-cockpit-{version}.tar.gz"
    return tarball, f"{tarball}.sha256"


# ---------------------------------------------------------------- CHANGELOG

def changelog_sections(text: str) -> list[tuple[str, str | None, str]]:
    """[(name, date, body)] in file order; name is "Unreleased" or a version."""
    heads = list(HEADING.finditer(text))
    stray = [line for line in re.findall(r"^## .*$", text, re.M) if not HEADING.fullmatch(line)]
    if stray:
        raise ReleaseError(f"CHANGELOG heading not in release format: {stray[0]!r}")
    sections = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        sections.append((head.group("name"), head.group("date"), text[head.end():end].strip("\n")))
    return sections


def changelog_section(text: str, version: str) -> str | None:
    """VERSION's notes, without its heading. None when there is no such section."""
    found = [body for name, _, body in changelog_sections(text) if name == version]
    return found[0].strip() if found else None


def unreleased_body(text: str) -> str | None:
    found = [body for name, _, body in changelog_sections(text) if name == "Unreleased"]
    return found[0].strip() if found else None


def release_changelog(text: str, version: str, day: str) -> str:
    """Turn [Unreleased] into [VERSION] - DAY and open a fresh, empty [Unreleased]."""
    parse_semver(version)
    if changelog_section(text, version) is not None:
        raise ReleaseError(f"CHANGELOG already has a [{version}] section")
    if not unreleased_body(text):
        raise ReleaseError("CHANGELOG [Unreleased] has no entries; add what changed before releasing")
    heading = re.compile(r"^## \[Unreleased\][ \t]*$", re.M)
    return heading.sub(f"## [Unreleased]\n\n## [{version}] - {day}", text, count=1)


# ---------------------------------------------------------------- the repository

@dataclass(frozen=True)
class Repo:
    root: Path

    @property
    def plugin_json(self) -> Path:
        return self.root / "plugins" / "orchestrator" / ".claude-plugin" / "plugin.json"

    @property
    def marketplace_json(self) -> Path:
        return self.root / ".claude-plugin" / "marketplace.json"

    @property
    def init_py(self) -> Path:
        return self.root / "lib" / "cockpit" / "__init__.py"

    @property
    def policy(self) -> Path:
        return self.root / "plugins" / "orchestrator" / "hooks" / "routing-policy.md"

    @property
    def changelog(self) -> Path:
        return self.root / "CHANGELOG.md"

    def versions(self) -> dict[str, str]:
        """Every place the release version lives (the policy title carries major.minor)."""
        plugin = json.loads(self.plugin_json.read_text(encoding="utf-8"))
        market = json.loads(self.marketplace_json.read_text(encoding="utf-8"))
        entry = next((p for p in market.get("plugins", []) if p.get("name") == "orchestrator"), {})
        init = INIT_VERSION.search(self.init_py.read_text(encoding="utf-8"))
        title = POLICY_TITLE.search(self.policy.read_text(encoding="utf-8"))
        return {
            "plugins/orchestrator/.claude-plugin/plugin.json": str(plugin.get("version")),
            ".claude-plugin/marketplace.json": str(entry.get("version")),
            "lib/cockpit/__init__.py": init.group(2) if init else "missing",
            "routing-policy.md title (major.minor)": title.group(2) if title else "missing",
        }

    def version_problems(self, expected: str) -> list[str]:
        problems = []
        for where, found in self.versions().items():
            want = major_minor(expected) if "major.minor" in where else expected
            if found != want:
                problems.append(f"{where} says {found}, expected {want}")
        return problems

    def current_version(self) -> str:
        found = self.versions()
        exact = {v for k, v in found.items() if "major.minor" not in k}
        if len(exact) != 1:
            raise ReleaseError("version fields disagree: " + ", ".join(f"{k}={v}" for k, v in found.items()))
        return exact.pop()

    def bumped_files(self, version: str, day: str) -> dict[Path, str]:
        """New contents for every file the bump changes (nothing is written here)."""
        current = self.current_version()
        changes = {
            self.plugin_json: bump_json(self.plugin_json.read_text(encoding="utf-8"), version, "plugin.json"),
            self.marketplace_json: bump_json(self.marketplace_json.read_text(encoding="utf-8"), version,
                                             "marketplace.json"),
            self.init_py: bump_init(self.init_py.read_text(encoding="utf-8"), version),
            self.changelog: release_changelog(self.changelog.read_text(encoding="utf-8"), version, day),
        }
        if major_minor(version) != major_minor(current):
            changes[self.policy] = bump_policy_title(self.policy.read_text(encoding="utf-8"), version)
        return changes
