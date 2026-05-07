"""
settings_preview.py — Local preview settings (SQLite, no Redis, no Celery)
Used only for the live demo run. Not for production.
"""
from .settings import *

DEBUG = True
SECRET_KEY = "preview-secret-key-not-for-production"
ALLOWED_HOSTS = ["*"]

# Use SQLite instead of PostgreSQL for the preview
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "preview_db.sqlite3",
    }
}

# Use in-memory channel layer (no Redis needed)
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

# Disable Celery result backend DB requirement
CELERY_RESULT_BACKEND = "cache"
CELERY_CACHE_BACKEND = "memory"

# Static files served directly by Django in debug mode
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"

# Dummy trading credentials (preview only)
ALPACA_API_KEY = "preview"
ALPACA_SECRET_KEY = "preview"
ALPACA_PAPER = True
QUIVER_API_KEY = "preview"
OPENAI_API_KEY = "preview"
LLM_PROVIDER = "openai"
DEEP_MODEL = "gpt-4o"
QUICK_MODEL = "gpt-4o-mini"
TRAIL_PCT = 0.05
WHEEL_DTE = 30
