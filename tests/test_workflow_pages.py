"""Перевірка кроку workflow «Чи доступні GitHub Pages?» справжнім запуском.

Саме через цей крок у користувача не з'явилася сторінка результатів: у приватному
репозиторії на безкоштовному тарифі Pages недоступні. Тут ми підставляємо
підставний curl і перевіряємо всі три сценарії — щоб запуск ніколи не падав
через відсутність Pages і завжди писав зрозумілу причину.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monitor.yml"
STEP_NAME = "Чи доступні GitHub Pages? (за потреби — увімкнути)"

FAKE_CURL = """#!/usr/bin/env bash
case "$FAKE_MODE" in
  public)  if [[ "$*" == *"-X POST"* ]]; then echo 201; else echo 404; fi ;;
  private) if [[ "$*" == *"-X POST"* ]]; then echo 403; else echo 404; fi ;;
  enabled) echo 200 ;;
esac
"""


def _step_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["propose"]["steps"] if s.get("name") == STEP_NAME)
    return (step["run"]
            .replace("${{ secrets.GITHUB_TOKEN }}", "TOKEN")
            .replace("${{ github.repository }}", "user/repo")
            .replace("${{ vars.PAGES_ENABLED }}", ""))


@pytest.mark.skipif(shutil.which("bash") is None, reason="потрібен bash")
@pytest.mark.parametrize(
    "mode, expected_available, expect_hint",
    [
        ("enabled", "true", False),    # Pages уже увімкнені
        ("public", "true", False),     # увімкнули автоматично через API
        ("private", "false", True),    # приватний репо: підказка, але без падіння
    ],
)
def test_pages_step(tmp_path, mode, expected_available, expect_hint):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL, encoding="utf-8")
    curl.chmod(0o755)

    github_output = tmp_path / "out"
    summary = tmp_path / "summary"
    github_output.write_text("", encoding="utf-8")
    summary.write_text("", encoding="utf-8")

    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_MODE": mode,
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    result = subprocess.run(["bash", "-c", _step_script()], env=env,
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr          # крок не має валити запуск
    assert f"available={expected_available}" in github_output.read_text(encoding="utf-8")
    text = summary.read_text(encoding="utf-8")
    assert ("Сторінки результатів цього разу немає" in text) is expect_hint
    if expect_hint:
        assert "PAGES_ENABLED" in text                    # підказка, як увімкнути


def test_workflow_triggers_and_guards_are_in_place():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True]                             # YAML читає 'on' як True
    assert "workflow_dispatch" in triggers                # кнопка «Run workflow»
    crons = [item["cron"] for item in triggers["schedule"]]
    assert len(crons) == 4                                # два сезони × два вікна
    assert all(cron.split()[2:5] == ["*", "*", "1-5"] for cron in crons)

    steps = [s.get("name") or s.get("uses") for s in workflow["jobs"]["propose"]["steps"]]
    assert "Перевірка комплекту файлів" in steps           # ловить неповний архів
    assert "Ворота: чи запускати саме зараз" in steps
    assert "Статичний сайт результатів" in steps
    assert workflow["jobs"]["deploy"]["if"].endswith("== 'true'")   # без Pages не падаємо
    assert workflow["jobs"]["deploy"].get("continue-on-error") is True or \
        "continue-on-error" in workflow["jobs"]["deploy"]
