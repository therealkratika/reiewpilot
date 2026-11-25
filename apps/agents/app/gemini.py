import logging
import time
from collections.abc import Callable
from typing import Any

from google.genai.errors import APIError

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.5

logger = logging.getLogger("reviewpilot.gemini")


def _is_retryable(exc: Exception) -> bool:
    return (
        isinstance(exc, APIError)
        and isinstance(exc.code, int)
        and (exc.code in (408, 429) or 500 <= exc.code < 600)
    )


def generate_content_with_retries(
    generate_content: Callable[..., Any],
    **kwargs: Any,
) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return generate_content(**kwargs)
        except Exception as exc:
            if not _is_retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                raise

            delay = BASE_BACKOFF_SECONDS * (2**attempt)
            logger.warning(
                "Gemini request failed with retryable status %s; retrying in %.1f seconds (attempt %d/%d).",
                exc.code,
                delay,
                attempt + 2,
                MAX_ATTEMPTS,
            )
            time.sleep(delay)

    raise RuntimeError("Gemini request attempts were exhausted.")
