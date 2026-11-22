"""Draws the invoice as an image (PNG) + PDF, laid out like the invoice in the web app."""
import os
from PIL import Image, ImageDraw, ImageFont

import config

S = 2                      # scale: 850 "css px" wide -> 1700 px image
PAGE_W = 850
PAD = 50
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREY = (242, 242, 242)

_FONT_DIRS = [
    config.BASE_DIR,                         # fonts sit next to the code (no folders needed)
    os.path.join(config.BASE_DIR, "fonts"),
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/system/fonts",                         # Android / Termux
    "C:/Windows/Fonts",
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
]
_REGULAR = ["DejaVuSans.ttf", "Roboto-Regular.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"]
_BOLD = ["DejaVuSans-Bold.ttf", "Roboto-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"]
_cache = {}


def font(css_px, bold=False):
    key = (css_px, bold)
    if key in _cache:
        return _cache[key]
    size = int(css_px * S)
    f = None
    for d in _FONT_DIRS:
        for name in (_BOLD if bold else _REGULAR):
            p = os.path.join(d, name)
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, size)
                    break
                except OSError:
                    pass
        if f:
            break
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
    _cache[key] = f
    return f


def fmt(n):
    n = float(n)
    if abs(n - round(n)) < 1e-9:
        return f"{int(round(n)):,}"
    return f"{n:,.2f}"


_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _below_1000(n):
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")
    return _ONES[n // 100] + " hundred" + (" and " + _below_1000(n % 100) if n % 100 else "")


def number_to_words(n):
    n = int(round(n))
    if n == 0:
        return "zero"
    res = ""
    for size, label in ((1_000_000_000, "billion"), (1_000_000, "million"), (1000, "thousand")):
        if n >= size:
            chunk = n // size
            if res and chunk < 100:
                res += "and "
            res += _below_1000(chunk) + f" {label} "
            n %= size
    if n > 0:
        if res and n < 100:
            res += "and "
        res += _below_1000(n)
    return res.strip()


def _wrap(draw, text, fnt, max_w):
    lines, cur = [], ""
    for word in str(text).split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            # a single very long word: hard-split it
            while draw.textlength(word, font=fnt) > max_w and len(word) > 1:
                k = len(word)
                while k > 1 and draw.textlength(word[:k], font=fnt) > max_w:
                    k -= 1
                lines.append(word[:k])
                word = word[k:]
            cur = word
    if cur or not lines:
        lines.append(cur)
    return lines


def _logo_image(px):
    """The built-in company logo, shown exactly as it is."""
    import base64
    import io
    from logo_data import LOGO_PNG_B64
    im = Image.open(io.BytesIO(base64.b64decode(LOGO_PNG_B64))).convert("RGB")
    return im.resize((px, px), Image.LANCZOS).convert("RGBA")


def render_receipt(order, png_path, pdf_path=None):
    """order: dict with inv, customer, address, mode, discount, total, date, items[{name,qty,price}]"""
    u = lambda v: int(v * S)
    items = order["items"]
    canvas_h = u(1500 + 120 * len(items))
    img = Image.new("RGB", (u(PAGE_W), canvas_h), WHITE)
    d = ImageDraw.Draw(img)
    L, R = u(PAD), u(PAGE_W - PAD)
    content_w = R - L
    y = u(PAD)

    def lh(css_px):           # line height
        return u(css_px * 1.25)

    # ---------- header ----------
    logo_px = u(100)
    logo = _logo_image(logo_px)
    img.paste(logo, (L, y), logo)
    right_lines = [("Email: ", COMPANY_EMAIL()), ("Website: ", COMPANY_WEB())]
    f11, f11b = font(11), font(11, True)
    right_w = max(d.textlength(a, font=f11b) + d.textlength(b, font=f11) for a, b in right_lines)
    text_x = L + logo_px + u(20)
    text_w = (R - right_w - u(15)) - text_x

    ty = y
    name_font = font(26, True)
    for line in _wrap(d, f"{config.COMPANY['name']} {config.COMPANY['suffix']}", name_font, text_w):
        d.text((text_x, ty), line, font=name_font, fill=BLACK)
        ty += lh(26)
    ty += u(4)
    for line in _wrap(d, f"{config.COMPANY['addr1']} {config.COMPANY['addr2']}", f11, text_w):
        d.text((text_x, ty), line, font=f11, fill=BLACK)
        ty += lh(11)
    d.text((text_x, ty), "Tel: ", font=f11b, fill=BLACK)
    d.text((text_x + d.textlength("Tel: ", font=f11b), ty), config.COMPANY["phone"], font=f11, fill=BLACK)
    ty += lh(11)

    ry = y + (max(logo_px, ty - y) - 2 * lh(11)) // 2
    for a, b in right_lines:
        wb = d.textlength(b, font=f11)
        d.text((R - wb, ry), b, font=f11, fill=BLACK)
        d.text((R - wb - d.textlength(a, font=f11b), ry), a, font=f11b, fill=BLACK)
        ry += lh(11)

    y = y + max(logo_px, ty - y) + u(15)
    d.rectangle((L, y, R, y + u(3) - 1), fill=BLACK)
    y += u(3) + u(20)

    # ---------- title ----------
    tf = font(24, True)
    title, spacing = "INVOICE", u(5)
    tw = sum(d.textlength(c, font=tf) for c in title) + spacing * (len(title) - 1)
    x = (L + R) / 2 - tw / 2
    for c in title:
        d.text((x, y), c, font=tf, fill=BLACK)
        x += d.textlength(c, font=tf) + spacing
    y += lh(24) + u(20)

    # ---------- client / invoice info ----------
    f13, f13b = font(13), font(13, True)
    left_w = u(480)
    ly = y
    for label, value in (("CLIENT: ", order["customer"]), ("ADDRESS: ", order.get("address") or "N/A")):
        lw = d.textlength(label, font=f13b)
        lines = _wrap(d, value, f13, left_w - lw)
        d.text((L, ly), label, font=f13b, fill=BLACK)
        for i, ln in enumerate(lines):
            d.text((L + lw, ly), ln, font=f13, fill=BLACK)
            ly += lh(13)
    ry = y
    inv_val = str(order["inv"])
    wv = d.textlength(inv_val, font=f13)
    d.text((R - wv, ry), inv_val, font=f13, fill=BLACK)
    d.text((R - wv - d.textlength("INV #: ", font=f13b), ry), "INV #: ", font=f13b, fill=BLACK)
    ry += lh(13)
    dv = order["date"]
    d.text((R - d.textlength(dv, font=f13), ry), dv, font=f13, fill=BLACK)
    ry += lh(13)
    y = max(ly, ry) + u(10) + u(20)

    # ---------- table ----------
    cols = [("SN", 40), ("DESCRIPTION", 250), ("QTY", 70), ("RATE", 100), ("DISC", 60), ("MODE", 90), ("AMOUNT", 140)]
    xs = [L]
    for _, w in cols:
        xs.append(xs[-1] + u(w))
    xs[-1] = R
    pad = u(8)
    bw = max(1, S)

    def cell_lines(row):
        return [_wrap(d, txt, f13, xs[i + 1] - xs[i] - 2 * pad) for i, txt in enumerate(row)]

    def draw_row(top, row, bold=False, bg=None):
        fnt = f13b if bold else f13
        wrapped = [_wrap(d, txt, fnt, xs[i + 1] - xs[i] - 2 * pad) for i, txt in enumerate(row)]
        n = max(len(w) for w in wrapped)
        h = n * lh(13) + 2 * pad
        if bg:
            d.rectangle((xs[0], top, xs[-1], top + h), fill=bg)
        for i, lines in enumerate(wrapped):
            for k, ln in enumerate(lines):
                d.text((xs[i] + pad, top + pad + k * lh(13)), ln, font=fnt, fill=BLACK)
        d.line((xs[0], top, xs[-1], top), fill=BLACK, width=bw)
        for xx in xs:
            d.line((xx, top, xx, top + h), fill=BLACK, width=bw)
        return h

    table_top = y
    y += draw_row(y, [c for c, _ in cols], bold=True, bg=GREY)
    for idx, it in enumerate(items, 1):
        row = [str(idx), it["name"], str(it["qty"]), fmt(it["price"]), "-", order["mode"], fmt(it["price"] * it["qty"])]
        y += draw_row(y, row)
    d.line((xs[0], y, xs[-1], y), fill=BLACK, width=bw)
    # outer table border
    d.rectangle((xs[0], table_top, xs[-1], y), outline=BLACK, width=bw)
    y += u(20)

    # ---------- totals ----------
    d.rectangle((L, y, R, y + u(2) - 1), fill=BLACK)
    y += u(2) + u(10)
    disc = order.get("discount", 0) or 0
    if disc > 0:
        f15, f15b = font(15), font(15, True)
        val = f"- NGN {fmt(disc)}"
        wv = d.textlength(val, font=f15)
        d.text((R - wv, y), val, font=f15, fill=(51, 51, 51))
        d.text((R - wv - d.textlength(" ", font=f15) - d.textlength("DISCOUNT DEDUCTED:", font=f15b), y),
               "DISCOUNT DEDUCTED:", font=f15b, fill=(51, 51, 51))
        y += lh(15) + u(5)
    gt = f"GRAND TOTAL: NGN {fmt(order['total'])}"
    f20b = font(20, True)
    d.text((R - d.textlength(gt, font=f20b), y), gt, font=f20b, fill=BLACK)
    y += lh(20) + u(15)

    f12, f12b = font(12), font(12, True)
    words = (number_to_words(order["total"]).upper() + " NAIRA")
    label = "TOTAL IN WORDS: "
    lw = d.textlength(label, font=f12b)
    first_w = content_w - 2 * u(10) - lw
    wlines = _wrap(d, words, f12, first_w)
    # re-wrap remaining text to full width
    if len(wlines) > 1:
        rest = words[len(wlines[0]):].strip()
        wlines = [wlines[0]] + _wrap(d, rest, f12, content_w - 2 * u(10))
    box_h = len(wlines) * lh(12) + 2 * u(10)
    d.rectangle((L, y, R, y + box_h), outline=BLACK, width=bw)
    d.text((L + u(10), y + u(10)), label, font=f12b, fill=BLACK)
    for i, ln in enumerate(wlines):
        d.text((L + u(10) + (lw if i == 0 else 0), y + u(10) + i * lh(12)), ln, font=f12, fill=BLACK)
    y += box_h + u(30)

    # ---------- footer ----------
    d.line((L, y, R, y), fill=BLACK, width=bw)
    y += u(10)
    fm = font(13, True)
    for ln in _wrap(d, config.COMPANY["motto"], fm, content_w):
        d.text(((L + R) / 2 - d.textlength(ln, font=fm) / 2, y), ln, font=fm, fill=BLACK)
        y += lh(13)
    y += u(10)
    d.line((L, y - u(5), R, y - u(5)), fill=BLACK, width=bw)
    y += u(5)
    tf2 = font(14, True)
    thanks, sp = "THANKS FOR YOUR PATRONAGE", u(2)
    tw = sum(d.textlength(c, font=tf2) for c in thanks) + sp * (len(thanks) - 1)
    x = (L + R) / 2 - tw / 2
    for c in thanks:
        d.text((x, y), c, font=tf2, fill=BLACK)
        x += d.textlength(c, font=tf2) + sp
    y += lh(14) + u(15)
    f10 = font(10)
    for line in (config.COMPANY["bank1"], config.COMPANY["bank2"]):
        d.text((L, y), line, font=f10, fill=BLACK)
        y += lh(10)
    y += u(PAD)

    img = img.crop((0, 0, u(PAGE_W), y))
    img.save(png_path, "PNG", optimize=True)
    if pdf_path:
        img.save(pdf_path, "PDF", resolution=205.0)  # ~A4 width
    return png_path


def COMPANY_EMAIL():
    return config.COMPANY["email"]


def COMPANY_WEB():
    return config.COMPANY["website"]
