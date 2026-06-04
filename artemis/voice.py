"""Voice endpoint — Deepgram STT + ElevenLabs TTS with OpenAI fallbacks."""

import logging

import requests

from artemis import config
from artemis.briefs import handle_mention
from knowledge.secrets import (
    get_anthropic_key,
    get_deepgram_api_key,
    get_elevenlabs_api_key,
)

logger = logging.getLogger(__name__)

# ElevenLabs "Rachel" default voice
_ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


# ---------------------------------------------------------------------------
# STT — Deepgram Nova-2  (fallback: OpenAI Whisper)
# ---------------------------------------------------------------------------


def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    """Transcribe audio bytes to text via Deepgram Nova-2.

    Falls back to OpenAI Whisper on failure.
    """
    try:
        return _transcribe_deepgram(audio_bytes, mime_type)
    except Exception:
        logger.warning("Deepgram STT failed, falling back to Whisper", exc_info=True)
    try:
        return _transcribe_whisper(audio_bytes, mime_type)
    except Exception:
        logger.exception("Whisper STT fallback also failed")
        raise


def _transcribe_deepgram(audio_bytes: bytes, mime_type: str) -> str:
    api_key = get_deepgram_api_key()
    resp = requests.post(
        "https://api.deepgram.com/v1/listen",
        params={"model": "nova-2", "smart_format": "true", "language": "en-US"},
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": mime_type,
        },
        data=audio_bytes,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    transcript = (
        data.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("transcript", "")
    )
    if not transcript:
        raise ValueError("Deepgram returned empty transcript")
    logger.info("Deepgram STT: %d chars", len(transcript))
    return transcript


def _transcribe_whisper(audio_bytes: bytes, mime_type: str) -> str:
    api_key = get_anthropic_key()  # OpenAI key would be separate — use Anthropic for now
    # If no OpenAI key, try to get it from secrets
    try:
        from knowledge.secrets import get_secret
        api_key = get_secret("rdmis/dev/openai-api-key")["api_key"]
    except Exception:
        raise RuntimeError("No OpenAI API key available for Whisper fallback")

    ext = "webm" if "webm" in mime_type else "m4a" if "m4a" in mime_type else "wav"
    resp = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (f"audio.{ext}", audio_bytes, mime_type)},
        data={"model": "whisper-1"},
        timeout=30,
    )
    resp.raise_for_status()
    transcript = resp.json().get("text", "")
    if not transcript:
        raise ValueError("Whisper returned empty transcript")
    logger.info("Whisper STT fallback: %d chars", len(transcript))
    return transcript


# ---------------------------------------------------------------------------
# TTS — ElevenLabs  (fallback: OpenAI TTS)
# ---------------------------------------------------------------------------


def synthesize_speech(text: str) -> bytes:
    """Synthesize text to MP3 audio bytes via ElevenLabs.

    Falls back to OpenAI TTS on failure.
    """
    try:
        return _synthesize_elevenlabs(text)
    except Exception:
        logger.warning("ElevenLabs TTS failed, falling back to OpenAI TTS", exc_info=True)
    try:
        return _synthesize_openai(text)
    except Exception:
        logger.exception("OpenAI TTS fallback also failed")
        raise


def _synthesize_elevenlabs(text: str) -> bytes:
    api_key = get_elevenlabs_api_key()
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{_ELEVENLABS_VOICE_ID}",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json={
            "text": text[:5000],  # ElevenLabs has a character limit
            "model_id": "eleven_turbo_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    audio = resp.content
    if len(audio) < 100:
        raise ValueError("ElevenLabs returned suspiciously small audio")
    logger.info("ElevenLabs TTS: %d bytes", len(audio))
    return audio


def _synthesize_openai(text: str) -> bytes:
    try:
        from knowledge.secrets import get_secret
        api_key = get_secret("rdmis/dev/openai-api-key")["api_key"]
    except Exception:
        raise RuntimeError("No OpenAI API key available for TTS fallback")

    resp = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "tts-1",
            "voice": "onyx",
            "input": text[:4096],
        },
        timeout=30,
    )
    resp.raise_for_status()
    audio = resp.content
    logger.info("OpenAI TTS fallback: %d bytes", len(audio))
    return audio


# ---------------------------------------------------------------------------
# Voice processing pipeline
# ---------------------------------------------------------------------------


def process_voice_query(
    audio_bytes: bytes,
    mime_type: str,
    mm_client=None,
    gmail_client=None,
    calendar_client=None,
) -> tuple[str, bytes]:
    """Full voice pipeline: STT → Artemis brain → TTS.

    Posts transcript + response to Mattermost.
    Returns (response_text, audio_bytes).
    """
    # 1. Transcribe
    transcript = transcribe_audio(audio_bytes, mime_type)
    logger.info("Voice transcript: %s", transcript[:200])

    # 2. Build minimal context and get AI response
    from datetime import datetime
    now = datetime.now()
    data_context = f"**Current time:** {now.strftime('%A')}, {now.strftime('%I:%M %p')}"

    # Add calendar context if available
    if calendar_client and calendar_client.service:
        try:
            events = calendar_client.get_todays_events()
            if events:
                data_context += "\n**Today's calendar:**"
                for ev in events[:5]:
                    data_context += f"\n- {ev.get('summary', '?')} at {ev.get('time', '?')}"
        except Exception:
            pass

    response_text = handle_mention(
        question=transcript,
        thread_context="[Voice query — no prior thread context]",
        data_context=data_context,
    )

    if not response_text:
        response_text = "I didn't catch that. Could you try again?"

    # 3. Post to Mattermost
    if mm_client:
        try:
            mm_client.post_message(
                config.CHANNEL_OPS,
                f"\U0001f3a4 **Voice query:**\n"
                f"> {transcript}\n\n"
                f"\U0001f4ac **Response:**\n"
                f"{response_text}",
            )
        except Exception:
            logger.exception("Failed to post voice exchange to Mattermost")

    # 4. Synthesize response audio
    audio_out = synthesize_speech(response_text)

    return response_text, audio_out
