"""Перевірка кроку workflow «Чи увімкнені GitHub Pages?» справжнім запуском.

Історія питання: сторінка результатів не з'являлася, бо GitHub Pages не увімкнені,
а створити сайт Pages може лише адміністратор репозиторію — токен workflow таких
прав не має (configure-pages з `enablement: true` віддає «Resource not accessible
by integration»). Тому тепер workflow не вдає, що може це зробити сам:

  • Pages увімкнені  → у підсумок іде адреса github.io, артефакт публікується;
  • Pages вимкнені   → у підсумок іде робоче посилання через CDN (тека results/site/)
                       і одна інструкція, як увімкнути офіційну адресу.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monitor.yml"
STEP_NAME = "Чи увімкнені GitHub Pages?"

FAKE_CURL = """#!/usr/bin/env bash
# підставний curl: код відповіді залежить від FAKE_MODE
case "$FAKE_MODE" in
  enabled) echo 200 ;;   # Pages уже увімкнені
  off)     echo 404 ;;   # Pages не увімкнені (звичайний стан нового репозиторію)
  broken)  echo 403 ;;   # токен не має доступу (напр., політика організації)
esac
"""


def _step_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["propose"]["steps"] if s.get("name") == STEP_NAME)
    return (step["run"]
            .replace("${{ secrets.GITHUB_TOKEN }}", "TOKEN")
            .replace("${{ github.repository }}", "user/repo")
            .replace("${{ vars.PAGES_ENABLED }}", ""))


def _run(tmp_path: Path, mode: str) -> tuple[str, str, subprocess.CompletedProcess]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL, encoding="utf-8")
    curl.chmod(0o755)

    github_output = tmp_path / f"out_{mode}"
    summary = tmp_path / f"summary_{mode}"
    github_output.write_text("", encoding="utf-8")
    summary.write_text("", encoding="utf-8")

    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_MODE": mode,
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "OWNER": "adanossot04-spec",
        "REPO": "Trader",
    }
    result = subprocess.run(["bash", "-c", _step_script()], env=env,
                            capture_output=True, text=True, timeout=60)
    return github_output.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8"), result


@pytest.mark.skipif(shutil.which("bash") is None, reason="потрібен bash")
def test_pages_enabled_gives_github_io_link(tmp_path):
    output, summary, result = _run(tmp_path, "enabled")
    assert result.returncode == 0
    assert "available=true" in output
    assert "https://adanossot04-spec.github.io/Trader/" in summary
    assert "cdn.jsdelivr.net" not in summary


@pytest.mark.skipif(shutil.which("bash") is None, reason="потрібен bash")
def test_pages_off_gives_working_cdn_link_and_instructions(tmp_path):
    output, summary, result = _run(tmp_path, "off")
    assert result.returncode == 0                                  # крок не валить запуск
    assert "available=false" in output
    # робоче посилання на сторінку, яка лежить у репозиторії
    assert "https://cdn.jsdelivr.net/gh/adanossot04-spec/Trader@main/results/site/index.html" in summary
    assert "raw.githack.com" in summary                            # резервне джерело
    # і чесна інструкція замість обіцянки «workflow увімкне сам»
    assert "Settings → Pages" in summary
    assert "enablement" not in summary.lower()


@pytest.mark.skipif(shutil.which("bash") is None, reason="потрібен bash")
def test_unexpected_status_does_not_break_the_run(tmp_path):
    output, summary, result = _run(tmp_path, "broken")
    assert result.returncode == 0
    assert "available=false" in output
    assert "cdn.jsdelivr.net" in summary


def test_workflow_never_promises_to_enable_pages_itself():
    """enablement: true не працює з GITHUB_TOKEN — цю пастку фіксуємо тестом."""
    text = WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "enablement" not in code
    assert "Create Pages site" not in code


def test_workflow_shape():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True]                                     # YAML читає 'on' як True
    assert "workflow_dispatch" in triggers                        # кнопка «Run workflow»
    crons = [item["cron"] for item in triggers["schedule"]]
    assert len(crons) == 4                                        # два сезони × два вікна
    assert all(cron.split()[2:5] == ["*", "*", "1-5"] for cron in crons)

    propose = workflow["jobs"]["propose"]
    steps = [s.get("name") or s.get("uses") for s in propose["steps"]]
    assert "Перевірка комплекту файлів" in steps                   # ловить неповний архів
    assert "Ворота: чи запускати саме зараз" in steps
    assert "Статичний сайт результатів" in steps
    assert "Зберегти звіт і сторінку в репозиторії (results/)" in steps
    assert propose["permissions"]["contents"] == "write"          # потрібно для коміту

    upload = next(s for s in propose["steps"] if s.get("name") == "Артефакт для сторінки результатів")
    assert "pages.outputs.available == 'true'" in upload["if"]
    deploy = workflow["jobs"]["deploy"]
    assert deploy["if"].endswith("== 'true'")                     # без Pages не падаємо
    assert deploy["continue-on-error"] is True


def test_site_is_committed_and_cdn_path_matches(tmp_path):
    """Шлях у CDN-посиланні має збігатися з тим, що комітиться в results/site/."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["propose"]["steps"]
    commit = next(s for s in steps if s.get("name") == "Зберегти звіт і сторінку в репозиторії (results/)")
    pages = next(s for s in steps if s.get("name") == STEP_NAME)
    assert "cp -r site results/site" in commit["run"]
    assert "results/site/index.html" in pages["run"]
