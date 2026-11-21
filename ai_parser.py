"""Reads an order (text or photo) with FREE AI models: Gemini, Groq, OpenRouter (free models) or a local Ollama.
Providers (incl. a keyless one) are tried in order, so one free-tier limit never stops the bot.
Only plain HTTPS calls (httpx) - no SDK. API keys are sent in headers, never logged."""
import base64
import io
import json
import logging
import re

import httpx
from PIL import Image

import config

log = logging.getLogger("esmo-bot")


class AIBusy(Exception):
    """Rate limit / overloaded."""


class AIModelMissing(Exception):
    """Model name not available."""


class AIError(Exception):
    pass


SHAPE = """Return ONLY a JSON object with exactly these keys:
{
  "customer": string  (customer/client name; "" if not written),
  "address": string   (address/town; "" if not written),
  "payment_mode": "CASH" | "TRANSFER" | "POS" | "LOAN" | "UNKNOWN"   (LOAN = credit/debt/pay later; UNKNOWN if not written),
  "sale_type": "retail" | "whole" | "supply" | "UNKNOWN"   (whole = wholesale; UNKNOWN if not written),
  "discount": number  (total naira discount; 0 if none or not mentioned),
  "items": [ {"name": string, "qty": number, "unit_price": number} ],
  "problems": [string]  (anything hard to read or doubtful)
}
In items: qty = 0 if the quantity is not clear; unit_price = 0 unless a price is written for that item."""


def _system(product_names):
    catalogue = "\n".join(f"- {n}" for n in product_names)
    return (
        "You are the order-entry clerk for ESMO PHARMACEUTICAL, a Nigerian poultry/veterinary drug company. "
        "Read the order and extract it. Orders are typed messages (informal, typos, Pidgin/Hausa words) or "
        "photos of handwritten/printed/WhatsApp orders.\n\n"
        f"PRODUCT CATALOGUE (use these EXACT names when you are confident which product is meant):\n{catalogue}\n\n"
        "Rules:\n"
        "- Copy what is written. NEVER guess a quantity, price, name, address, payment method or sale type "
        "that is not there - use the 'not written' value instead (the clerk will ask the boss).\n"
        "- 'x20', '20pcs', '20 cartons', 'twenty' all mean quantity 20. A number beside a product is its quantity "
        "unless it is clearly part of the name (e.g. 'TYLOESMO 250', 'AMPROCOC 100G').\n"
        "- Ignore greetings and chit-chat.\n"
        "- If a number or word is smudged/ambiguous, give your best reading AND add a note in 'problems'.\n\n"
        + SHAPE
    )


def _prepare_image(data):
    """Return (bytes, mime). Re-encodes big/unusual images so the API accepts them."""
    im = Image.open(io.BytesIO(data))
    im.load()
    fmt = (im.format or "").upper()
    if fmt in ("JPEG", "PNG", "WEBP") and len(data) < 3_500_000 and max(im.size) <= 2400:
        return data, {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[fmt]
    im = im.convert("RGB")
    im.thumbnail((2400, 2400))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def _extract_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise AIError("AI did not return JSON")
    return json.loads(text[a:b + 1])


def _post(url, headers, payload, timeout=90):
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        raise AIError(f"network error: {type(e).__name__}") from None
    if r.status_code in (429, 500, 502, 503, 504):
        raise AIBusy()
    if r.status_code == 404:
        raise AIModelMissing()
    if r.status_code >= 400:
        raise AIError(f"AI service returned HTTP {r.status_code}")   # body deliberately not logged
    return r.json()


# ---------------- Gemini ----------------
_GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "customer": {"type": "STRING"},
        "address": {"type": "STRING"},
        "payment_mode": {"type": "STRING", "enum": ["CASH", "TRANSFER", "POS", "LOAN", "UNKNOWN"]},
        "sale_type": {"type": "STRING", "enum": ["retail", "whole", "supply", "UNKNOWN"]},
        "discount": {"type": "NUMBER"},
        "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "qty": {"type": "NUMBER"}, "unit_price": {"type": "NUMBER"}},
            "required": ["name", "qty", "unit_price"]}},
        "problems": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["customer", "address", "payment_mode", "sale_type", "discount", "items", "problems"],
}


def _gemini(text, image, names):
    if not config.GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY is not set")
    parts = []
    if image:
        data, mime = _prepare_image(image)
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    parts.append({"text": "Extract the order from this " + ("photo" if image else "message") +
                  (f".\n\nMessage text:\n{text}" if text else ".")})
    payload = {
        "systemInstruction": {"parts": [{"text": _system(names)}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": _GEMINI_SCHEMA},
    }
    last = None
    for model in config.GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            out = _post(url, {"x-goog-api-key": config.GEMINI_API_KEY}, payload)
            return _extract_json(out["candidates"][0]["content"]["parts"][0]["text"])
        except (AIBusy, AIModelMissing) as e:
            last = e
        except (KeyError, IndexError, json.JSONDecodeError):
            last = AIError("unreadable AI reply")
    raise last or AIError("no model available")


# ---------------- Groq / OpenRouter (OpenAI-compatible) ----------------
def _openai_compat(url, key, model, text, image, names, json_mode):
    content = [{"type": "text", "text": "Extract the order from this " + ("photo" if image else "message") +
                (f".\n\nMessage text:\n{text}" if text else ".")}]
    if image:
        data, mime = _prepare_image(image)
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "system", "content": _system(names)}, {"role": "user", "content": content}]}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    out = _post(url, {"Authorization": f"Bearer {key}"} if key else {}, payload)
    try:
        return _extract_json(out["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise AIError("unreadable AI reply") from None


def _groq(text, image, names):
    return _openai_compat("https://api.groq.com/openai/v1/chat/completions", config.GROQ_API_KEY,
                          config.GROQ_MODEL, text, image, names, json_mode=True)


def _openrouter(text, image, names):
    return _openai_compat("https://openrouter.ai/api/v1/chat/completions", config.OPENROUTER_API_KEY,
                          config.OPENROUTER_MODEL, text, image, names, json_mode=False)


def _pollinations(text, image, names):
    """Keyless free AI. Anonymous endpoint unless a Pollinations key is set."""
    if config.POLLINATIONS_API_KEY:
        url, key = "https://gen.pollinations.ai/v1/chat/completions", config.POLLINATIONS_API_KEY
    else:
        url, key = "https://text.pollinations.ai/openai", ""
    return _openai_compat(url, key, config.POLLINATIONS_MODEL, text, image, names, json_mode=False)


# ---------------- Ollama (local, free forever, private) ----------------
def _ollama(text, image, names):
    user = {"role": "user", "content": "Extract the order from this " + ("photo" if image else "message") +
            (f".\n\nMessage text:\n{text}" if text else ".")}
    if image:
        data, _ = _prepare_image(image)
        user["images"] = [base64.b64encode(data).decode()]
    payload = {"model": config.OLLAMA_MODEL, "stream": False, "format": "json", "options": {"temperature": 0},
               "messages": [{"role": "system", "content": _system(names)}, user]}
    out = _post(config.OLLAMA_URL.rstrip("/") + "/api/chat", {}, payload, timeout=300)
    try:
        return _extract_json(out["message"]["content"])
    except (KeyError, TypeError, json.JSONDecodeError):
        raise AIError("unreadable AI reply") from None


PROVIDERS = {"gemini": _gemini, "groq": _groq, "openrouter": _openrouter, "pollinations": _pollinations,
             "ollama": _ollama}


def parse_order(text, image_bytes, product_names):
    """Try each configured free provider in order; the first good answer wins."""
    last, busy = None, False
    for name in config.ready_providers():
        fn = PROVIDERS.get(name)
        if not fn:
            continue
        try:
            result = fn(text, image_bytes, product_names)
            log.info("order read by %s", name)
            return result
        except AIBusy as e:
            busy, last = True, e
            log.warning("%s is busy/out of free quota, trying next", name)
        except (AIModelMissing, AIError) as e:
            last = e
            log.warning("%s failed (%s), trying next", name, type(e).__name__)
    if busy:
        raise AIBusy()
    raise last or AIError("No AI provider is set up. Add a free key in the .env file.")
