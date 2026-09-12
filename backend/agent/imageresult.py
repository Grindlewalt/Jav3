"""Out-of-band channel for a tool result that carries a rendered image.

A vision-capable tool (e.g. a browser screenshot) writes a PNG in the guest and
returns its normal text result with the PNG's path appended after a sentinel.
`run_turn` splits the sentinel off, keeps the text as the tool result, and
re-attaches the image to the model as an image block in a FOLLOWING user message
— DeepSeek accepts image content only in user messages, not tool messages
(verified: a role:"tool" image body is rejected, a user one is accepted).

The image BYTES never ride in the result string — only a filesystem path — so
tool_result events, the DB tool_calls ledger, and eviction stubs all stay small.
"""

# Control chars that will not appear in an ordinary tool result.
MARKER = "\x00\x01JARVIS_IMG\x01\x00"


def with_image(text: str, image_path: str) -> str:
    """A tool handler's return value that carries a screenshot at `image_path`."""
    return f"{text}{MARKER}{image_path}"


def split(result: str) -> tuple[str, str | None]:
    """(text, image_path or None) — inverse of with_image; a plain string passes
    through unchanged with image_path None."""
    if MARKER in result:
        text, _, path = result.partition(MARKER)
        return text, (path.strip() or None)
    return result, None
