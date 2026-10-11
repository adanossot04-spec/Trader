"""Мінімальна заглушка DEFAULT_CONFIG (структура як у справжнього пакета)."""

DEFAULT_CONFIG = {
    "llm_provider": "openai",
    "quick_think_llm": "gpt-6-luna",
    "deep_think_llm": "gpt-6-sol",
    "backend_url": None,
    "max_debate_rounds": 1,
    "max_risk_rounds": 1,
    "online_tools": False,
    "output_language": "English",
    "temperature": 0.2,
    "results_dir": "results",
    "data_dir": "data",
    "checkpoint_enabled": False,
}
