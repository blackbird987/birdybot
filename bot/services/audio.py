"""Speech-to-text transcription via OpenAI Whisper API."""

from __future__ import annotations

import logging

import httpx

from bot.config import OPENAI_API_KEY

log = logging.getLogger(__name__)


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe audio bytes to text using OpenAI Whisper.

    Returns the transcribed text, or raises on failure.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (filename, audio_bytes, "audio/ogg")},
            data={"model": "whisper-1"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["text"]
