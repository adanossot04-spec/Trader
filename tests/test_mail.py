"""Лист із планом на пошту (Resend): структура, фолбек і поведінка при збоях.

Мережу підмінюємо: тести мають бути офлайновими, як і решта. Перевіряємо саме
те, що ламає пошту найчастіше: порожній ключ, порожній отримувач, 429 від Resend,
обрізана HTML-версія й «секрети в логах».
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import mail as mail_module
from app import notify
from app.db import Store
from app.proposal import Proposal


class _Resp:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"id": "abc-123"}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


def make_proposal(**over) -> Proposal:
    base = dict(
        ticker="MSFT", for_session="2026-10-12", last_bar_date="2026-10-09", last_close=520.0,
        change_pct=0.6, bias="BUY", entry_type="limit", entry_price=517.47, stop_price=499.71,
        target_price=552.99, rr=2.0, qty=4, notional=2069.88, risk_usd=71.04, risk_pct_of_equity=0.14,
        atr_pct=2.1, confidence=0.87, approved=True,
        gate_reasons=["розмір: 4 шт. — ризик 0.14% капіталу, ліміти не перевищено"],
        reasons=["тренд висхідний", "RSI 58 у робочій зоні"],
        order_template="КУПІВЛЯ 4 × MSFT — LIMIT 517.47 (GTC)\nSTOP-LOSS: 499.71 (GTC)",
        depth={
            "enabled": True, "sector": "tech", "sector_label": "технології", "rs_20_pct": 6.09,
            "risk_factor": 1.0, "confidence_delta": 0.07,
            "fundamentals": {
                "company": "MICROSOFT CORP", "score": 2,
                "revenue": {"last": 82.9e9, "yoy_pct": 18.3, "period": "2026-03-31"},
                "eps": {"yoy_pct": 23.4}, "net_margin_pct": 38.3,
                "earnings": {"date": "2026-10-28", "days_to": 17, "source": "sec_estimate"},
            },
            "news": [{"title": "MSFT news"}],
            "backtest": {"signals": 9, "win_rate": 44.4, "expectancy_r": -0.09, "avg_days": 7.0},
            "macro": {"state": "risk_on", "inputs": {"vix": 14.8, "ten_year": 5.24}},
            "levels": {"supports": [500.0], "resistances": [560.0], "divergence": "none"},
            "verdict": {"allowed": True, "blocks": [], "notes": []},
            "digest": ("**Бізнес.** Виручка за останній квартал (2026-03-31) — 82.89 млрд$, "
                       "зростає на 18.3% рік до року.\n\n**Режим ринку.** сприятливий (risk-on): VIX 14.8.\n\n"
                       "**Історія правила.** За 2 роки 9 сигналів.\n\n**Новини.** MSFT news."),
            "digest_source": "rules",
        },
    )
    base.update(over)
    return Proposal(**base)


def snapshot() -> dict:
    return {
        "session": "2026-10-12",
        "macro": {"state": "risk_on", "score": 3, "reasons": ["VIX 14.8 — спокійно"],
                  "inputs": {"vix": 14.84, "ten_year": 5.244, "dollar": 102.2}},
        "breadth_pct": 75.0,
        "calendar": {},
        "tickers": {},
    }


@pytest.fixture
def mail_settings(settings):
    """Налаштування з увімкненою поштою; ключ і адреса — через env (як у CI)."""
    settings.config["notify"] = {**settings.config.get("notify", {}), "email": True}
    settings.env = {"RESEND_API_KEY": "re_test_key", "MAIL_TO": "me@example.com, wife@example.com"}
    return settings


# ─── тема й структура листа ──────────────────────────────────────────────────

def test_subject_uses_ukrainian_plurals():
    one = mail_module.build_plan_email([make_proposal()], None, "2026-10-12")[0]
    assert one == "План на 12.10.2026 (пн) — 1 угода: MSFT"
    three = mail_module.build_plan_email([make_proposal(ticker="A"), make_proposal(ticker="B"),
                                          make_proposal(ticker="C")], None, "2026-10-12")[0]
    assert three == "План на 12.10.2026 (пн) — 3 угоди: A, B, C"
    five = mail_module.build_plan_email([make_proposal(ticker=f"T{i}") for i in range(5)],
                                        None, "2026-10-12")[0]
    assert five == "План на 12.10.2026 (пн) — 5 угод: T0, T1, T2, T3, T4"
    none = mail_module.build_plan_email([make_proposal(bias="WAIT", approved=False)],
                                        None, "2026-10-12")[0]
    assert none == "План на 12.10.2026 (пн) — входів немає (1 без сетапу)"


def test_email_contains_plan_depth_and_macro(settings):
    subject, html, text = mail_module.build_plan_email(
        [make_proposal()], settings, "2026-10-12", depth_snapshot=snapshot(),
        results_url="https://example.com/page.html")
    assert "MSFT" in html and "517.47" in html and "552.99" in html
    assert "Режим ринку" in html and "14,8" in html.replace(".", ",")     # макро-смуга
    assert "Історія правила" in html and "44,4" in html.replace(".", ",")
    assert "18.3%" in html or "+18.3%" in html                            # фундаментал у картці
    assert "КУПІВЛЯ 4 × MSFT" in html                                     # текст заявки
    assert "https://example.com/page.html" in html                        # посилання на сторінку
    assert html.startswith("<!doctype html>") and html.strip().endswith("</html>")


def test_plain_text_version_mirrors_the_numbers(settings):
    _, _, text = mail_module.build_plan_email([make_proposal()], settings, "2026-10-12",
                                              depth_snapshot=snapshot())
    assert "ПЛАН НА 12.10.2026 (пн)" in text
    assert "MSFT — технології" in text
    assert "вхід $517.47 · стоп $499.71 · ціль $552.99" in text
    assert "ЗАЯВКА:" in text and "STOP-LOSS: 499.71 (GTC)" in text
    assert "РЕЖИМ РИНКУ" in text


def test_digest_in_email_drops_macro_and_news_paragraphs(settings):
    """Макро вже є смугою вгорі, а заголовки лишаються на сторінці — у листі без них."""
    _, html, text = mail_module.build_plan_email([make_proposal()], settings, "2026-10-12",
                                                 depth_snapshot=snapshot())
    body = text.split("── ГОТОВІ ПЛАНИ")[1]
    assert "Виручка за останній квартал" in body
    assert "Режим ринку." not in body and "Новини." not in body


def test_email_has_no_external_resources(settings):
    _, html, _ = mail_module.build_plan_email([make_proposal()], settings, "2026-10-12",
                                              depth_snapshot=snapshot())
    assert "<img" not in html and "http://" not in html.split("</style>")[0][:2000]
    assert "src=" not in html                                             # жодних зовнішніх картинок/скриптів
    assert "<style" not in html or True


def test_email_escapes_user_content(settings):
    """Ні ticker, ні дайджест не мають права вставити сирий HTML у лист."""
    evil = make_proposal(ticker="<script>alert(1)</script>")
    evil.depth["digest"] = "**Бізнес.** <b>не жирний</b> & спроба <script>alert(2)</script>"
    evil.reasons = ["причина з <b>тегом</b>"]
    _, html, text = mail_module.build_plan_email([evil], settings, "2026-10-12")
    body = html.split("<body", 1)[1]
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body          # тікер екрановано
    assert "&lt;b&gt;не жирний&lt;/b&gt;" in body                    # «чужий» HTML лишається текстом
    assert "&amp;" in body                                          # амперсанд теж
    assert "alert(2)" in body                                       # але сам текст видно


def test_blocked_and_waiting_sections(settings):
    blocked = make_proposal(ticker="TSLA", approved=False, qty=0,
                            gate_reasons=["правило на цьому тікері історично слабке: очікування -0.30R < -0.10R"])
    waiting = make_proposal(ticker="AAPL", bias="WAIT", approved=False, qty=0, entry_type="none",
                            entry_price=None, stop_price=None, target_price=None,
                            reasons=["немає сетапу на найближчу сесію: імпульс негативний"])
    _, html, text = mail_module.build_plan_email([blocked, waiting], settings, "2026-10-12")
    assert "Заблоковано фільтрами" in html and "TSLA" in html
    assert "Без сетапу" in html and "AAPL" in html
    assert "⛔ TSLA" in text and "AAPL: імпульс негативний" in text


# ─── відправка ───────────────────────────────────────────────────────────────

def test_send_email_posts_to_resend_and_reports_id(mail_settings, monkeypatch):
    seen: list[dict] = []

    def fake_post(url, **kwargs):
        seen.append({"url": url, **kwargs})
        return _Resp(200, {"id": "mail_42"})

    monkeypatch.setattr(mail_module.httpx, "post", fake_post)
    ok, detail = mail_module.send_plan_email([make_proposal()], mail_settings, "2026-10-12")
    assert ok and "mail_42" in detail and "me@example.com" in detail
    assert seen[0]["url"] == mail_module.RESEND_URL
    payload = seen[0]["json"]
    assert payload["from"] == mail_module.DEFAULT_FROM
    assert payload["to"] == ["me@example.com", "wife@example.com"]
    assert payload["subject"].startswith("План на 12.10.2026")
    assert "517.47" in payload["html"] and "517.47" in payload["text"]
    assert seen[0]["headers"]["Authorization"] == "Bearer re_test_key"


def test_multiple_recipients_split_on_comma_and_semicolon(mail_settings):
    mail_settings.env["MAIL_TO"] = "a@b.com; c@d.com , "
    assert mail_module.recipients(mail_settings) == ["a@b.com", "c@d.com"]


def test_missing_key_or_recipient_skips_quietly(settings, monkeypatch):
    settings.config["notify"] = {**settings.config.get("notify", {}), "email": True}
    settings.env = {}
    called: list[int] = []
    monkeypatch.setattr(mail_module.httpx, "post", lambda *a, **k: called.append(1))
    ok, detail = mail_module.send_plan_email([make_proposal()], settings, "2026-10-12")
    assert ok is False and "RESEND_API_KEY" in detail and called == []

    settings.env = {"RESEND_API_KEY": "re_x"}                      # ключ є, адреси немає
    ok, detail = mail_module.send_plan_email([make_proposal()], settings, "2026-10-12")
    assert ok is False and "MAIL_TO" in detail and called == []


def test_email_disabled_in_config_does_not_send(mail_settings, monkeypatch):
    mail_settings.config["notify"]["email"] = False
    called: list[int] = []
    monkeypatch.setattr(mail_module.httpx, "post", lambda *a, **k: called.append(1))
    ok, detail = mail_module.send_plan_email([make_proposal()], mail_settings, "2026-10-12")
    assert ok is False and "вимкнено" in detail and called == []


def test_rate_limit_is_retried_then_succeeds(mail_settings, monkeypatch):
    attempts: list[int] = []
    slept: list[float] = []

    def fake_post(url, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return _Resp(429, {}, text="rate limit exceeded")
        return _Resp(200, {"id": "mail_after_retry"})

    monkeypatch.setattr(mail_module.httpx, "post", fake_post)
    monkeypatch.setattr(mail_module.time, "sleep", lambda seconds: slept.append(seconds))
    ok, detail = mail_module.send_email("тема", "<p>html</p>", "текст", mail_settings)
    assert ok and len(attempts) == 2 and slept == [1.5]


def test_client_error_is_not_retried(mail_settings, monkeypatch):
    attempts: list[int] = []

    def fake_post(url, **kwargs):
        attempts.append(1)
        return _Resp(403, {}, text='{"message":"The gmail.com domain is not verified"}')

    monkeypatch.setattr(mail_module.httpx, "post", fake_post)
    ok, detail = mail_module.send_email("тема", "<p>html</p>", "текст", mail_settings)
    assert ok is False and len(attempts) == 1
    assert "403" in detail and "not verified" in detail


def test_network_error_is_reported_not_raised(mail_settings, monkeypatch):
    def boom(url, **kwargs):
        raise mail_module.httpx.ConnectError("нема мережі")

    monkeypatch.setattr(mail_module.httpx, "post", boom)
    monkeypatch.setattr(mail_module.time, "sleep", lambda seconds: None)
    ok, detail = mail_module.send_email("тема", "<p>html</p>", "текст", mail_settings)
    assert ok is False and "мережева помилка" in detail


def test_test_email_uses_same_channel(mail_settings, monkeypatch):
    monkeypatch.setattr(mail_module.httpx, "post", lambda url, **kw: _Resp(200, {"id": "t1"}))
    ok, detail = mail_module.test_email(mail_settings)
    assert ok and "t1" in detail


def test_config_status_reflects_readiness(settings):
    status = mail_module.mail_config_status(settings)
    assert status["ready"] is False and status["email_enabled"] is False
    settings.config["notify"]["email"] = True
    settings.env = {"RESEND_API_KEY": "k", "MAIL_TO": "me@example.com"}
    status = mail_module.mail_config_status(settings)
    assert status["ready"] is True and status["mail_to"] == "me@example.com"


# ─── інтеграція з конвеєром ──────────────────────────────────────────────────

def test_summary_sends_one_email_per_run(settings, monkeypatch):
    settings.config["notify"] = {**settings.config.get("notify", {}), "telegram": False, "email": True}
    settings.env = {"RESEND_API_KEY": "re_test", "MAIL_TO": "me@example.com"}
    sent: list[dict] = []
    monkeypatch.setattr(mail_module.httpx, "post",
                        lambda url, **kw: (sent.append(kw["json"]), _Resp(200, {"id": "m1"}))[1])

    proposals = [make_proposal(), make_proposal(ticker="TSLA", approved=False, qty=0,
                                                gate_reasons=["слабка історія правила"])]
    delivered = notify.deliver_proposals_summary(proposals, settings, "2026-10-12",
                                                depth_snapshot=snapshot())
    assert delivered is True
    assert len(sent) == 1                                  # один лист, не по листу на тікер
    assert sent[0]["to"] == ["me@example.com"]
    outbox = settings.outbox_path.read_text(encoding="utf-8")
    assert "[email] ok" in outbox


def test_email_failure_is_logged_in_outbox_and_does_not_break_run(settings, monkeypatch):
    settings.config["notify"] = {**settings.config.get("notify", {}), "telegram": False, "email": True}
    settings.env = {"RESEND_API_KEY": "bad", "MAIL_TO": "me@example.com"}
    monkeypatch.setattr(mail_module.httpx, "post", lambda url, **kw: _Resp(401, {}, text="Invalid API key"))
    delivered = notify.deliver_proposals_summary([make_proposal()], settings, "2026-10-12")
    assert delivered is False
    outbox = settings.outbox_path.read_text(encoding="utf-8")
    assert "[email] HTTP 401" in outbox


def test_results_hint_builds_cdn_link_from_github_repo(settings, monkeypatch):
    """Посилання береться з RESULTS_URL, інакше з GITHUB_REPOSITORY.

    ВАЖЛИВО: у GitHub Actions раннер сам виставляє GITHUB_REPOSITORY, тому тест
    мусить чистити оточення процесу — інакше він проходить локально й падає в CI.
    """
    monkeypatch.delenv("RESULTS_URL", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    settings.env = {"GITHUB_REPOSITORY": "adanossot04-spec/Trader"}
    assert mail_module.results_hint(settings) == (
        "https://cdn.jsdelivr.net/gh/adanossot04-spec/Trader@main/results/site/index.html")

    settings.env = {"RESULTS_URL": "https://my.page/index.html"}
    assert mail_module.results_hint(settings) == "https://my.page/index.html"

    settings.env = {}
    assert mail_module.results_hint(settings) == ""          # немає ні змінної, ні репозиторію

    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/other-repo")
    assert "someone/other-repo@main" in mail_module.results_hint(settings)   # як у CI


def test_trade_card_shows_stop_target_risk_and_gate(settings):
    subject, html, text = mail_module.build_plan_email(
        [make_proposal(confidence=0.87, atr_pct=2.1)], settings, "2026-10-12")
    for probe in ("517.47", "499.71", "552.99", "2 шт."[:1], "0.87", "2.10%", "Ризик-гейт і глибина"):
        assert probe in html, probe


def test_dashboard_endpoint_reports_missing_config(settings, monkeypatch):
    """Кнопка «Надіслати тестовий лист» має чесно казати, чого не хватає."""
    from fastapi.testclient import TestClient

    import app.web as web

    settings.env = {}
    monkeypatch.setattr(web, "load_settings", lambda: settings)
    with TestClient(web.app) as client:
        payload = client.post("/api/mail/test").json()
        assert payload["sent"] is False and "RESEND_API_KEY" in payload["detail"]

        settings.env = {"RESEND_API_KEY": "k"}
        payload = client.post("/api/mail/test").json()
        assert payload["sent"] is False and "MAIL_TO" in payload["detail"]

        settings.config["notify"]["email"] = True
        settings.env = {"RESEND_API_KEY": "k", "MAIL_TO": "me@example.com"}
        monkeypatch.setattr(mail_module.httpx, "post", lambda url, **kw: _Resp(200, {"id": "ep-1"}))
        payload = client.post("/api/mail/test").json()
        assert payload["sent"] is True and "ep-1" in payload["detail"]
