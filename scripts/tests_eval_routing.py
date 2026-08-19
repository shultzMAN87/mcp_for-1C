"""
FIX-23. Прибор не имеет права молча мерить не то.
==================================================

Что было. Команда

    python3 scripts/eval.py --dataset metadata_graph

напечатала `[eval] сервер: help` и пошла дальше. Упала она позже — на
пути к файлу датасета, — и про сервер не сказала ничего. Для скрипта
«имя не нашлось в карте DATASET_SERVER» и «сервер не указан» были одним
событием: `DATASET_SERVER.get(имя, "help")`.

Почему это опаснее, чем выглядит. Падение было случайностью: лежи датасет
по угаданному пути — прогон **прошёл бы**. Датасетом графа против сервера
справки: инструментов таких там нет, ответы пришли бы пустыми, и цифры
вышли бы правдоподобно плохими. Дальше эти цифры сравнили бы с прошлым
прогоном и получили «регресс», которого нет.

Та же семья, что `API-1` (фильтр инструментов жаловался вместо отказа) и
`FIX-16` (запрос по несуществующему типу ребра отвечал пустотой без
ошибки): молчаливая подмена, дающая правдоподобный неверный результат.

Проверяется поведение функций разбора, а не запуск docker: маршрут
решается до того, как что-либо поднимается.

Запуск:  python3 scripts/tests_eval_routing.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import eval as eval_mod  # noqa: E402


class TestDatasetNameResolution(unittest.TestCase):
    """Короткое имя — законная форма записи: рука пишет именно её."""

    def test_short_name_becomes_path(self):
        self.assertEqual(
            eval_mod.resolve_dataset("metadata_graph"),
            "evals/datasets/metadata_graph.jsonl",
        )

    def test_file_name_becomes_path(self):
        self.assertEqual(
            eval_mod.resolve_dataset("metadata_graph.jsonl"),
            "evals/datasets/metadata_graph.jsonl",
        )

    def test_full_path_is_left_alone(self):
        self.assertEqual(
            eval_mod.resolve_dataset("evals/datasets/metadata_graph.jsonl"),
            "evals/datasets/metadata_graph.jsonl",
        )

    def test_windows_slashes_are_understood(self):
        """Основная рабочая машина проекта — Windows, и путь оттуда."""
        self.assertEqual(
            eval_mod.resolve_dataset(r"evals\datasets\v8std.jsonl"),
            "evals/datasets/v8std.jsonl",
        )

    def test_empty_is_refused(self):
        with self.assertRaises(eval_mod.DatasetRoutingError):
            eval_mod.resolve_dataset("  ")


class TestServerIsNeverGuessed(unittest.TestCase):
    """Сердце FIX-23: неизвестное имя — это отказ, а не умолчание."""

    def test_known_dataset_routes_to_its_server(self):
        for stem, server in eval_mod.DATASET_SERVER.items():
            with self.subTest(stem=stem):
                self.assertEqual(
                    eval_mod.resolve_server(f"evals/datasets/{stem}.jsonl", None),
                    server,
                )

    def test_unknown_dataset_refuses(self):
        with self.assertRaises(eval_mod.DatasetRoutingError) as ctx:
            eval_mod.resolve_server("evals/datasets/чего-то-новое.jsonl", None)
        text = str(ctx.exception)
        self.assertIn("--server", text,
                      "отказ обязан сказать, чем его лечить")

    def test_unknown_dataset_with_explicit_server_is_fine(self):
        self.assertEqual(
            eval_mod.resolve_server("evals/datasets/что-то.jsonl", "meta"),
            "meta",
        )

    def test_explicit_server_wins_over_the_map(self):
        """
        Явное указание сильнее карты: иначе нельзя прогнать датасет против
        соседнего сервера, а именно так проверяют, что фильтр инструментов
        действительно разный.
        """
        self.assertEqual(
            eval_mod.resolve_server("evals/datasets/v8std.jsonl", "help"),
            "help",
        )

    def test_no_default_server_left_in_code(self):
        """
        Сторож на форму записи, а не на поведение. Умолчание вернётся
        одной строкой `\u002eget(name, "help")` — и вернётся молча.
        """
        text = (SCRIPTS / "eval.py").read_text(encoding="utf-8")
        self.assertNotIn('DATASET_SERVER.get(Path(args.dataset).name, "help")', text)
        for bad in ('DATASET_SERVER.get(stem, "', "DATASET_SERVER.get(stem, '"):
            self.assertNotIn(
                bad, text,
                "у карты датасет→сервер снова появилось умолчание — "
                "неизвестный датасет обязан требовать --server, а не "
                "уходить к чужому серверу (FIX-23)",
            )


class TestMapMatchesReality(unittest.TestCase):
    """
    Шестой список в проекте, который может разойтись с содержимым каталога.
    Предыдущие пять расходились молча (LOCK-1, B-6, HYG-2, HYG-4, указатель
    архива), поэтому сверяется, а не поддерживается на честном слове.
    """

    def test_every_measured_dataset_has_a_server(self):
        datasets = set(eval_mod.known_datasets())
        # `probe` — служебный набор про транспорт, качеством не считается
        # (тот же список исключений, что в eval_all.SKIP_STEMS).
        datasets.discard("probe")
        unmapped = sorted(datasets - set(eval_mod.DATASET_SERVER))
        self.assertFalse(
            unmapped,
            f"датасеты без сервера в карте: {unmapped}. Прогон каждого из "
            f"них теперь потребует --server руками — либо допишите их в "
            f"DATASET_SERVER",
        )

    def test_map_has_no_ghosts(self):
        ghosts = sorted(set(eval_mod.DATASET_SERVER) - set(eval_mod.known_datasets()))
        self.assertFalse(ghosts, f"в карте есть несуществующие датасеты: {ghosts}")

    def test_every_server_in_map_exists(self):
        for stem, server in eval_mod.DATASET_SERVER.items():
            self.assertIn(server, eval_mod.SERVERS,
                          f"{stem} показывает на неизвестный сервер {server}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
