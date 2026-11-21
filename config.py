"""Company details, bank lines and settings. Edit here, nowhere else."""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Same details as the web app (COMPANY object in bam.html)
COMPANY = {
    "name": "ESMO PHARMACEUTICAL",
    "suffix": "INT'L LTD",
    "motto": "THE PRIDE OF POULTRY FARMERS AND VETERINARY SOLUTION",
    "addr1": "NO 19 KATURU ROAD OFF ISA KAITA ROAD.,",
    "addr2": "BESIDE 212 BAKERY, KADUNA.",
    "phone": "08144571996, 080179863130",
    "email": "info@esmopharm.com",
    "website": "www.esmopharm.com",
    "bank1": "STERLING: 0012739278 (ESMO PHARMACEUTICAL)",
    "bank2": "UBA: 1023090218 (ESMO PHARMACEUTICAL)",
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# --- AI providers (all have FREE options) -------------------------------------
# The bot tries them in this order and moves to the next one if a provider is busy,
# out of free quota, or down - so one free limit never stops your work.
#   gemini      Google AI Studio free key          https://aistudio.google.com/apikey
#   groq        Groq free key                      https://console.groq.com/keys
#   openrouter  OpenRouter free (":free") models   https://openrouter.ai/keys
#   pollinations  NO KEY NEEDED (anonymous, rate-limited, third-party; last resort before ollama)
#   ollama      runs on YOUR computer - free forever, private, no internet needed (set OLLAMA_MODEL)
AI_PROVIDERS = [x.strip().lower() for x in
                (os.getenv("AI_PROVIDERS") or os.getenv("AI_PROVIDER") or "gemini,groq,openrouter,pollinations,ollama").split(",")
                if x.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.0-flash").split(",") if m.strip()]
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-3-27b-it:free")
# Pollinations: works WITHOUT any key (anonymous, rate-limited, third-party). A key is optional.
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "")
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "openai")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "")      # e.g. qwen2.5vl:7b  (must be a VISION model to read photos)


def provider_ready(name):
    if name == "pollinations":
        return True                      # no key needed
    return bool({"gemini": GEMINI_API_KEY, "groq": GROQ_API_KEY, "openrouter": OPENROUTER_API_KEY,
                 "ollama": OLLAMA_MODEL}.get(name))


def ready_providers():
    return [p for p in AI_PROVIDERS if provider_ready(p)]


TIMEZONE = os.getenv("TIMEZONE", "Africa/Lagos")
START_INVOICE = int(os.getenv("START_INVOICE", "735"))

# Telegram user IDs allowed to use the bot (comma separated). Empty = nobody.
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# --- hosting online -----------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")           # the SECRET key (server only - never share it)
PORT = int(os.getenv("PORT", "10000"))
# Public address of the hosted bot. Render sets RENDER_EXTERNAL_URL by itself.
BASE_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")       # random text; Telegram sends it back on every update

LOGO_CANDIDATES = ["logo.png", "logo.jpg", "logo.jpeg", "logo.webp"]
