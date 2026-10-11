"""Журнал рішень на SQLite. Усе, що система рекомендувала і що з цього вийшло."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import RunRecord, Signal

log = logging.getLogger("assistant.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    analysis_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT 'mock',
    status TEXT NOT NULL DEFAULT 'pending',
    action TEXT, rating TEXT, confidence REAL, rationale TEXT, reference_price REAL,
    approved INTEGER, reasons TEXT DEFAULT '', quantity INTEGER, notional REAL,
    stop_loss_price REAL, take_profit_price REAL,
    kill_switch INTEGER DEFAULT 0,
    report_path TEXT, report_html TEXT,
    error TEXT, duration_sec REAL,
    notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_ticker_date ON runs(ticker, analysis_date);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);

CREATE TABLE IF NOT EXISTS outcomes (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    settled_at TEXT NOT NULL,
    holding_days INTEGER,
    return_pct REAL,
    alpha_pct REAL,
    benchmark TEXT,
    reflection TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    for_session TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_bar_date TEXT, last_close REAL, change_pct REAL,
    bias TEXT, entry_type TEXT, entry_price REAL, stop_price REAL, target_price REAL, rr REAL,
    qty INTEGER, notional REAL, risk_usd REAL, risk_pct_of_equity REAL, atr_pct REAL,
    confidence REAL, approved INTEGER DEFAULT 0, gate_reasons TEXT DEFAULT '', reasons TEXT DEFAULT '',
    invalidation TEXT DEFAULT '', note TEXT DEFAULT '', note_source TEXT DEFAULT 'rules',
    llm_error TEXT, order_template TEXT DEFAULT '',
    data_source TEXT DEFAULT '', synthetic INTEGER DEFAULT 0, warnings TEXT DEFAULT '',
    premarket_price REAL, premarket_gap_pct REAL, premarket_as_of TEXT, premarket_note TEXT DEFAULT '',
    technical TEXT DEFAULT '{}',
    depth TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_proposals_session ON proposals(for_session);
CREATE INDEX IF NOT EXISTS idx_proposals_ticker ON proposals(ticker, for_session);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals(created_at);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
        self._migrate()

    # колонки, додані після першого релізу: дописуємо в наявні бази
    EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
        "proposals": {
            "premarket_price": "REAL",
            "premarket_gap_pct": "REAL",
            "premarket_as_of": "TEXT",
            "premarket_note": "TEXT DEFAULT ''",
            "depth": "TEXT DEFAULT '{}'",
        }
    }

    def _migrate(self) -> None:
        with self.connect() as con:
            for table, columns in self.EXPECTED_COLUMNS.items():
                existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns.items():
                    if name not in existing:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                        log.info("міграція: додано %s.%s", table, name)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    # ── runs ──────────────────────────────────────────────────────────────
    def insert_run(self, record: RunRecord) -> int:
        payload = record.model_dump()
        payload["approved"] = None if payload["approved"] is None else int(payload["approved"])
        payload["kill_switch"] = int(payload["kill_switch"])
        payload["notified"] = int(payload["notified"])
        cols = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        with self.connect() as con:
            cur = con.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})", list(payload.values()))
            return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in ("approved", "kill_switch", "notified"):
            if key in fields and fields[key] is not None:
                fields[key] = int(fields[key])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as con:
            con.execute(f"UPDATE runs SET {assignments} WHERE id = ?", [*fields.values(), run_id])

    def _row_to_record(self, row: sqlite3.Row) -> RunRecord:
        data = dict(row)
        if data.get("approved") is not None:
            data["approved"] = bool(data["approved"])
        data["kill_switch"] = bool(data.get("kill_switch"))
        data["notified"] = bool(data.get("notified"))
        return RunRecord(**data)

    def get_run(self, run_id: int) -> RunRecord | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def recent_runs(self, limit: int = 50, ticker: str | None = None) -> list[RunRecord]:
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if ticker:
            sql += " WHERE ticker = ?"
            params.append(ticker.upper())
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def runs_for_day(self, day: date) -> list[RunRecord]:
        prefix = day.isoformat()
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM runs WHERE substr(created_at, 1, 10) = ? ORDER BY id DESC", (prefix,)
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def approved_runs_since(self, days: int) -> list[RunRecord]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM runs WHERE approved = 1 AND created_at >= ? ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self.connect() as con:
            total = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            ok = con.execute("SELECT COUNT(*) FROM runs WHERE status='ok'").fetchone()[0]
            err = con.execute("SELECT COUNT(*) FROM runs WHERE status='error'").fetchone()[0]
            approved = con.execute("SELECT COUNT(*) FROM runs WHERE approved=1").fetchone()[0]
            settled = con.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
            avg_alpha = con.execute("SELECT AVG(alpha_pct) FROM outcomes").fetchone()[0]
        return {
            "runs_total": total,
            "runs_ok": ok,
            "runs_error": err,
            "approved": approved,
            "settled": settled,
            "avg_alpha_pct": round(avg_alpha, 2) if avg_alpha is not None else None,
        }

    def save_report(self, run_id: int, markdown: str, html: str | None = None) -> Path:
        folder = self.path.parent / "reports" / f"run-{run_id}"
        folder.mkdir(parents=True, exist_ok=True)
        md_path = folder / "report.md"
        md_path.write_text(markdown, encoding="utf-8")
        html_path = None
        if html:
            html_path = folder / "report.html"
            html_path.write_text(html, encoding="utf-8")
        self.update_run(run_id, report_path=str(md_path), report_html=str(html_path) if html_path else None)
        return md_path


    # ── proposals ─────────────────────────────────────────────────────────
    def insert_proposal(self, proposal) -> int:
        payload = {
            "ticker": proposal.ticker,
            "for_session": proposal.for_session,
            "created_at": proposal.created_at,
            "last_bar_date": proposal.last_bar_date,
            "last_close": proposal.last_close,
            "change_pct": proposal.change_pct,
            "bias": proposal.bias,
            "entry_type": proposal.entry_type,
            "entry_price": proposal.entry_price,
            "stop_price": proposal.stop_price,
            "target_price": proposal.target_price,
            "rr": proposal.rr,
            "qty": proposal.qty,
            "notional": proposal.notional,
            "risk_usd": proposal.risk_usd,
            "risk_pct_of_equity": proposal.risk_pct_of_equity,
            "atr_pct": proposal.atr_pct,
            "confidence": proposal.confidence,
            "approved": int(proposal.approved),
            "gate_reasons": "\n".join(proposal.gate_reasons or []),
            "reasons": "\n".join(proposal.reasons or []),
            "invalidation": proposal.invalidation,
            "note": proposal.note,
            "note_source": proposal.note_source,
            "llm_error": proposal.llm_error,
            "order_template": proposal.order_template,
            "data_source": proposal.data_source,
            "synthetic": int(proposal.synthetic),
            "warnings": "\n".join(proposal.warnings or []),
            "premarket_price": proposal.premarket_price,
            "premarket_gap_pct": proposal.premarket_gap_pct,
            "premarket_as_of": proposal.premarket_as_of,
            "premarket_note": proposal.premarket_note,
            "technical": json.dumps(proposal.technical, ensure_ascii=False),
            "depth": json.dumps(getattr(proposal, "depth", {}) or {}, ensure_ascii=False),
        }
        cols = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        with self.connect() as con:
            cur = con.execute(f"INSERT INTO proposals ({cols}) VALUES ({marks})", list(payload.values()))
            return int(cur.lastrowid)

    def _row_to_proposal(self, row: sqlite3.Row):
        from .proposal import Proposal

        data = dict(row)
        data["approved"] = bool(data.get("approved"))
        data["synthetic"] = bool(data.get("synthetic"))
        data["gate_reasons"] = [r for r in (data.get("gate_reasons") or "").split("\n") if r.strip()]
        data["reasons"] = [r for r in (data.get("reasons") or "").split("\n") if r.strip()]
        data["warnings"] = [w for w in (data.get("warnings") or "").split("\n") if w.strip()]
        try:
            data["technical"] = json.loads(data.get("technical") or "{}")
        except json.JSONDecodeError:
            data["technical"] = {}
        try:
            data["depth"] = json.loads(data.get("depth") or "{}")
        except json.JSONDecodeError:
            data["depth"] = {}
        return Proposal(**data)

    def approved_proposals_since(self, days: int):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM proposals WHERE approved = 1 AND created_at >= ? ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        return [self._row_to_proposal(r) for r in rows]

    def get_proposal(self, proposal_id: int):
        with self.connect() as con:
            row = con.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return self._row_to_proposal(row) if row else None

    def recent_proposals(self, limit: int = 50, ticker: str | None = None, session: str | None = None):
        sql = "SELECT * FROM proposals"
        params: list[Any] = []
        conds: list[str] = []
        if ticker:
            conds.append("ticker = ?")
            params.append(ticker.upper())
        if session:
            conds.append("for_session = ?")
            params.append(session)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._row_to_proposal(r) for r in rows]

    def proposals_for_session(self, session: str):
        """Остання пропозиція по кожному тікеру для конкретної сесії."""
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT p.* FROM proposals p
                JOIN (SELECT ticker, MAX(id) AS mid FROM proposals WHERE for_session = ? GROUP BY ticker) latest
                  ON latest.mid = p.id
                ORDER BY p.ticker
                """,
                (session,),
            ).fetchall()
        return [self._row_to_proposal(r) for r in rows]

    def latest_proposal_session(self) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT MAX(for_session) AS s FROM proposals").fetchone()
        return row["s"] if row and row["s"] else None

    def proposal_stats(self) -> dict[str, Any]:
        with self.connect() as con:
            total = con.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
            approved = con.execute("SELECT COUNT(*) FROM proposals WHERE approved = 1").fetchone()[0]
            pending = con.execute("SELECT COUNT(*) FROM proposals WHERE bias = 'WAIT'").fetchone()[0]
            last = con.execute("SELECT MAX(created_at) FROM proposals").fetchone()[0]
        return {"proposals_total": total, "proposals_approved": approved, "proposals_wait": pending, "proposals_last": last}

    # ── watchlist ─────────────────────────────────────────────────────────
    def seed_watchlist(self, tickers: list[str]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect() as con:
            for t in tickers:
                con.execute(
                    "INSERT OR IGNORE INTO watchlist (ticker, added_at) VALUES (?, ?)",
                    (t.upper(), now),
                )

    def watchlist(self) -> list[str]:
        with self.connect() as con:
            rows = con.execute("SELECT ticker FROM watchlist ORDER BY ticker").fetchall()
        return [r["ticker"] for r in rows]

    def add_ticker(self, ticker: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO watchlist (ticker, added_at) VALUES (?, ?)",
                (ticker.upper(), now),
            )

    def remove_ticker(self, ticker: str) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))


def signal_from_record(record: RunRecord) -> Signal:
    return Signal(
        ticker=record.ticker,
        analysis_date=record.analysis_date,
        action=record.action or "HOLD",  # type: ignore[arg-type]
        rating=record.rating,
        confidence=record.confidence or 0.0,
        rationale=record.rationale or "",
        reference_price=record.reference_price,
    )
