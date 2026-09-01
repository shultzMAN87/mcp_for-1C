#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-4. Валидация корпуса techdocs/ в общем прогоне тестов.

Правки в run_all_tests.py не потребовалось: он находит наборы сам по маске
`tests_*.py`, и каталог scripts/ у него в SUITE_ROOTS с захода HYG-2. Набор,
дописанный в захардкоженный список, рано или поздно из него выпал бы — ровно
тот класс расхождений, который заход 4 и разбирал.

Проверок две, и они про разное:

  1. Корпус проходит валидатор. Ненулевой код — провал набора.
  2. Валидатор работает при остановленной Neo4j. Это R13: если проверка
     начнёт ходить в граф, весь прогон станет зависеть от поднятой базы, и
     первый же локальный запуск без docker compose даст красное поле.

Запуск: python scripts\\tests_docs_validate.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / "scripts" / "docs_validate.py"
CORPUS = ROOT / "techdocs"


def _run(*extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *extra],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
        timeout=120,
    )


@unittest.skipUnless(CORPUS.is_dir(), "корпус techdocs/ ещё не заведён")
class TestCorpusValid(unittest.TestCase):

    def test_validator_present(self):
        self.assertTrue(VALIDATOR.is_file(), "scripts/docs_validate.py не найден")

    def test_corpus_passes(self):
        """Корпус чистый: ненулевой код валидатора — провал прогона."""
        result = _run("--quiet")
        self.assertEqual(
            result.returncode, 0,
            "валидатор нашёл ошибки в корпусе:\n"
            f"{result.stdout}\n{result.stderr}")

    def test_works_without_neo4j(self):
        """R13. Валидатор читает словарь имён из файла, а не из графа.

        Ломаем адрес базы: если проверка втихую начнёт ходить в Neo4j,
        поведение изменится, и это надо заметить здесь, а не на машине,
        где стек не поднят.
        """
        result = _run("--quiet", env={"NEO4J_URL": "http://127.0.0.1:1/",
                                      "NEO4J_PASSWORD": "нет"})
        self.assertEqual(
            result.returncode, 0,
            "при недоступной Neo4j валидатор обязан отработать так же:\n"
            f"{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
