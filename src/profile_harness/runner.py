"""Read-only Codex structured-output invocation."""

from __future__ import annotations

from pathlib import Path
import os

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
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        str(command),
        "exec",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--sandbox",
        "read-only",
        "--output-schema",
        str(Path(schema_path).resolve()),
        "-o",
        str(output),
        "-",
    ]
    run_bounded_process(
        arguments,
        cwd=root,
        input_bytes=prompt.encode("utf-8"),
        timeout=timeout,
        max_output_bytes=MAX_CODEX_OUTPUT_BYTES,
        environment={**os.environ, "PROFILE_HARNESS_CURATOR": "1"},
    )
    if not output.is_file():
        raise RuntimeError("codex completed without producing the result file")
    return output
