from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from .models import (
    DEFAULT_SEQUENCE_PROMPT_FILE,
    DEFAULT_SINGLE_PROMPT_FILE,
    ConfigError,
    LooperConfig,
    LooperMode,
    PromptError,
)


def load_prompts(path: Path, separator: str) -> list[str]:
    if not path.exists():
        raise PromptError(f"prompt file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"failed to read prompt file {path}: {exc}") from exc
    try:
        parts = [part.strip() for part in re.split(separator, text, flags=re.MULTILINE)]
    except re.error as exc:
        raise PromptError(f"invalid prompt separator regex {separator!r}: {exc}") from exc
    prompts = [part for part in parts if part]
    if not prompts:
        raise PromptError(f"no prompts found in {path}")
    return prompts


def load_prompts_for_mode(path: Path, separator: str, mode: LooperMode) -> list[str]:
    if mode == "sequence":
        return load_prompts(path, separator)
    if mode != "single":
        raise ConfigError(f"unsupported looper mode: {mode}")
    if not path.exists():
        raise PromptError(f"prompt file not found: {path}")
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptError(f"failed to read prompt file {path}: {exc}") from exc
    if not prompt:
        raise PromptError(f"no prompts found in {path}")
    return [prompt]


def _path_exists_from(cwd: Path, path: Path) -> bool:
    return path.exists() if path.is_absolute() else (cwd / path).exists()


def resolve_prompt_defaults(looper: LooperConfig, *, cwd: Path) -> LooperConfig:
    prompt_file_is_default = looper.prompt_file == DEFAULT_SINGLE_PROMPT_FILE
    prompt_file_explicit = looper.prompt_file_explicit or not prompt_file_is_default
    mode = looper.mode
    prompt_file = looper.prompt_file

    if not looper.mode_explicit:
        if prompt_file_explicit:
            mode = "sequence" if prompt_file == DEFAULT_SEQUENCE_PROMPT_FILE else "single"
        elif not _path_exists_from(cwd, DEFAULT_SINGLE_PROMPT_FILE) and _path_exists_from(
            cwd, DEFAULT_SEQUENCE_PROMPT_FILE
        ):
            mode = "sequence"
            prompt_file = DEFAULT_SEQUENCE_PROMPT_FILE
        else:
            mode = "single"
            prompt_file = DEFAULT_SINGLE_PROMPT_FILE
    elif not prompt_file_explicit:
        prompt_file = (
            DEFAULT_SEQUENCE_PROMPT_FILE if mode == "sequence" else DEFAULT_SINGLE_PROMPT_FILE
        )

    return replace(looper, mode=mode, prompt_file=prompt_file)
