"""Необов'язковий LLM-коментар до пропозиції. Працює з БЕЗКОШТОВНИМИ ключами.

Провайдери (config.yaml → llm.provider):
  none               — без LLM: коментар генерується правилами (нуль витрат, за замовчуванням)
  groq               — безкоштовний тариф, дуже швидко            → GROQ_API_KEY
  google             — Gemini з безкоштовним тарифом AI Studio    → GOOGLE_API_KEY
  openrouter         — безкоштовні моделі з суфіксом :free        → OPENROUTER_API_KEY
  ollama             — локально, без ключа й без інтернету
  openai/openai_compatible/deepseek/xai/mistral — сумісні API (ключ відповідного провайдера)
  anthropic          — окремий формат /v1/messages

Важливо: LLM пише лише текст-пояснення. Рівні, розмір позиції та вердикт ризик-гейта
рахує код — модель не може їх змінити.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger("assistant.llm")

OPENAI_COMPATIBLE = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct:free"),
    "ollama": ("http://localhost:11434/v1", None, "llama3.1"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-5-mini"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY", "grok-4-fast"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-latest"),
    "openai_compatible": (None, "OPENAI_COMPATIBLE_API_KEY", None),
}

FREE_PROVIDERS = ("none", "groq", "google", "openrouter", "ollama")


def provider_status(settings) -> dict[str, Any]:
    cfg = settings.llm_cfg
    provider = str(cfg.get("provider", "none")).lower()
    info: dict[str, Any] = {
        "provider": provider,
        "model": cfg.get("model"),
        "is_free_tier": provider in FREE_PROVIDERS,
        "key_present": True,
        "hint": "",
    }
    if provider == "google":
        info["key_present"] = bool(settings.env_or("GOOGLE_API_KEY") or settings.env_or("GEMINI_API_KEY"))
        info["hint"] = "безкоштовний ключ: aistudio.google.com/apikey → GOOGLE_API_KEY"
    elif provider in OPENAI_COMPATIBLE:
        _, env_name, default_model = OPENAI_COMPATIBLE[provider]
        if env_name:
            info["key_present"] = bool(settings.env_or(env_name))
        info["model"] = info["model"] or default_model
        if provider == "groq":
            info["hint"] = "безкоштовний ключ: console.groq.com/keys → GROQ_API_KEY"
        elif provider == "openrouter":
            info["hint"] = "безкоштовні моделі з :free → OPENROUTER_API_KEY"
        elif provider == "ollama":
            info["hint"] = "локальна модель, ключ не потрібен: ollama pull llama3.1"
    elif provider == "none":
        info["hint"] = "LLM вимкнено: коментар до пропозиції складає код із цифр (0 витрат)"
    return info


def _prompt(payload: dict[str, Any]) -> str:
    return (
        "Ти — асистент трейдера. Нижче фактичні дані добового графіка акції (числа вже перевірені, "
        "нічого не вигадуй і не додавай нових цін).\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "Напиши українською 4-6 коротких речень: (1) сценарій на наступну сесію і чому саме такий; "
        "(2) два-три конкретні підтвердження з наведених цифр; (3) за якої умови сценарій скасовується. "
        "Без дисклеймерів, без закликів купувати, без згадок про «консультацію з фінансовим радником»."
    )


def _openai_compatible(provider: str, prompt: str, cfg: dict[str, Any], settings) -> str:
    base, env_name, default_model = OPENAI_COMPATIBLE[provider]
    base = cfg.get("backend_url") or base
    if not base:
        raise RuntimeError(f"{provider}: не вказано backend_url")
    headers = {"Content-Type": "application/json"}
    key = (settings.env_or(env_name) if env_name else "") or ""
    if key:
        headers["Authorization"] = f"Bearer {key.strip()}"
    model = cfg.get("model") or default_model
    if not model:
        raise RuntimeError(f"{provider}: не вказано модель")
    response = httpx.post(
        f"{base.rstrip('/')}/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(cfg.get("temperature", 0.3)),
            "max_tokens": int(cfg.get("max_tokens", 400)),
        },
        timeout=float(cfg.get("timeout_sec", 60)),
    )
    response.raise_for_status()
    data = response.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def _google(prompt: str, cfg: dict[str, Any], settings) -> str:
    key = (settings.env_or("GOOGLE_API_KEY") or settings.env_or("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("google: немає GOOGLE_API_KEY")
    model = cfg.get("model") or "gemini-2.5-flash"
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": float(cfg.get("temperature", 0.3)), "maxOutputTokens": 500},
        },
        timeout=float(cfg.get("timeout_sec", 60)),
    )
    response.raise_for_status()
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"google: порожня відповідь {str(data)[:200]}")
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def _anthropic(prompt: str, cfg: dict[str, Any], settings) -> str:
    key = (settings.env_or("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("anthropic: немає ANTHROPIC_API_KEY")
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json={
            "model": cfg.get("model") or "claude-sonnet-4-5",
            "max_tokens": int(cfg.get("max_tokens", 500)),
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=float(cfg.get("timeout_sec", 60)),
    )
    response.raise_for_status()
    blocks = response.json().get("content") or []
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def narrate(payload: dict[str, Any], settings) -> tuple[str | None, str | None]:
    """Повертає (текст, помилка). Будь-який збій не ламає пропозицію — просто не буде коментаря."""
    cfg = settings.llm_cfg
    provider = str(cfg.get("provider", "none")).lower()
    if provider in ("none", "", "disabled"):
        return None, None

    prompt = _prompt(payload)
    try:
        if provider == "google":
            text = _google(prompt, cfg, settings)
        elif provider == "anthropic":
            text = _anthropic(prompt, cfg, settings)
        elif provider in OPENAI_COMPATIBLE:
            text = _openai_compatible(provider, prompt, cfg, settings)
        else:
            return None, f"невідомий провайдер llm: {provider}"
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM-коментар не вдався (%s): %s", provider, exc)
        return None, f"{type(exc).__name__}: {exc}"[:400]

    return (text or None), None
