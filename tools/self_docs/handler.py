"""self_docs: serve docs/SELF.md (whole, one section, or the section list)."""
import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / "docs" / "SELF.md"
_SECTION = re.compile(r"^## +(.+?) *$", re.M)


async def run(section: str = "") -> str:
    try:
        text = DOC.read_text()
    except OSError:
        return "error: docs/SELF.md is missing from this deployment"
    names = _SECTION.findall(text)
    want = (section or "").strip().lower()
    if not want:
        listing = "\n".join(f"- {n}" for n in names)
        return ("Sections (call again with section=<name>):\n" + listing)
    if want == "all":
        return text
    for m in _SECTION.finditer(text):
        if m.group(1).strip().lower() == want:
            start = m.start()
            nxt = _SECTION.search(text, m.end())
            return text[start:nxt.start() if nxt else len(text)].strip()
    return (f"no section '{section}'. Available: " + ", ".join(names))
