"""
config.py — User configuration for Dict Tool.

Loads and saves a JSON config file from the user's APPDATA directory.
All keys have safe defaults — missing keys are merged from DEFAULT_CONFIG.

Usage:
    from config import load_config, ensure_config

    cfg = ensure_config()
    if cfg["pronunciation"]:
        ...
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "pronunciation": True,
    "hotkey": "ctrl+shift+d",
    "search_hotkey": "ctrl+shift+f",
    "ai_enabled": True,
    "ai_provider": "nvidia",        # "nvidia" | "local"
    "nvidia_model": "nvidia/llama-3.3-nemotron-super-49b-v1",
    "tts_engine": "pyttsx3",
    "tts_rate": 150,
    "tts_volume": 100,
    "gemini_voice_name": "Kore",
    "popup_font": "Pretendard Variable",
    "popup_font_size": 11,
    "ai_language": "ko",
    "ai_style": "detailed",
    "ai_custom_prompt": "",
    "theme": "dark",
    "history_enabled": True,
    "show_chips": True,
}

# ------------------------------------------------------------------
# Config file location
# ------------------------------------------------------------------

def _config_path() -> Path:
    """Return the config file path, respecting APPDATA on Windows."""
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "dict-tool" / "config.json"


# Exposed as a module-level property for testability (tests can monkeypatch)
CONFIG_PATH: Path = _config_path()


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def load_config(path: Path | None = None) -> dict:
    """Load config from *path* (default: CONFIG_PATH), merging defaults.

    Returns DEFAULT_CONFIG if the file does not exist or is malformed.
    Missing keys in the file are filled from DEFAULT_CONFIG.
    """
    target = path if path is not None else CONFIG_PATH
    cfg = dict(DEFAULT_CONFIG)

    if not target.exists():
        return cfg

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update(raw)
    except Exception as exc:
        logger.warning("Could not read config %s: %s — using defaults", target, exc)

    return cfg


def save_config(cfg: dict, path: Path | None = None) -> None:
    """Write *cfg* to *path* (default: CONFIG_PATH) as JSON."""
    target = path if path is not None else CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    logger.info("Config saved to %s", target)


def ensure_config(path: Path | None = None) -> dict:
    """Load config, creating the file with defaults if it doesn't exist."""
    target = path if path is not None else CONFIG_PATH
    if not target.exists():
        save_config(DEFAULT_CONFIG, path=target)
        logger.info("Created default config at %s", target)
    return load_config(path=target)
