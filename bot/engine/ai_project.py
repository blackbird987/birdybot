"""Detect repos that call an LLM, so the code-review step can apply an extra lens.

The standards we hold LLM-shaped code to (does anything assert on output
quality? is model output validated or regex'd out of prose? can a summary
field drift from the raw records it claims to describe?) currently live as
prose in a global instructions file — they apply only when a session happens
to read it. This module is the detection half of enforcing them from the
harness instead: cheap, read-only, and correct to skip when unsure.

Deliberately shallow: it reads dependency manifests at the repo root only. A
recursive content grep would be both slower and more prone to false positives
(a README mentioning "openai" is not an LLM project).
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Substrings that identify a model-provider SDK in a dependency manifest.
# Lowercase — manifests are lowercased before matching.
_LLM_MARKERS: tuple[str, ...] = (
    "anthropic",           # anthropic, @anthropic-ai/sdk, Anthropic.SDK
    "openai",              # openai, @openai/..., Azure.AI.OpenAI
    "langchain",
    "llamaindex", "llama-index",
    "google-generativeai", "google.generativeai", "generativeai",
    "@google/genai", "google-genai",
    "mistralai",
    "cohere",
    "ollama",
    "@ai-sdk/", "vercel/ai",
)

# Manifests checked at the repo root. Globs are resolved non-recursively.
_MANIFEST_GLOBS: tuple[str, ...] = (
    "requirements.txt",
    "requirements-*.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "*.csproj",
    "Cargo.toml",
    "go.mod",
)

# repo_path -> verdict, for the life of the process. Dependencies change on a
# scale of days; re-reading manifests on every review round would be waste.
_cache: dict[str, bool] = {}


def is_llm_project(repo_path: str | None) -> bool:
    """True when the repo's root manifests declare a model-provider SDK.

    Never raises — an unreadable repo is reported as "not an LLM project" so
    a detection failure can only ever cost us the extra lens, never a crash
    or a spurious review of an unrelated codebase.
    """
    if not repo_path:
        return False
    cached = _cache.get(repo_path)
    if cached is not None:
        return cached

    verdict = False
    try:
        root = Path(repo_path)
        if root.is_dir():
            for pattern in _MANIFEST_GLOBS:
                if verdict:
                    break
                for manifest in root.glob(pattern):
                    if not manifest.is_file():
                        continue
                    try:
                        text = manifest.read_text(
                            encoding="utf-8", errors="ignore",
                        ).lower()
                    except OSError:
                        continue
                    if any(marker in text for marker in _LLM_MARKERS):
                        log.debug(
                            "LLM project detected: %s matched in %s",
                            repo_path, manifest.name,
                        )
                        verdict = True
                        break
    except Exception:
        log.debug("LLM-project detection failed for %s", repo_path, exc_info=True)
        verdict = False

    _cache[repo_path] = verdict
    return verdict


def reset_cache() -> None:
    """Clear the memoised verdicts (tests, and after dependency changes)."""
    _cache.clear()
