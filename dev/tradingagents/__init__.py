"""DEV-ЗАГЛУШКА. Не для реальної роботи.

Імітує мінімальний поверх TradingAgents, щоб перевірити інтеграційний шлях
(engine.py → TradingAgentsEngine) без встановлення справжнього пакета й без ключів.

Як користуватися: у config.yaml поставте engine.mode: real і в tests — див.
tests/test_engine_real_path.py. Справжній TradingAgents має пріоритет у продакшн-запуску,
бо dev/ не додається в sys.path автоматично.
"""

__version__ = "0.0.0-dev-stub"
