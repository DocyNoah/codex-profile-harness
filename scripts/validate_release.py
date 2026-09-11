#!/usr/bin/env python3
"""Dependency-free public release validation and generated artifact smoke test."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.packaging import (  # noqa: E402
    PACKAGED_FILES,
    RELEASE_FILES,
    build_local_marketplace,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-(?:(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
    re.ASCII,
)
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def validate_manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("plugin manifest must be readable JSON") from error
    require(isinstance(value, dict), "plugin manifest must be an object")
    allowed = {
        "id", "name", "version", "description", "skills", "apps", "mcpServers",
        "interface", "author", "homepage", "repository", "license", "keywords",
        "hooks",
    }
    require(set(value) <= allowed, "plugin manifest contains unknown fields")
    name = value.get("name")
    version = value.get("version")
    description = value.get("description")
    require(isinstance(name, str) and _IDENTIFIER.fullmatch(name) is not None, "invalid plugin name")
    require(isinstance(version, str) and _SEMVER.fullmatch(version) is not None, "invalid plugin version")
    require(isinstance(description, str) and bool(description.strip()), "invalid plugin description")
    author = value.get("author")
    require(isinstance(author, dict) and isinstance(author.get("name"), str) and bool(author["name"].strip()), "invalid plugin author")
    require(set(author) <= {"name", "email", "url"}, "plugin author contains unknown fields")
    interface = value.get("interface")
    require(isinstance(interface, dict), "missing plugin interface")
    interface_allowed = {
        "displayName", "shortDescription", "longDescription", "developerName",
        "category", "capabilities", "websiteURL", "privacyPolicyURL",
        "termsOfServiceURL", "brandColor", "composerIcon", "logo", "logoDark",
        "screenshots", "defaultPrompt", "default_prompt",
    }
    require(set(interface) <= interface_allowed, "plugin interface contains unknown fields")
    for field in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
        require(isinstance(interface.get(field), str) and bool(interface[field].strip()), f"invalid interface.{field}")
    capabilities = interface.get("capabilities")
    require(isinstance(capabilities, list) and all(isinstance(item, str) and item.strip() for item in capabilities), "invalid interface capabilities")
    prompt = interface.get("defaultPrompt", interface.get("default_prompt"))
    require(
        (isinstance(prompt, str) and bool(prompt.strip()))
        or (isinstance(prompt, list) and bool(prompt) and all(isinstance(item, str) and item.strip() for item in prompt)),
        "missing interface default prompt",
    )
    for field in ("homepage", "repository"):
        item = value.get(field)
        require(isinstance(item, str) and item.startswith("https://"), f"invalid {field}")
    require(value.get("license") == "MIT", "invalid plugin license")
    require(value.get("hooks") == "./hooks/hooks.json", "invalid plugin hooks path")
    keywords = value.get("keywords")
    require(isinstance(keywords, list) and all(isinstance(item, str) and item.strip() for item in keywords), "invalid plugin keywords")
    return value


def _frontmatter_scalar(raw: str) -> str:
    value = raw.strip()
    require(bool(value), "skill frontmatter values must not be empty")
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("invalid quoted skill value") from error
        require(isinstance(decoded, str), "skill frontmatter values must be strings")
        value = decoded
    elif value.startswith("'"):
        require(len(value) >= 2 and value[-1] == value[0], "invalid quoted skill value")
        value = value[1:-1].replace("''", "'")
    else:
        require(value[0] not in "[{*&!|>", "skill frontmatter values must be strings")
        require(value.lower() not in {"true", "false", "null", "~"}, "skill frontmatter values must be strings")
    require("\n" not in value and "\r" not in value, "invalid skill value")
    return value


def validate_skill(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("skill must be readable UTF-8") from error
    match = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, re.DOTALL)
    require(match is not None, "skill must have YAML frontmatter")
    values: dict[str, str] = {}
    assert match is not None
    for raw_line in match.group(1).splitlines():
        require(bool(raw_line.strip()) and not raw_line[:1].isspace(), "unsupported skill YAML structure")
        require(":" in raw_line, "invalid skill YAML mapping")
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        require(key not in values, "duplicate skill frontmatter key")
        require(key in {"name", "description", "license", "allowed-tools"}, "unknown skill frontmatter key")
        values[key] = _frontmatter_scalar(raw_value)
    require(set(values) >= {"name", "description"}, "skill name and description are required")
    require(_IDENTIFIER.fullmatch(values["name"]) is not None and len(values["name"]) <= 64, "invalid skill name")
    require(len(values["description"]) <= 1024 and "<" not in values["description"] and ">" not in values["description"], "invalid skill description")
    marker = chr(91) + "TO" + "DO:"
    require(marker not in text, "skill contains unfinished placeholder")
    return values


def _workflow_job(text: str, name: str) -> str:
    marker = f"  {name}:\n"
    start = text.find(marker)
    require(start >= 0, f"release workflow is missing the {name} job")
    start += len(marker)
    match = re.search(r"(?m)^  [A-Za-z0-9_-]+:\s*$", text[start:])
    end = len(text) if match is None else start + match.start()
    return text[start:end]


def validate_release_workflow(path: Path) -> None:
    """Validate least-privilege release semantics without a YAML dependency."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("release workflow must be readable UTF-8") from error
    jobs_at = text.find("\njobs:\n")
    require(jobs_at >= 0, "release workflow jobs are missing")
    header = text[:jobs_at]
    require(
        re.search(r"(?m)^permissions:\n  contents: read$", header) is not None,
        "release workflow top-level permissions must be contents: read",
    )
    require("contents: write" not in header, "release workflow top-level write is forbidden")
    validate = _workflow_job(text, "validate")
    publish = _workflow_job(text, "publish")
    require("contents: write" not in validate, "release validation job must remain read-only")
    require(
        re.search(r"(?m)^    permissions:\n      contents: write$", publish) is not None,
        "release publish job alone must grant contents: write",
    )
    uses = re.findall(r"(?m)^      - uses: ([^\s]+)", publish)
    require(
        all(item == "actions/download-artifact@v4" for item in uses),
        "release publish job may use only the trusted artifact download boundary",
    )
    require("GH_TOKEN: ${{ github.token }}" in publish, "release publish token binding is missing")
    require("gh release create" in publish, "release publication must use the GitHub CLI")
    require("$GITHUB_REF_NAME" in publish, "release publication must bind the triggering tag")
    require("--verify-tag" in publish, "release publication must verify the tag")
    require(
        '--repo "$GITHUB_REPOSITORY"' in publish,
        "release publication must bind explicit repository context",
    )
    require(
        'archive="dist/codex-profile-harness-${version}.tar.gz"' in publish
        and 'checksum="${archive}.sha256"' in publish,
        "release publication must name the exact versioned assets",
    )


def validate_source() -> None:
    manifest = validate_manifest(ROOT / ".codex-plugin/plugin.json")
    require(manifest.get("name") == "codex-profile-harness", "invalid plugin name")
    require(manifest.get("version") == "0.4.1", "invalid plugin version")
    skill = validate_skill(ROOT / "skills/profile-harness/SKILL.md")
    require(skill["name"] == "profile-harness", "invalid skill name")
    validate_release_workflow(ROOT / ".github/workflows/release.yml")
    for relative in RELEASE_FILES:
        path = ROOT / relative
        require(path.is_file() and not path.is_symlink(), f"missing or unsafe release file: {relative}")
    combined = "\n".join(
        (ROOT / name).read_text(encoding="utf-8").lower()
        for name in ("README.md", "INSTALL.md", "INSTALL_AGENT.md", "SECURITY.md", "CHANGELOG.md")
    )
    for phrase in (
        "agent-assisted", "does not promise a universal installer",
        "automatic_apply = false", "automatic_apply = true",
        "legacy markdown", "sha-256", "clean extraction",
    ):
        require(phrase in combined, f"public documentation is missing: {phrase}")


def validate_generated() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        parent = Path(temporary_directory)
        marketplace = parent / "marketplace"
        build_local_marketplace(ROOT, marketplace)
        plugin = marketplace / "plugins/codex-profile-harness"
        actual = {
            path.relative_to(plugin).as_posix()
            for path in plugin.rglob("*")
            if path.is_file()
        }
        require(actual == set(PACKAGED_FILES), "generated artifact differs from allowlist")
        forbidden = {".git", "tests", "profiles", "__pycache__", ".superpowers"}
        require(
            all(forbidden.isdisjoint(Path(item).parts) for item in actual),
            "generated artifact contains forbidden paths",
        )
        profile = parent / "profile"
        cli = plugin / "bin/profile-harness"
        initialized = subprocess.run(
            [sys.executable, str(cli), "init", str(profile), "--name", "CI"],
            text=True, capture_output=True, check=False,
        )
        require(initialized.returncode == 0, initialized.stderr)
        for arguments in (("maintain",), ("dashboard",), ("doctor",), ("git", "status")):
            completed = subprocess.run(
                [sys.executable, str(cli), *arguments], cwd=profile,
                text=True, capture_output=True, check=False,
            )
            require(completed.returncode == 0, completed.stdout + completed.stderr)


def main() -> int:
    if len(sys.argv) > 2:
        raise ValueError("usage: validate_release.py [RELEASE_ROOT]")
    if len(sys.argv) == 2 and Path(sys.argv[1]).expanduser().resolve() != ROOT:
        raise ValueError("validator must execute from the release root it validates")
    validate_source()
    validate_generated()
    print("release validation: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
