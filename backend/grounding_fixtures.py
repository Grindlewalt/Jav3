"""The model finder's labelled screens (docs/navigation-contract.md C).

About two dozen synthetic desktop screenshots, rendered with Pillow from code
(seed 7, so every run and every machine draws the same pixels for a given
Pillow version), each with 3-6 targets: a description a model is asked to
find and the box a click must land in. They cover what makes grounding hard
on a real desk: light and dark themes, a 2560x1600 "Retina" capture shrunk to
1280x800 the way jav3-desk does, a dense toolbar of 16-px glyph icons, forms,
menus, a dialog over a page where BOTH say "Save" (the description says
which), tiny close buttons, and lists of near-identical rows.

    fixtures() -> [{png, w, h, name, targets: [{description, box: (x,y,w,h)}]}]
    score((x, y), box) -> (hit, px_error)

Pillow is optional: without it `fixtures()` raises FixturesUnavailable and the
probe reports "Pillow not installed"; `locate()` never needs this module.
"""
from __future__ import annotations

import io
import math
import random
import threading

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:          # pragma: no cover — exercised on a box without Pillow
    Image = ImageDraw = ImageFont = None
    HAVE_PIL = False

SEED = 7
W, H = 1280, 800
TOLERANCE = 2                # px a click may miss the box by and still hit


class FixturesUnavailable(RuntimeError):
    pass


def score(point: tuple[int, int] | None, box: tuple[int, int, int, int]) -> tuple[bool, float | None]:
    """(hit, pixel error). Hit = inside the box grown by TOLERANCE; the error
    is the distance to the box (0 inside). No point -> (False, None)."""
    if point is None:
        return False, None
    px, py = point
    x, y, w, h = box
    dx = max(x - px, 0, px - (x + w - 1))
    dy = max(y - py, 0, py - (y + h - 1))
    hit = dx <= TOLERANCE and dy <= TOLERANCE
    return hit, float(math.hypot(dx, dy))


# --- drawing -----------------------------------------------------------------

THEMES = {
    "light": {"bg": (243, 244, 246), "panel": (255, 255, 255), "bar": (229, 231, 235),
              "text": (17, 24, 39), "dim": (107, 114, 128), "border": (209, 213, 219),
              "button": (243, 244, 246), "accent": (37, 99, 235), "on_accent": (255, 255, 255),
              "danger": (220, 38, 38), "sel": (219, 234, 254), "shade": (0, 0, 0, 90)},
    "dark": {"bg": (24, 24, 27), "panel": (39, 39, 42), "bar": (32, 32, 36),
             "text": (228, 228, 231), "dim": (161, 161, 170), "border": (63, 63, 70),
             "button": (52, 52, 58), "accent": (96, 165, 250), "on_accent": (15, 23, 42),
             "danger": (248, 113, 113), "sel": (30, 58, 138), "shade": (0, 0, 0, 140)},
}

_fonts: dict[int, object] = {}


def _font(px: int):
    f = _fonts.get(px)
    if f is None:
        try:
            f = ImageFont.load_default(size=px)
        except TypeError:        # Pillow < 10.1: the bitmap font, one size
            f = ImageFont.load_default()
        _fonts[px] = f
    return f


class Screen:
    """A 1280x800 canvas in logical px. scale=2 draws a Retina-sized image and
    `render()` downscales it (LANCZOS, like sips/-resize on the client);
    target boxes stay in the logical 1280x800 space the model sees."""

    def __init__(self, name: str, theme: str = "light", scale: int = 1):
        self.name, self.t, self.s = name, THEMES[theme], scale
        self.img = Image.new("RGB", (W * scale, H * scale), self.t["bg"])
        self.d = ImageDraw.Draw(self.img)
        self.targets: list[dict] = []

    def _b(self, box):
        x, y, w, h = box
        s = self.s
        return [x * s, y * s, (x + w) * s - 1, (y + h) * s - 1]

    def rect(self, box, fill=None, outline=None, radius=0, width=1):
        self.d.rounded_rectangle(self._b(box), radius=radius * self.s, fill=fill,
                                 outline=outline, width=width * self.s)

    def text(self, x, y, s, size=14, fill=None):
        self.d.text((x * self.s, y * self.s), s, font=_font(size * self.s),
                    fill=fill or self.t["text"])

    def text_w(self, s, size=14) -> int:
        return int(math.ceil(_font(size * self.s).getlength(s) / self.s))

    def ctext(self, box, s, size=14, fill=None):
        x, y, w, h = box
        tw = self.text_w(s, size)
        self.text(x + (w - tw) / 2, y + (h - size) / 2 - 1, s, size, fill)

    def line(self, x1, y1, x2, y2, fill=None, width=1):
        s = self.s
        self.d.line([x1 * s, y1 * s, x2 * s, y2 * s], fill=fill or self.t["border"],
                    width=max(1, width * s))

    def ellipse(self, box, fill=None, outline=None, width=1):
        self.d.ellipse(self._b(box), fill=fill, outline=outline, width=width * self.s)

    def button(self, box, label, primary=False, size=14):
        t = self.t
        self.rect(box, fill=t["accent"] if primary else t["button"],
                  outline=None if primary else t["border"], radius=5)
        self.ctext(box, label, size, t["on_accent"] if primary else t["text"])
        return box

    def field(self, box, placeholder=""):
        self.rect(box, fill=self.t["panel"], outline=self.t["border"], radius=4)
        if placeholder:
            self.text(box[0] + 8, box[1] + (box[3] - 14) / 2 - 1, placeholder, 14, self.t["dim"])
        return box

    def checkbox(self, x, y, label, checked=False):
        box = (x, y, 16, 16)
        self.rect(box, fill=self.t["accent"] if checked else self.t["panel"],
                  outline=self.t["border"], radius=3)
        if checked:
            self.line(x + 3, y + 8, x + 7, y + 12, self.t["on_accent"], 2)
            self.line(x + 7, y + 12, x + 13, y + 4, self.t["on_accent"], 2)
        self.text(x + 24, y, label)
        return box

    def close_x(self, box, color=None):
        x, y, w, h = box
        c = color or self.t["dim"]
        self.line(x + 2, y + 2, x + w - 3, y + h - 3, c, 1)
        self.line(x + w - 3, y + 2, x + 2, y + h - 3, c, 1)
        return box

    def glyph(self, kind, box):
        """A 16-px icon drawn from primitives; `box` is the whole button."""
        t = self.t
        x, y, w, h = box
        cx, cy = x + w // 2, y + h // 2
        c = t["text"]
        if kind in ("B", "I", "U", "S"):
            self.ctext(box, kind, 14, c)
            if kind == "U":
                self.line(cx - 4, cy + 7, cx + 4, cy + 7, c)
        elif kind == "plus":
            self.line(cx - 6, cy, cx + 6, cy, c, 2)
            self.line(cx, cy - 6, cx, cy + 6, c, 2)
        elif kind == "minus":
            self.line(cx - 6, cy, cx + 6, cy, c, 2)
        elif kind == "search":
            self.ellipse((cx - 6, cy - 6, 9, 9), outline=c, width=2)
            self.line(cx + 2, cy + 2, cx + 6, cy + 6, c, 2)
        elif kind == "trash":
            self.rect((cx - 5, cy - 3, 10, 10), outline=c)
            self.line(cx - 7, cy - 5, cx + 7, cy - 5, c, 2)
        elif kind == "gear":
            self.ellipse((cx - 5, cy - 5, 10, 10), outline=c, width=2)
            for a in range(0, 360, 60):
                r = math.radians(a)
                self.line(cx + 5 * math.cos(r), cy + 5 * math.sin(r),
                          cx + 8 * math.cos(r), cy + 8 * math.sin(r), c, 2)
        elif kind.startswith("align"):
            widths = {"align_left": (12, 8, 12, 6), "align_center": (12, 8, 12, 6),
                      "align_right": (12, 8, 12, 6)}[kind]
            for i, lw in enumerate(widths):
                if kind == "align_left":
                    x0 = cx - 6
                elif kind == "align_right":
                    x0 = cx + 6 - lw
                else:
                    x0 = cx - lw // 2
                self.line(x0, cy - 6 + i * 4, x0 + lw, cy - 6 + i * 4, c)
        elif kind == "undo":
            self.d.arc(self._b((cx - 6, cy - 5, 12, 12)), 180, 360, fill=c, width=2 * self.s)
            self.line(cx - 6, cy + 1, cx - 9, cy - 3, c, 2)
        elif kind == "star":
            pts = []
            for i in range(10):
                r = 7 if i % 2 == 0 else 3
                a = math.radians(-90 + i * 36)
                pts.append(((cx + r * math.cos(a)) * self.s, (cy + r * math.sin(a)) * self.s))
            self.d.polygon(pts, outline=c)
        elif kind == "play":
            s = self.s
            self.d.polygon([((cx - 4) * s, (cy - 6) * s), ((cx - 4) * s, (cy + 6) * s),
                            ((cx + 6) * s, cy * s)], fill=c)
        elif kind == "pause":
            self.rect((cx - 5, cy - 6, 3, 12), fill=c)
            self.rect((cx + 2, cy - 6, 3, 12), fill=c)
        elif kind == "image":
            self.rect((cx - 7, cy - 6, 14, 12), outline=c)
            self.line(cx - 6, cy + 4, cx - 1, cy - 1, c)
            self.line(cx - 1, cy - 1, cx + 6, cy + 5, c)
        elif kind == "link":
            self.ellipse((cx - 8, cy - 3, 9, 6), outline=c)
            self.ellipse((cx - 1, cy - 3, 9, 6), outline=c)
        return box

    def window(self, box, title):
        """A window frame with a title bar; returns the content box."""
        x, y, w, h = box
        t = self.t
        self.rect(box, fill=t["panel"], outline=t["border"], radius=8)
        self.rect((x, y, w, 32), fill=t["bar"], radius=8)
        self.rect((x, y + 24, w, 8), fill=t["bar"])
        self.line(x, y + 32, x + w, y + 32)
        self.ctext((x, y, w, 32), title, 13, t["dim"])
        return (x, y + 33, w, h - 33)

    def target(self, description, box):
        self.targets.append({"description": description,
                             "box": tuple(int(round(v)) for v in box)})

    def render(self) -> dict:
        img = self.img
        if self.s != 1:
            img = img.resize((W, H), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=False)
        return {"png": buf.getvalue(), "w": W, "h": H, "name": self.name,
                "targets": list(self.targets)}


# --- the screens ---------------------------------------------------------------

def _editor(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.rect((0, 0, W, 28), fill=t["bar"])
    for i, m in enumerate(("File", "Edit", "View", "Window", "Help")):
        sc.text(16 + i * 64, 7, m, 13)
    sc.rect((0, 28, 220, H - 28), fill=t["panel"])
    sc.line(220, 28, 220, H)
    files = ["README.md", "main.py", "config.py", "server.py", "notes.txt", "todo.md"]
    for i, f in enumerate(files):
        sc.text(24, 48 + i * 28, f, 14)
    pick = rnd.choice(files[1:5])
    k = files.index(pick)
    sc.target(f"the file \"{pick}\" in the sidebar", (16, 42 + k * 28, 190, 26))
    run = sc.button((1060, 40, 90, 30), "Run", primary=True)
    stop = sc.button((1160, 40, 90, 30), "Stop")
    sc.target("the Run button", run)
    sc.target("the Stop button at the top right", stop)
    for i in range(18):
        sc.text(250, 90 + i * 22, f"{i + 1:>3}  " + "x" * rnd.randint(8, 60), 13, t["dim"])
    sc.rect((0, H - 26, W, 26), fill=t["bar"])
    br = (1140, H - 24, 120, 22)
    sc.ctext(br, "main +2", 12, t["dim"])
    sc.target("the git branch indicator in the status bar", br)


def _glyph_toolbar(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.rect((0, 0, W, 36), fill=t["bar"])
    kinds = ["B", "I", "U", "S", "align_left", "align_center", "align_right",
             "undo", "plus", "minus", "search", "trash", "gear", "star",
             "image", "link", "play", "pause"]
    boxes = {}
    x = 12
    for i, k in enumerate(kinds):
        box = (x, 8, 22, 22)
        sc.glyph(k, box)
        boxes[k] = box
        x += 24 + (10 if i in (3, 6, 9, 13) else 0)
        if i in (3, 6, 9, 13):
            sc.line(x - 7, 10, x - 7, 28)
    sc.rect((40, 60, W - 80, H - 100), fill=t["panel"], outline=t["border"])
    for i in range(14):
        sc.text(60, 80 + i * 24, "Lorem ipsum " * rnd.randint(2, 7), 14, t["dim"])
    desc = {"B": "the bold (B) button in the toolbar",
            "U": "the underline (U) button in the toolbar",
            "align_right": "the align-right icon in the toolbar",
            "trash": "the trash can icon in the toolbar",
            "gear": "the gear (settings) icon in the toolbar",
            "search": "the magnifying glass icon in the toolbar",
            "star": "the star icon in the toolbar",
            "link": "the link (chain) icon in the toolbar",
            "undo": "the undo arrow in the toolbar",
            "plus": "the plus (+) icon in the toolbar"}
    for k in rnd.sample(sorted(desc), 5):
        sc.target(desc[k], boxes[k])


def _form(sc: Screen, rnd: random.Random):
    c = sc.window((340, 90, 600, 560), "Create account")
    x, y = c[0] + 40, c[1] + 30
    fields = [("Full name", "Ada Lovelace"), ("Email", "you@example.com"),
              ("Password", ""), ("Confirm password", "")]
    for i, (lab, ph) in enumerate(fields):
        sc.text(x, y + i * 76, lab, 14)
        box = sc.field((x, y + 22 + i * 76, 520, 34), ph)
        if lab in ("Email", "Confirm password", "Full name"):
            sc.target(f"the {lab} text field", box)
    cb = sc.checkbox(x, y + 312, "I agree to the terms", checked=False)
    sc.target("the checkbox to agree to the terms", cb)
    sc.button((x + 300, y + 360, 100, 36), "Cancel")
    sub = sc.button((x + 410, y + 360, 110, 36), "Sign up", primary=True)
    sc.target("the Sign up button", sub)


def _login(sc: Screen, rnd: random.Random):
    c = sc.window((440, 160, 400, 440), "Sign in")
    x, y = c[0] + 30, c[1] + 30
    sc.text(x, y, "Username", 14)
    u = sc.field((x, y + 22, 340, 34), "username")
    sc.text(x, y + 76, "Password", 14)
    p = sc.field((x, y + 98, 340, 34))
    rm = sc.checkbox(x, y + 150, "Remember me", checked=True)
    go = sc.button((x, y + 190, 340, 38), "Sign in", primary=True)
    fg = (x + 100, y + 246, sc.text_w("Forgot password?"), 16)
    sc.text(fg[0], fg[1], "Forgot password?", 14, sc.t["accent"])
    for d, b in (("the username field", u), ("the password field", p),
                 ("the Remember me checkbox", rm), ("the Sign in button", go),
                 ("the Forgot password? link", fg)):
        sc.target(d, b)


def _menu(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.rect((0, 0, W, 28), fill=t["bar"])
    names = ["File", "Edit", "View", "Go", "Help"]
    for i, m in enumerate(names):
        sc.text(16 + i * 64, 7, m, 13)
    sc.rect((8, 2, 52, 24), fill=t["sel"], radius=4)
    sc.text(16, 7, "File", 13)
    items = ["New Window", "Open...", "Open Recent", "-", "Save", "Save As...",
             "Export as PDF...", "-", "Print...", "Close Window"]
    mx, my, mw = 8, 30, 240
    rows = [i for i in items]
    mh = sum(9 if r == "-" else 26 for r in rows) + 10
    sc.rect((mx, my, mw, mh), fill=t["panel"], outline=t["border"], radius=6)
    yy = my + 5
    boxes = {}
    for r in rows:
        if r == "-":
            sc.line(mx + 8, yy + 4, mx + mw - 8, yy + 4)
            yy += 9
            continue
        sc.text(mx + 14, yy + 5, r, 14)
        boxes[r] = (mx + 4, yy, mw - 8, 26)
        yy += 26
    for r in ("Save As...", "Export as PDF...", "Close Window", "Open Recent"):
        sc.target(f"the \"{r.rstrip('.')}\" item in the open File menu", boxes[r])
    sc.target("the Help menu in the menu bar", (8 + 4 * 64, 2, 52, 24))


def _context_menu(sc: Screen, rnd: random.Random):
    t = sc.t
    for i in range(12):
        sc.rect((80 + (i % 6) * 190, 80 + (i // 6) * 150, 150, 110), fill=t["panel"],
                outline=t["border"], radius=6)
        sc.ctext((80 + (i % 6) * 190, 170 + (i // 6) * 150, 150, 20), f"Photo {i + 1:02}.jpg", 12)
    mx, my = 470, 260
    items = ["Open", "Open With", "Rename", "Duplicate", "Copy", "Move to Trash", "Get Info"]
    sc.rect((mx, my, 200, len(items) * 26 + 10), fill=t["panel"], outline=t["border"], radius=6)
    boxes = {}
    for i, r in enumerate(items):
        sc.text(mx + 14, my + 10 + i * 26, r, 14, t["danger"] if r == "Move to Trash" else None)
        boxes[r] = (mx + 4, my + 5 + i * 26, 192, 26)
    for r in ("Rename", "Move to Trash", "Get Info", "Duplicate"):
        sc.target(f"\"{r}\" in the context menu", boxes[r])


def _dialog_dup(sc: Screen, rnd: random.Random):
    """A page with its own Save button under a modal dialog that also says
    Save: the description has to disambiguate."""
    t = sc.t
    sc.rect((0, 0, W, 56), fill=t["panel"])
    sc.line(0, 56, W, 56)
    sc.text(24, 18, "Quarterly report - draft", 16)
    page_save = sc.button((1060, 12, 90, 32), "Save")
    page_share = sc.button((1160, 12, 96, 32), "Share", primary=True)
    for i in range(16):
        sc.text(80, 90 + i * 26, "Revenue " * rnd.randint(3, 12), 14, t["dim"])
    ov = Image.new("RGBA", sc.img.size, t["shade"])
    sc.img.paste(Image.alpha_composite(sc.img.convert("RGBA"), ov).convert("RGB"))
    sc.d = ImageDraw.Draw(sc.img)
    c = sc.window((390, 250, 500, 220), "Unsaved changes")
    sc.text(c[0] + 24, c[1] + 24, "Save changes to \"Quarterly report\" before closing?", 14)
    dsave = sc.button((c[0] + c[2] - 110, c[1] + c[3] - 56, 90, 34), "Save", primary=True)
    dcancel = sc.button((c[0] + c[2] - 210, c[1] + c[3] - 56, 90, 34), "Cancel")
    ddont = sc.button((c[0] + 20, c[1] + c[3] - 56, 120, 34), "Don't Save")
    sc.target("the Save button in the dialog", dsave)
    sc.target("the Save button in the page's top bar, behind the dialog", page_save)
    sc.target("the Cancel button in the dialog", dcancel)
    sc.target("the Don't Save button", ddont)
    sc.target("the Share button in the top bar", page_share)


def _tabs_close(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.rect((0, 0, W, 38), fill=t["bar"])
    names = ["Inbox", "Report.pdf", "Budget 2026.xlsx", "Design review", "notes.txt",
             "Invoice 0412", "Roadmap"]
    x = 8
    closes = {}
    tabs = {}
    for i, n in enumerate(names):
        w = sc.text_w(n, 13) + 46
        box = (x, 6, w, 30)
        sc.rect(box, fill=t["panel"] if i == 2 else t["button"], outline=t["border"], radius=6)
        sc.text(x + 12, 13, n, 13)
        cb = (x + w - 22, 14, 12, 12)
        sc.close_x(cb)
        closes[n], tabs[n] = cb, box
        x += w + 4
    sc.rect((0, 38, W, H - 38), fill=t["panel"])
    for n in rnd.sample(names, 3):
        sc.target(f"the small close (×) button on the \"{n}\" tab", closes[n])
    n = rnd.choice([m for m in names if m != "Budget 2026.xlsx"])
    sc.target(f"the \"{n}\" tab (not its close button)", (tabs[n][0], 6, tabs[n][2] - 24, 30))


def _toasts(sc: Screen, rnd: random.Random):
    t = sc.t
    for i in range(20):
        sc.text(40, 40 + i * 30, "Build log line " + str(rnd.randint(1000, 9999)), 13, t["dim"])
    msgs = ["Upload complete", "3 files could not be synced", "New message from Sam",
            "Backup finished"]
    for i, m in enumerate(msgs):
        box = (W - 380, 20 + i * 78, 360, 66)
        sc.rect(box, fill=t["panel"], outline=t["border"], radius=8)
        sc.text(box[0] + 16, box[1] + 14, m, 14)
        sc.text(box[0] + 16, box[1] + 38, "just now", 12, t["dim"])
        cb = (box[0] + box[2] - 24, box[1] + 10, 12, 12)
        sc.close_x(cb)
        sc.target(f"the close (×) button on the \"{m}\" notification", cb)


def _rows(sc: Screen, rnd: random.Random):
    t = sc.t
    c = sc.window((160, 60, 960, 680), "Documents")
    names = [f"invoice-{n:04}.pdf" for n in sorted(rnd.sample(range(100, 999), 14))]
    dels = {}
    opens = {}
    for i, n in enumerate(names):
        y = c[1] + 12 + i * 44
        if i % 2:
            sc.rect((c[0] + 8, y - 4, c[2] - 16, 40), fill=t["bg"])
        sc.text(c[0] + 24, y + 8, n, 14)
        sc.text(c[0] + 360, y + 8, f"{rnd.randint(40, 900)} KB", 13, t["dim"])
        opens[n] = sc.button((c[0] + c[2] - 200, y + 2, 80, 28), "Open")
        dels[n] = sc.button((c[0] + c[2] - 110, y + 2, 80, 28), "Delete")
    for n in rnd.sample(names, 3):
        sc.target(f"the Delete button on the row for {n}", dels[n])
    n = rnd.choice(names)
    sc.target(f"the Open button on the row for {n}", opens[n])


def _table_checks(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.text(40, 30, "Users", 18)
    people = ["Alice Moreau", "Bilal Khan", "Chen Wei", "Dana Ortiz", "Emeka Obi",
              "Freya Lind", "Gus Patel", "Hana Sato", "Ivan Petrov", "Jo Adams"]
    boxes = {}
    for i, p in enumerate(people):
        y = 80 + i * 40
        sc.line(40, y + 36, W - 40, y + 36)
        boxes[p] = sc.checkbox(56, y + 10, "", checked=(i % 3 == 0))
        sc.text(96, y + 10, p, 14)
        sc.text(420, y + 10, p.split()[0].lower() + "@example.com", 14, t["dim"])
        sc.text(800, y + 10, rnd.choice(["Admin", "Editor", "Viewer"]), 14)
    for p in rnd.sample(people, 4):
        sc.target(f"the checkbox on the row for {p}", boxes[p])
    inv = sc.button((W - 180, 24, 140, 34), "Invite user", primary=True)
    sc.target("the Invite user button", inv)


def _toggles(sc: Screen, rnd: random.Random):
    t = sc.t
    c = sc.window((260, 60, 760, 660), "Settings")
    opts = ["Wi-Fi", "Bluetooth", "Airplane mode", "Do not disturb", "Night light",
            "Location services", "Automatic updates", "Send diagnostics"]
    for i, o in enumerate(opts):
        y = c[1] + 20 + i * 64
        sc.text(c[0] + 30, y + 10, o, 15)
        on = rnd.random() < 0.5
        box = (c[0] + c[2] - 90, y + 6, 46, 26)
        sc.rect(box, fill=t["accent"] if on else t["border"], radius=13)
        kx = box[0] + (22 if on else 3)
        sc.ellipse((kx, box[1] + 3, 20, 20), fill=(255, 255, 255))
        if o in ("Bluetooth", "Night light", "Send diagnostics", "Location services"):
            sc.target(f"the toggle switch for {o}", box)


def _email(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.rect((0, 0, 220, H), fill=t["panel"])
    comp = sc.button((16, 16, 188, 40), "Compose", primary=True)
    folders = ["Inbox", "Starred", "Sent", "Drafts", "Spam", "Trash"]
    fb = {}
    for i, f in enumerate(folders):
        fb[f] = (8, 76 + i * 34, 204, 30)
        sc.text(24, 83 + i * 34, f, 14)
    search = sc.field((240, 14, 520, 36), "Search mail")
    senders = ["GitHub", "Maya", "Stripe", "Leo", "Calendar", "Maya", "Airline"]
    for i, s in enumerate(senders):
        y = 70 + i * 52
        sc.line(240, y + 50, W - 20, y + 50)
        sc.text(260, y + 16, s, 14)
        sc.text(420, y + 16, "Re: " + "update " * rnd.randint(1, 5), 14, t["dim"])
    sc.target("the Compose button", comp)
    sc.target("the Drafts folder in the sidebar", fb["Drafts"])
    sc.target("the Search mail box", search)
    sc.target("the email from Stripe in the list", (240, 70 + 2 * 52, W - 260, 50))


def _calendar(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.text(40, 24, "September 2026", 20)
    prev = sc.button((W - 140, 20, 44, 32), "<")
    nxt = sc.button((W - 88, 20, 44, 32), ">")
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cw, ch = (W - 80) // 7, 110
    for i, d in enumerate(days):
        sc.text(40 + i * cw + 8, 70, d, 13, t["dim"])
    cells = {}
    for n in range(1, 31):
        k = n + 0          # 1 Sep 2026 is a Tuesday
        col, row = k % 7, k // 7
        box = (40 + col * cw, 92 + row * ch, cw, ch)
        sc.rect(box, fill=t["panel"], outline=t["border"])
        sc.text(box[0] + 8, box[1] + 6, str(n), 14)
        cells[n] = box
    for n in rnd.sample(range(2, 30), 3):
        sc.target(f"the day {n} in the calendar grid", cells[n])
    sc.target("the next-month (>) button", nxt)
    sc.target("the previous-month (<) button", prev)


def _file_icons(sc: Screen, rnd: random.Random):
    t = sc.t
    names = ["Photos", "Music", "Taxes 2025", "Projects", "Downloads", "backup.zip",
             "resume.pdf", "Screenshots", "Games", "notes.md", "Recipes", "Taxes 2024"]
    boxes = {}
    for i, n in enumerate(names):
        x, y = 80 + (i % 6) * 190, 100 + (i // 6) * 200
        sc.rect((x + 35, y, 80, 64), fill=t["accent"] if "." not in n else t["border"], radius=6)
        sc.ctext((x, y + 72, 150, 20), n, 13)
        boxes[n] = (x + 20, y - 4, 110, 100)
    for n in ("Taxes 2024", "Taxes 2025", "resume.pdf", "Screenshots"):
        sc.target(f"the \"{n}\" icon", boxes[n])


def _player(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.rect((0, H - 100, W, 100), fill=t["panel"])
    sc.line(0, H - 100, W, H - 100)
    sc.rect((20, H - 84, 68, 68), fill=t["accent"], radius=4)
    sc.text(104, H - 70, "Clair de Lune", 15)
    sc.text(104, H - 46, "Debussy", 13, t["dim"])
    cx = W // 2
    prev = (cx - 80, H - 80, 32, 32)
    play = (cx - 20, H - 84, 40, 40)
    nxt = (cx + 48, H - 80, 32, 32)
    sc.ctext(prev, "|<", 14)
    sc.ellipse(play, fill=t["text"])
    s = sc.s
    sc.d.polygon([((cx - 5) * s, (H - 73) * s), ((cx - 5) * s, (H - 55) * s),
                  ((cx + 9) * s, (H - 64) * s)], fill=t["panel"])
    sc.ctext(nxt, ">|", 14)
    vol = (W - 200, H - 56, 150, 8)
    sc.rect(vol, fill=t["border"], radius=4)
    sc.ellipse((W - 110, H - 60, 16, 16), fill=t["accent"])
    for i in range(8):
        sc.text(60, 60 + i * 40, f"{i + 1}.  Track {rnd.randint(10, 99)}", 15)
    sc.target("the play button", play)
    sc.target("the next-track button", nxt)
    sc.target("the previous-track button", prev)
    sc.target("the volume slider's knob", (W - 110, H - 60, 16, 16))


def _traffic_lights(sc: Screen, rnd: random.Random):
    t = sc.t
    x, y = 200, 120
    sc.rect((x, y, 880, 560), fill=(20, 20, 20), outline=t["border"], radius=10)
    sc.rect((x, y, 880, 30), fill=t["bar"], radius=10)
    cols = [(255, 95, 86), (255, 189, 46), (39, 201, 63)]
    for i, c in enumerate(cols):
        sc.ellipse((x + 12 + i * 20, y + 9, 12, 12), fill=c)
    sc.ctext((x, y, 880, 30), "zsh - 120x40", 13, t["dim"])
    for i in range(18):
        sc.text(x + 14, y + 44 + i * 26, "$ " + "ls -la " * rnd.randint(1, 5), 14, (200, 200, 200))
    sc.target("the red close circle at the top-left of the terminal window", (x + 12, y + 9, 12, 12))
    sc.target("the green zoom circle of the terminal window", (x + 52, y + 9, 12, 12))
    sc.target("the yellow minimise circle of the terminal window", (x + 32, y + 9, 12, 12))


def _browser(sc: Screen, rnd: random.Random):
    t = sc.t
    sc.rect((0, 0, W, 80), fill=t["bar"])
    back = (10, 44, 28, 28)
    fwd = (42, 44, 28, 28)
    reload_ = (74, 44, 28, 28)
    sc.ctext(back, "<", 16)
    sc.ctext(fwd, ">", 16)
    sc.ctext(reload_, "C", 14)
    url = sc.field((112, 44, 900, 28), "https://example.com/pricing")
    sc.rect((8, 6, 220, 32), fill=t["panel"], radius=6)
    sc.text(20, 14, "Pricing - Example", 13)
    nav = ["Product", "Pricing", "Docs", "Blog"]
    for i, n in enumerate(nav):
        sc.text(80 + i * 110, 110, n, 15)
    buy = []
    for i, plan in enumerate(("Starter", "Team", "Enterprise")):
        bx = 100 + i * 370
        sc.rect((bx, 170, 320, 420), fill=t["panel"], outline=t["border"], radius=10)
        sc.text(bx + 24, 196, plan, 20)
        sc.text(bx + 24, 240, f"${[9, 29, 99][i]}/mo", 26)
        buy.append(sc.button((bx + 24, 520, 272, 40), "Choose plan", primary=(i == 1)))
    sc.target("the back button of the browser", back)
    sc.target("the address bar", url)
    sc.target("the Choose plan button under Team", buy[1])
    sc.target("the Choose plan button under Enterprise", buy[2])
    sc.target("the Docs link in the page's navigation", (80 + 2 * 110, 108, sc.text_w("Docs", 15), 20))


# name, builder, theme, scale
_SPECS = [
    ("editor-light", _editor, "light", 1),
    ("editor-dark", _editor, "dark", 1),
    ("editor-retina", _editor, "light", 2),
    ("glyph-toolbar-light", _glyph_toolbar, "light", 1),
    ("glyph-toolbar-dark", _glyph_toolbar, "dark", 1),
    ("form-signup", _form, "light", 1),
    ("form-login-dark", _login, "dark", 1),
    ("form-login-retina", _login, "light", 2),
    ("menu-file", _menu, "light", 1),
    ("menu-context-dark", _context_menu, "dark", 1),
    ("dialog-duplicate-save", _dialog_dup, "light", 1),
    ("dialog-duplicate-save-dark", _dialog_dup, "dark", 1),
    ("tabs-close-buttons", _tabs_close, "light", 1),
    ("toasts-close-dark", _toasts, "dark", 1),
    ("rows-similar", _rows, "light", 1),
    ("table-checkboxes-dark", _table_checks, "dark", 1),
    ("settings-toggles", _toggles, "light", 1),
    ("email-client", _email, "light", 1),
    ("calendar-dark", _calendar, "dark", 1),
    ("file-icons", _file_icons, "light", 1),
    ("media-player-dark", _player, "dark", 1),
    ("terminal-traffic-lights", _traffic_lights, "light", 1),
    ("browser-pricing", _browser, "light", 1),
    ("browser-pricing-retina", _browser, "dark", 2),
]

_cache: list[dict] | None = None
_lock = threading.Lock()


def count() -> int:
    return len(_SPECS)


def fixtures() -> list[dict]:
    """Every labelled screen, rendered once per process."""
    global _cache
    if not HAVE_PIL:
        raise FixturesUnavailable("Pillow not installed")
    with _lock:
        if _cache is None:
            out = []
            for i, (name, build, theme, scale) in enumerate(_SPECS):
                rnd = random.Random(SEED * 1000 + i)
                sc = Screen(name, theme, scale)
                build(sc, rnd)
                out.append(sc.render())
            _cache = out
        return _cache


def fixture(i: int) -> dict:
    return fixtures()[i]
