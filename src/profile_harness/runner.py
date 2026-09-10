"""Read-only Codex structured-output invocation."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess

from .config import PLUGIN_ROOT


RESULT_SCHEMA = PLUGIN_ROOT / "schemas/curation-result.schema.json"


def run_codex(
    profile_root: Path,
    prompt_path: Path,
    output_path: Path,
    *,
    command: str = "codex",
    timeout: float = 300,
) -> Path:
    """Run Codex with a prompt on stdin, never exposing prompt data in argv."""
    root = Path(profile_root).resolve()
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        str(command),
        "exec",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(RESULT_SCHEMA.resolve()),
        "-o",
        str(output),
        "-",
    ]
    subprocess.run(
        arguments,
        cwd=root,
        input=prompt,
        text=True,
        check=True,
        timeout=timeout,
        env={**os.environ, "PROFILE_HARNESS_CURATOR": "1"},
    )
    if not output.is_file():
        raise RuntimeError("codex completed without producing the result file")
    return output
