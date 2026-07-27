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

Known blind spot: a repo that drives a model by shelling out to a CLI declares
no SDK, so it reads as "not an LLM project" — this bot itself is the example.
Closing that would mean grepping source for subprocess invocations, which is
exactly the fuzzy content search this module exists to avoid. Detection stays
manifest-based and conservative; the cost of a miss is one review lens.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Substrings that identify a model-provider SDK in a dependency manifest.
# Written lowercase with hyphens; matching is case-insensitive and treats
# -, _ and . as interchangeable (see _marker_pattern), so each entry only
# needs one spelling. Entries that are a substring of another are omitted:
# "generativeai" already covers google-generativeai and google.generativeai.
_LLM_MARKERS: tuple[str, ...] = (
    "anthropic",           # anthropic, @anthropic-ai/sdk, Anthropic.SDK
    "claude-agent-sdk", "claude-code-sdk",   # PyPI names carrying no "anthropic"
    "openai",              # openai, @openai/..., Azure.AI.OpenAI
    "langchain",
    "llamaindex", "llama-index",   # genuinely two spellings, not a separator swap
    "generativeai",
    "@google/genai", "google-genai",
    "mistralai",
    "cohere",
    "ollama",
    "litellm",
    "@ai-sdk/", "vercel/ai",
)

# Package ecosystems treat these as the same character — PEP 503 says so
# outright for Python, and npm/NuGet names vary the same way in practice. A
# marker written one way must match all of them, or `llama-index` silently
# misses the `llama_index` spelling that appears in half of real manifests.
_SEPARATORS = "-_."


def _marker_pattern(marker: str) -> str:
    """Marker regex that won't fire on a longer word that merely starts with it.

    Plain substring matching reads "coherence" as the Cohere SDK. Markers that
    end in a letter therefore may not be followed by a LOWERCASE letter;
    markers ending in punctuation (``@ai-sdk/``) get no such rule, since a
    package name follows.

    Lowercase specifically, not "any letter", because .NET and JS packages are
    camel-cased: ``OllamaSharp`` is a genuine Ollama client and ``Coherence``
    is not a Cohere one, and the capital is the only thing distinguishing a
    compound package name from an English word that happens to start the same
    way. This is why the manifest text is matched case-insensitively rather
    than lowercased first — lowercasing would destroy the evidence.
    """
    pattern = "".join(
        f"[{re.escape(_SEPARATORS)}]" if ch in _SEPARATORS else re.escape(ch)
        for ch in marker
    )
    if marker[-1].isalpha():
        # (?-i:...) turns IGNORECASE OFF inside the lookahead. Without it the
        # whole point is lost: under IGNORECASE, [a-z] also matches A-Z, so the
        # rule would read "not followed by any letter" and reject OllamaSharp
        # exactly like coherence.
        pattern += r"(?!(?-i:[a-z]))"
    return pattern


# Empty entries are filtered out, not tolerated: one would match every file
# and silently turn detection on everywhere — and would crash this module at
# import time, taking the whole bot down with it.
_MARKER_RE = re.compile(
    "|".join(_marker_pattern(m) for m in _LLM_MARKERS if m), re.IGNORECASE
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
# Callers pass the registered repo path, which is stable per repo — never a
# build worktree path, which would make this grow once per build forever.
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
    resolved = False
    try:
        root = Path(repo_path)
        resolved = root.is_dir()
        if resolved:
            for pattern in _MANIFEST_GLOBS:
                if verdict:
                    break
                for manifest in root.glob(pattern):
                    if not manifest.is_file():
                        continue
                    try:
                        # NOT lowercased — see _marker_pattern: the capital in
                        # "OllamaSharp" is what tells it apart from "coherence".
                        text = manifest.read_text(
                            encoding="utf-8", errors="ignore",
                        )
                    except OSError:
                        continue
                    if _MARKER_RE.search(text):
                        log.debug(
                            "LLM project detected: %s matched in %s",
                            repo_path, manifest.name,
                        )
                        verdict = True
                        break
    except Exception:
        log.debug("LLM-project detection failed for %s", repo_path, exc_info=True)
        verdict = False
        resolved = False

    # Only memoise a verdict we actually reached. Caching the "couldn't read
    # the repo" answer would make one transient failure (a detached drive, a
    # path not yet checked out) permanent for the life of the process.
    if resolved:
        _cache[repo_path] = verdict
    return verdict


def reset_cache() -> None:
    """Clear the memoised verdicts (tests, and after dependency changes)."""
    _cache.clear()
