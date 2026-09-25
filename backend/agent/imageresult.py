"""Out-of-band channel for a tool result that carries a rendered image.

A vision-capable tool returns its normal text result with an image descriptor
appended after a sentinel. `run_turn` splits the sentinel off, keeps the text
as the tool result, and re-attaches the image to the model as an image block in
a FOLLOWING user message — DeepSeek accepts image content only in user
messages, not tool messages (verified: a role:"tool" image body is rejected, a
user one is accepted).

The image is either a PATH (a tool that runs where the loop runs and wrote a
file there) or INLINE bytes (base64 + mime). Inline exists because the loop runs
in the guest and a host-brokered tool's file path means nothing there: the
broker peels the image off the host result (`split`) and ships it beside the
text in the `broker_result` frame, and the guest registry re-attaches it with
`with_inline`. Either way the bytes are peeled off BEFORE the result is
persisted, so tool_result events, the DB tool_calls ledger and eviction stubs
stay text-only.
"""
import base64
import binascii
import json
from dataclasses import dataclass

# Control chars that will not appear in an ordinary tool result.
MARKER = "\x00\x01JARVIS_IMG\x01\x00"

# what the model endpoint takes; anything else is dropped rather than sent
MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def sniff(data: bytes) -> str | None:
    """The image type from its magic bytes, or None. The declared mime is never
    trusted: a desk client (or a bug) labelling JPEG bytes image/png made the
    data URL lie, and a non-image labelled image/* must not reach the model."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


@dataclass
class Image:
    path: str | None = None
    b64: str | None = None
    mime: str | None = None
    caption: str | None = None

    def data(self, cap: int) -> bytes | None:
        """The bytes, or None if missing, unreadable, undecodable or over
        `cap` — a bad image never breaks the turn, it is just not shown."""
        if self.b64 is not None:
            if len(self.b64) > cap * 4 // 3 + 8:
                return None
            try:
                data = base64.b64decode(self.b64, validate=True)
            except (binascii.Error, ValueError):
                return None
        elif self.path:
            try:
                with open(self.path, "rb") as f:
                    data = f.read(cap + 1)
            except OSError:
                return None
        else:
            return None
        if not data or len(data) > cap:
            return None
        return data

    def wire(self, cap: int) -> dict | None:
        """{"b64", "mime", "caption"} with the bytes inline — what the broker
        sends to the guest. None if there is nothing showable."""
        data = self.data(cap)
        mime = sniff(data) if data else None
        if mime is None:
            return None
        return {"b64": self.b64 if self.b64 is not None else
                base64.b64encode(data).decode(), "mime": mime,
                "caption": (self.caption or "")[:300] or None}


def with_image(text: str, image_path: str, *, caption: str | None = None) -> str:
    """A tool result that carries an image file at `image_path`."""
    return f"{text}{MARKER}" + json.dumps({"path": image_path, "caption": caption})


def with_inline(text: str, data: bytes | None = None, *, b64: str | None = None,
                mime: str | None = None, caption: str | None = None) -> str:
    """A tool result that carries the image bytes themselves (or their base64)."""
    if b64 is None:
        b64 = base64.b64encode(data or b"").decode()
    return f"{text}{MARKER}" + json.dumps({"b64": b64, "mime": mime,
                                           "caption": caption})


def split(result: str) -> tuple[str, Image | None]:
    """(text, Image or None) — inverse of with_image/with_inline; a plain string
    passes through unchanged with None. A bare path after the marker (the old
    form) still reads as a path."""
    if MARKER not in result:
        return result, None
    text, _, tail = result.partition(MARKER)
    tail = tail.strip()
    if not tail:
        return text, None
    if not tail.startswith("{"):
        return text, Image(path=tail)
    try:
        d = json.loads(tail)
    except ValueError:
        return text, None
    if not isinstance(d, dict):
        return text, None
    s = lambda k: d[k] if isinstance(d.get(k), str) else None   # noqa: E731
    img = Image(path=s("path"), b64=s("b64"), mime=s("mime"), caption=s("caption"))
    return text, (img if (img.path or img.b64) else None)
