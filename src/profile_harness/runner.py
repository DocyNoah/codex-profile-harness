"""Read-only Codex structured-output invocation."""

from __future__ import annotations

from pathlib import Path
import os
import tempfile

from .config import PLUGIN_ROOT
from .process import run_bounded_process


RESULT_SCHEMA = PLUGIN_ROOT / "schemas/curation-result.schema.json"
MAX_CODEX_OUTPUT_BYTES = 1024 * 1024


def run_codex(
    profile_root: Path,
    prompt_path: Path,
    output_path: Path,
    *,
    command: str = "codex",
    model: str = "gpt-5.6-sol",
    reasoning_effort: str = "medium",
    schema_path: Path = RESULT_SCHEMA,
    timeout: float = 300,
) -> Path:
    """Run Codex with a prompt on stdin, never exposing prompt data in argv."""
    root = Path(profile_root).resolve()
    configured_home = os.environ.get("CODEX_HOME")
    source_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    ).resolve()
    source_auth = source_home / "auth.json"
    if not source_auth.is_file():
        raise RuntimeError(f"Codex authentication file is unavailable: {source_auth}")
    resolved_auth = source_auth.resolve(strict=True)
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        str(command),
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--disable",
        "shell_tool",
        "--disable",
        "unified_exec",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        "project_doc_max_bytes=0",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(Path(schema_path).resolve()),
        "-o",
        str(output),
        "-",
    ]
    with tempfile.TemporaryDirectory(prefix="profile-harness-codex-home-") as directory:
        isolated_home = Path(directory).resolve()
        if isolated_home == root or isolated_home.is_relative_to(root):
            raise RuntimeError("isolated Codex home is inside the profile")
        (isolated_home / "auth.json").symlink_to(resolved_auth)
        run_bounded_process(
            arguments,
            cwd=root,
            input_bytes=prompt.encode("utf-8"),
            timeout=timeout,
            max_output_bytes=MAX_CODEX_OUTPUT_BYTES,
            environment={
                **os.environ,
                "PROFILE_HARNESS_CURATOR": "1",
                "CODEX_HOME": str(isolated_home),
            },
        )
    if not output.is_file():
        raise RuntimeError("codex completed without producing the result file")
    return output
