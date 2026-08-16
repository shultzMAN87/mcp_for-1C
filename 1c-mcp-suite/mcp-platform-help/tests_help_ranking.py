"""
Тесты прореживания и перемешивания выдачи (help_ranking).

Ключевой тест — `TestPh002`: воспроизводит реальную выдачу на запрос
«длина строки», снятую с боевой коллекции 14 августа, и проверяет, что
`СтрДлина` возвращается в топ-5. Числа не выдуманы: это скоры из отчёта
`evals/reports/probe_20260814_200047.json`.

Второй по важности — `TestNoHarm`: набор случаев, где перемешивание не
должно менять ничего. Правка, которая чинит один запрос и ломает
остальные, хуже, чем отсутствие правки.

Запуск:
    python3 tests_help_ranking.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from help_ranking import (
    collapse_pages,
    demote_repeated_objects,
    diversify_hits,
    object_key,
    page_key,
)


def hit(name, score, *, parent="", path="", chunk="card", kind="method"):
    return {
        "score": score,
        "chunk_type": chunk,
        "kind": kind,
        "name_ru": name,
        "name_en": "",
        "parent_ru": parent,
        "parent_en": "",
        "full_name": f"{parent}.{name}" if parent else name,
        "file_path": path or f"objects/{parent or 'x'}/{name}.html",
        "text": "",
    }


def names(hits):
    return [h["name_ru"] for h in hits]


class TestKeys(unittest.TestCase):
    def test_page_key_prefers_path(self):
        self.assertEqual(page_key(hit("А", 1, path="p/1.html")), "p/1.html")

    def test_page_key_falls_back(self):
        self.assertEqual(page_key({"full_name": "Х.У"}), "Х.У")

    def test_object_key_uses_parent(self):
        self.assertEqual(
            object_key(hit("Длина", 1, parent="КвалификаторыСтроки")),
            "parent:КвалификаторыСтроки",
        )

    def test_article_without_parent_is_its_own_group(self):
        a = hit("Статья А", 1, path="a.html", kind="article")
        b = hit("Статья Б", 1, path="b.html", kind="article")
        self.assertNotEqual(object_key(a), object_key(b))


class TestCollapsePages(unittest.TestCase):
    def test_second_chunk_of_same_page_dropped(self):
        hits = [
            hit("СтрДлина", 0.9, path="g/StrLen.html", chunk="card"),
            hit("СтрДлина", 0.7, path="g/StrLen.html", chunk="description"),
            hit("СтрНайти", 0.5, path="g/StrFind.html"),
        ]
        self.assertEqual(names(collapse_pages(hits)), ["СтрДлина", "СтрНайти"])

    def test_best_chunk_survives(self):
        hits = [
            hit("СтрДлина", 0.9, path="p.html", chunk="card"),
            hit("СтрДлина", 0.7, path="p.html", chunk="description"),
        ]
        self.assertEqual(collapse_pages(hits)[0]["chunk_type"], "card")

    def test_hits_without_keys_are_kept(self):
        hits = [{"score": 1}, {"score": 0.5}]
        self.assertEqual(len(collapse_pages(hits)), 2)


class TestDemoteRepeats(unittest.TestCase):
    def test_single_group_unchanged(self):
        """Вся выдача про один объект — понижать нечего, порядок прежний."""
        hits = [hit(f"Метод{i}", 1 - i / 10, parent="Глобальный контекст")
                for i in range(5)]
        self.assertEqual(names(demote_repeated_objects(hits)), names(hits))

    def test_weak_repeat_falls_below_stranger(self):
        hits = [
            hit("Длина", 0.533, parent="КвалификаторыСтроки"),
            hit("По умолчанию", 0.341, parent="КвалификаторыСтроки"),
            hit("СтрДлина", 0.310, parent="Глобальный контекст"),
        ]
        # 0.341 / 2 = 0.17 — ниже 0.310
        self.assertEqual(
            names(demote_repeated_objects(hits)),
            ["Длина", "СтрДлина", "По умолчанию"],
        )

    def test_strong_repeat_keeps_its_place(self):
        """Второй результат объекта остаётся выше, если он честно сильнее."""
        hits = [
            hit("СтрЗаменитьПоРегулярномуВыражению", 0.667, parent="Глобальный контекст"),
            hit("СтрЗаменить", 0.583, parent="Глобальный контекст"),
            hit("Содержит", 0.25, parent="ВидСравнения"),
        ]
        # 0.583 / 2 = 0.29 — всё ещё выше 0.25
        self.assertEqual(names(demote_repeated_objects(hits))[1], "СтрЗаменить")

    def test_nothing_is_lost(self):
        hits = [hit(f"М{i}", 1 - i / 20, parent=f"Об{i % 3}") for i in range(12)]
        self.assertEqual(sorted(names(demote_repeated_objects(hits))),
                         sorted(names(hits)))

    def test_first_place_never_changes(self):
        """Самый релевантный результат обязан остаться первым."""
        hits = [hit(f"М{i}", 1 - i / 20, parent=f"Об{i % 4}") for i in range(9)]
        self.assertEqual(demote_repeated_objects(hits)[0], hits[0])

    def test_missing_score_does_not_crash(self):
        hits = [{"name_ru": "А", "file_path": "a"}, {"name_ru": "Б", "file_path": "b"}]
        self.assertEqual(len(demote_repeated_objects(hits)), 2)


class TestPh002(unittest.TestCase):
    """
    Реальная выдача на «длина строки» (боевая коллекция, 14.08.2026).
    До правки СтрДлина была шестой.
    """

    def setUp(self):
        self.hits = [
            hit("Функция Строка", 0.667, path="q/String.html", kind="article"),
            hit("Функция ДлинаСтроки", 0.625, path="q/StringLength.html", kind="article"),
            hit("Длина", 0.533, parent="КвалификаторыСтроки", kind="property"),
            hit("По умолчанию", 0.341, parent="КвалификаторыСтроки", kind="object_type"),
            hit("Работа со строками", 0.333, path="d/strings.html", kind="article"),
            hit("СтрДлина", 0.310, parent="Глобальный контекст"),
        ]

    def test_strdlina_enters_top5(self):
        top5 = names(diversify_hits(self.hits, 5))
        self.assertIn("СтрДлина", top5)
        self.assertEqual(len(top5), 5)

    def test_relevant_query_language_pages_stay_on_top(self):
        """Новые страницы языка запросов отвечают по делу — не вытесняем их."""
        top5 = names(diversify_hits(self.hits, 5))
        self.assertEqual(top5[0], "Функция Строка")
        self.assertIn("Функция ДлинаСтроки", top5)

    def test_duplicate_object_pushed_out(self):
        top5 = names(diversify_hits(self.hits, 5))
        self.assertIn("Длина", top5)
        self.assertNotIn("По умолчанию", top5)


class TestPh003(unittest.TestCase):
    """
    Реальная выдача на «заменить подстроку в строке» (10.08.2026).
    Первая версия правки (чередование по кругу) роняла этот пример:
    СтрЗаменить уходил из топ-5. Числа — из отчёта
    evals/reports/platform_help_20260810_201204.json.
    """

    def setUp(self):
        gk = "Глобальный контекст"
        self.hits = [
            hit("СтрЗаменитьПоРегулярномуВыражению", 0.667, parent=gk),
            hit("СтрЗаменить", 0.583, parent=gk, path="g/StrReplace.html", chunk="card"),
            hit("СтрЗаменить", 0.500, parent=gk, path="g/StrReplace.html", chunk="description"),
            hit("СтрЗаменить", 0.333, parent=gk, path="g/StrReplace.html", chunk="syntax"),
            hit("Содержит", 0.250, parent="ВидСравнения"),
            # Хвост добора: много чужих объектов со слабыми скорами. Именно
            # он топил СтрЗаменить при чередовании по кругу.
            *[hit(f"Чужой{i}", 0.2 - i / 100, parent=f"Объект{i}") for i in range(8)],
        ]

    def test_strzamenit_stays_in_top5(self):
        self.assertIn("СтрЗаменить", names(diversify_hits(self.hits, 5)))

    def test_duplicate_chunks_collapsed(self):
        top10 = names(diversify_hits(self.hits, 10))
        self.assertEqual(top10.count("СтрЗаменить"), 1)

    def test_weak_tail_does_not_outrank_strong_object(self):
        top3 = names(diversify_hits(self.hits, 3))
        self.assertEqual(top3[:2], ["СтрЗаменитьПоРегулярномуВыражению", "СтрЗаменить"])


class TestNoHarm(unittest.TestCase):
    """Случаи, где выдача обязана остаться прежней."""

    def test_empty(self):
        self.assertEqual(diversify_hits([], 10), [])

    def test_all_distinct_objects_unchanged(self):
        hits = [hit(f"М{i}", 1 - i / 10, parent=f"Об{i}") for i in range(6)]
        self.assertEqual(names(diversify_hits(hits, 6)), names(hits))

    def test_limit_respected(self):
        hits = [hit(f"М{i}", 1 - i / 40, parent=f"Об{i % 5}") for i in range(30)]
        self.assertEqual(len(diversify_hits(hits, 10)), 10)

    def test_limit_larger_than_hits(self):
        hits = [hit("А", 0.9, parent="Об")]
        self.assertEqual(len(diversify_hits(hits, 10)), 1)

    def test_methods_of_one_object_survive_together(self):
        """
        Запрос про методы одного объекта: все они одной группы, порядок
        сохраняется, ничего не схлопывается.
        """
        hits = [hit(f"Метод{i}", 1 - i / 10, parent="Справочники") for i in range(8)]
        self.assertEqual(names(diversify_hits(hits, 8)), names(hits))


class TestPageKeyIsUniqueAcrossContainers(unittest.TestCase):
    """
    A-7. `file_path` уникален внутри одного .hbk — и только внутри него.
    После HBK-1 контейнеров сорок, и индексатор насчитал 11 страниц из 27
    тысяч под уже занятым путём.

    Цена именно здесь: `collapse_pages` считала такие страницы одной и
    выбрасывала вторую как дубль. Не отказ и не ошибка — просто выдача
    беднее на страницу, и заметить это со стороны нельзя.
    """

    def test_same_path_in_different_books_is_two_pages(self):
        hits = [
            {"file_path": "objects/catalog213/Add4692.html",
             "hbk_file": "shcntx_ru.hbk", "name_ru": "Добавить", "score": 9},
            {"file_path": "objects/catalog213/Add4692.html",
             "hbk_file": "shlang_ru.hbk", "name_ru": "Добавить", "score": 8},
        ]
        self.assertNotEqual(page_key(hits[0]), page_key(hits[1]))
        self.assertEqual(len(collapse_pages(hits)), 2,
                         "страница из другого контейнера выброшена как дубль")

    def test_same_path_same_book_is_one_page(self):
        hits = [
            {"file_path": "objects/catalog213/Add4692.html",
             "hbk_file": "shcntx_ru.hbk", "chunk_type": "description", "score": 9},
            {"file_path": "objects/catalog213/Add4692.html",
             "hbk_file": "shcntx_ru.hbk", "chunk_type": "params", "score": 8},
        ]
        self.assertEqual(page_key(hits[0]), page_key(hits[1]))
        self.assertEqual(len(collapse_pages(hits)), 1)

    def test_old_payload_without_hbk_file_still_works(self):
        """Схема без `hbk_file` — путь остаётся ключом, как раньше."""
        hit = {"file_path": "a/b.html", "name_ru": "X"}
        self.assertEqual(page_key(hit), "a/b.html")

    def test_no_path_falls_back_to_name(self):
        self.assertEqual(page_key({"full_name": "Массив.Добавить"}),
                         "Массив.Добавить")
        self.assertEqual(page_key({}), "")


class TestRelatedChunksAreFilteredByPair(unittest.TestCase):
    """
    Контрактная часть: карточка `platform_help_lookup` собирает синтаксис,
    параметры и пример через фильтр Qdrant. Пока фильтр стоял по одному
    пути, на 11 занятых путях карточка собирала части ЧУЖОЙ страницы — и
    наружу это выходило не отказом, а правдоподобным неверным ответом.

    Проверяем текстом по исходнику: server.py не импортируется — он тянет
    FastMCP и qdrant_client.
    """

    def setUp(self):
        self.src = (Path(__file__).resolve().parent / "server.py").read_text(
            encoding="utf-8")

    def test_filter_includes_hbk_file(self):
        self.assertIn('key="hbk_file"', self.src,
                      "фильтр связанных чанков не учитывает контейнер")

    def test_lookup_card_carries_hbk_file(self):
        """
        Без `hbk_file` в карточке пара не собирается, и фильтр вырождается
        обратно в поиск по одному пути — правка стала бы косметической.
        """
        card_block = self.src[self.src.index('"lookup": name,') - 2000:
                              self.src.index('"lookup": name,')]
        self.assertIn('"hbk_file"', card_block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
