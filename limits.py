"""Size and length limits for the self-hosted app.

The hosted edition read these from its billing package, which this distribution does not ship.
Each value can be overridden by an environment variable of the same name.
"""
import os


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError as e:
        raise ValueError(f"{name} must be an integer") from e


# Thumbnail Studio: longest source it accepts, and request size caps.
THUMBNAIL_MAX_SOURCE_MINUTES = _int("THUMBNAIL_MAX_SOURCE_MINUTES", 180)
THUMBNAIL_SOURCE_MINUTES_PER_CHARGED = _int("THUMBNAIL_SOURCE_MINUTES_PER_CHARGED", 10)
THUMBNAIL_TITLE_MAX_CHARS = _int("THUMBNAIL_TITLE_MAX_CHARS", 300)
THUMBNAIL_MESSAGE_MAX_CHARS = _int("THUMBNAIL_MESSAGE_MAX_CHARS", 2000)
THUMBNAIL_REFINES_PER_SESSION = _int("THUMBNAIL_REFINES_PER_SESSION", 40)
THUMBNAIL_CONVERSATION_MAX_TURNS = _int("THUMBNAIL_CONVERSATION_MAX_TURNS", 20)
DESCRIBE_TRANSCRIPT_MAX_CHARS = _int("DESCRIBE_TRANSCRIPT_MAX_CHARS", 200000)

# AI Shorts (SaaS video) request caps.
SAAS_DESCRIPTION_MAX_CHARS = _int("SAAS_DESCRIPTION_MAX_CHARS", 10000)
SAAS_MAX_SCRIPTS = _int("SAAS_MAX_SCRIPTS", 10)

# Minute accounting helpers still referenced by the (inactive) quota code paths.
TRANSCRIBE_MINUTES = 2
PARTIAL_MIN_MINUTES = 5
FIRST_VIDEO_MAX_MINUTES = 60
