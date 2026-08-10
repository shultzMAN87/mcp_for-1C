"""
Тесты блока A Захода 4: PERF-3 (fingerprint) и PERF-4 (запись рёбер).
======================================================================

Почему отдельный файл, а не дополнение к tests_graph_writer.py:
tests_graph_writer требует живой Neo4j (или testcontainers) и скипается
целиком, когда базы нет. Логика, которую правил блок A, к базе не
привязана — её можно и нужно проверять офлайн, в том же прогоне, что
парсер и резолвер.

Neo4j подменён стабом `FakeNeo4j`, который запоминает пары
(cypher, parameters). Это ровно та граница, которая нас интересует:
PERF-4 — про то, КАКОЙ Cypher уходит в базу и с какими строками, а не
про то, что база с ним сделает. Проверка «а действительно ли теперь
используется индекс» живёт на боевом прогоне и в его таймингах.

Запуск:
    python tests_fingerprint.py -v
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from graph_writer import (
    FP_MODE_CONTENT, FP_MODE_STAT,
    EDGE_QUERIES, _infer_src_label,
    fingerprint_matches, fingerprint_workspace,
    fingerprint_workspace_files, fingerprint_workspace_multi,
    write_edges,
)


# ─── Стаб Neo4j ───────────────────────────────────────────────────────


class FakeNeo4j:
    """Запоминает всё, что в него писали. Ничего не исполняет."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def query(self, cypher, parameters=None):
        self.calls.append((cypher, parameters or {}))
        return {"results": [{"columns": [], "data": []}], "errors": []}

    def rows(self, cypher, parameters=None):
        self.calls.append((cypher, parameters or {}))
        return []


def _mkfile(root: Path, rel: str, content: str = "x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ─── PERF-3: fingerprint ──────────────────────────────────────────────


class TestFingerprintModes(unittest.TestCase):
    """Оба режима должны отвечать на один вопрос: менялось ли что-нибудь."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _mkfile(self.root, "Catalogs/Контрагенты.xml", "<a/>")
        _mkfile(self.root, "Catalogs/Контрагенты/Ext/ObjectModule.bsl", "Процедура П() КонецПроцедуры")
        _mkfile(self.root, "CommonModules/Общий/Ext/Module.bsl", "Функция Ф() Возврат 1; КонецФункции")

    def tearDown(self):
        self._tmp.cleanup()

    # ─ Базовое поведение ─

    def test_stat_mode_is_default(self):
        _, meta = fingerprint_workspace_multi(self.root)
        self.assertEqual(meta["mode"], FP_MODE_STAT)

    def test_strict_mode_reports_content(self):
        _, meta = fingerprint_workspace_multi(self.root, strict=True)
        self.assertEqual(meta["mode"], FP_MODE_CONTENT)

    def test_separate_digests_per_suffix(self):
        digests, meta = fingerprint_workspace_multi(self.root, (".xml", ".bsl"))
        self.assertEqual(set(digests), {".xml", ".bsl"})
        self.assertNotEqual(digests[".xml"], digests[".bsl"])
        self.assertEqual(meta["by_suffix"][".xml"], 1)
        self.assertEqual(meta["by_suffix"][".bsl"], 2)
        self.assertEqual(meta["files"], 3)

    def test_stable_across_runs(self):
        d1, _ = fingerprint_workspace_multi(self.root)
        d2, _ = fingerprint_workspace_multi(self.root)
        self.assertEqual(d1, d2)

    def test_empty_tree_gives_stable_digest(self):
        with tempfile.TemporaryDirectory() as empty:
            d1, _ = fingerprint_workspace_multi(Path(empty))
            d2, _ = fingerprint_workspace_multi(Path(empty))
            self.assertEqual(d1[".xml"], d2[".xml"])

    # ─ Главное свойство: изменение файла видно ─

    def test_content_change_detected_in_stat_mode(self):
        """
        Правка меняет и размер, и mtime — режим stat её видит.
        Это основной сценарий: разработчик поправил модуль.
        """
        before, _ = fingerprint_workspace_multi(self.root)
        p = self.root / "CommonModules/Общий/Ext/Module.bsl"
        p.write_text("Функция Ф() Возврат 2; КонецФункции // дописали", encoding="utf-8")
        after, _ = fingerprint_workspace_multi(self.root)
        self.assertNotEqual(before[".bsl"], after[".bsl"])
        self.assertEqual(before[".xml"], after[".xml"], "чужое расширение не должно шевелиться")

    def test_same_size_but_new_mtime_detected(self):
        """
        Правка, не изменившая размер (заменили символ на символ). Ловится
        по mtime — ради этого в кортеже и лежит st_mtime_ns, а не только
        размер.
        """
        p = self.root / "CommonModules/Общий/Ext/Module.bsl"
        before, _ = fingerprint_workspace_multi(self.root)
        text = p.read_text(encoding="utf-8")
        p.write_text(text.replace("1", "2"), encoding="utf-8")
        self.assertEqual(len(text), len(p.read_text(encoding="utf-8")))
        after, _ = fingerprint_workspace_multi(self.root)
        self.assertNotEqual(before[".bsl"], after[".bsl"])

    def test_new_file_detected(self):
        before, _ = fingerprint_workspace_multi(self.root)
        _mkfile(self.root, "Documents/Заказ.xml", "<d/>")
        after, _ = fingerprint_workspace_multi(self.root)
        self.assertNotEqual(before[".xml"], after[".xml"])

    def test_deleted_file_detected(self):
        before, _ = fingerprint_workspace_multi(self.root)
        (self.root / "Catalogs/Контрагенты.xml").unlink()
        after, _ = fingerprint_workspace_multi(self.root)
        self.assertNotEqual(before[".xml"], after[".xml"])

    def test_renamed_file_detected(self):
        """Переименование не меняет ни размер, ни mtime — ловится по пути."""
        before, _ = fingerprint_workspace_multi(self.root)
        src = self.root / "Catalogs/Контрагенты.xml"
        src.rename(self.root / "Catalogs/Партнёры.xml")
        after, _ = fingerprint_workspace_multi(self.root)
        self.assertNotEqual(before[".xml"], after[".xml"])

    # ─ Известное ограничение режима stat ─

    def test_known_blind_spot_same_size_same_mtime(self):
        """
        Документируем границу честно: если содержимое подменили, а размер и
        mtime восстановили (так делает копирование выгрузки утилитой,
        сохраняющей время), режим stat изменения НЕ увидит, а строгий —
        увидит. Ровно за этим и оставлен METADATA_FINGERPRINT_STRICT.

        Тест на ограничение, а не на дефект: он падает, если ограничение
        случайно исчезнет — и тогда комментарий в .env.example надо будет
        переписать.
        """
        p = self.root / "CommonModules/Общий/Ext/Module.bsl"
        st = os.stat(p)
        stat_before, _ = fingerprint_workspace_multi(self.root)
        strict_before, _ = fingerprint_workspace_multi(self.root, strict=True)

        text = p.read_text(encoding="utf-8")
        p.write_text(text.replace("1", "9"), encoding="utf-8")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))

        stat_after, _ = fingerprint_workspace_multi(self.root)
        strict_after, _ = fingerprint_workspace_multi(self.root, strict=True)

        self.assertEqual(stat_before[".bsl"], stat_after[".bsl"],
                         "режим stat такую подмену не видит — это известная граница")
        self.assertNotEqual(strict_before[".bsl"], strict_after[".bsl"],
                            "строгий режим обязан её видеть, иначе он бесполезен")

    # ─ Прочее ─

    def test_meta_counts_bytes_and_newest_mtime(self):
        _, meta = fingerprint_workspace_multi(self.root)
        self.assertGreater(meta["bytes"], 0)
        self.assertGreater(meta["newest_mtime"], 0)
        self.assertLessEqual(meta["newest_mtime"], time.time() + 5)
        self.assertGreaterEqual(meta["elapsed_sec"], 0.0)

    def test_suffix_matching_is_case_insensitive(self):
        """В выгрузках встречается .XML — он должен попадать в тот же набор."""
        _mkfile(self.root, "Reports/Отчёт.XML", "<r/>")
        digests, meta = fingerprint_workspace_multi(self.root, (".xml",))
        self.assertEqual(meta["by_suffix"][".xml"], 2)
        self.assertTrue(digests[".xml"])

    def test_nested_dirs_are_walked(self):
        _mkfile(self.root, "a/b/c/d/e/Глубоко.xml", "<x/>")
        _, meta = fingerprint_workspace_multi(self.root, (".xml",))
        self.assertEqual(meta["by_suffix"][".xml"], 2)

    def test_single_suffix_wrapper_matches_multi(self):
        digests, _ = fingerprint_workspace_multi(self.root, (".xml",))
        self.assertEqual(fingerprint_workspace_files(self.root, ".xml"), digests[".xml"])
        self.assertEqual(fingerprint_workspace_files(self.root, "xml"), digests[".xml"])
        self.assertEqual(fingerprint_workspace(self.root), digests[".xml"])

    def test_multi_walk_equals_two_single_walks(self):
        """
        Обход стал один на оба расширения. Значения обязаны совпасть с тем,
        что дают раздельные обходы, иначе экономия куплена сменой семантики.
        """
        digests, _ = fingerprint_workspace_multi(self.root, (".xml", ".bsl"))
        self.assertEqual(digests[".xml"], fingerprint_workspace_files(self.root, ".xml"))
        self.assertEqual(digests[".bsl"], fingerprint_workspace_files(self.root, ".bsl"))

    def test_missing_root_does_not_raise(self):
        """
        Индексер сам проверяет существование каталога, но fingerprint не
        должен падать раньше этой проверки — иначе сообщение об ошибке
        будет про scandir, а не про отсутствующую выгрузку.
        """
        digests, meta = fingerprint_workspace_multi(self.root / "нет-такого")
        self.assertEqual(meta["files"], 0)
        self.assertTrue(digests[".xml"])


class TestFingerprintMatches(unittest.TestCase):
    """Сравнение сохранённого и свежего fingerprint'а."""

    def test_first_run(self):
        same, why = fingerprint_matches(None, "abc", FP_MODE_STAT)
        self.assertFalse(same)
        self.assertIn("отсутствует", why)

    def test_equal(self):
        old = {"value": "abc", "mode": FP_MODE_STAT}
        same, why = fingerprint_matches(old, "abc", FP_MODE_STAT)
        self.assertTrue(same)
        self.assertIn("совпал", why)

    def test_changed(self):
        old = {"value": "abc123", "mode": FP_MODE_STAT}
        same, why = fingerprint_matches(old, "def456", FP_MODE_STAT)
        self.assertFalse(same)
        self.assertIn("изменился", why)

    def test_mode_switch_is_not_reported_as_change(self):
        """
        Сумма по содержимому и сумма по (размер, mtime) — разные величины.
        Их несовпадение не значит, что файлы менялись, и лог не должен так
        говорить: иначе после включения STRICT человек пойдёт искать
        несуществующую правку.
        """
        old = {"value": "abc", "mode": FP_MODE_CONTENT}
        same, why = fingerprint_matches(old, "abc", FP_MODE_STAT)
        self.assertFalse(same)
        self.assertIn("способ подсчёта", why)
        self.assertNotIn("изменился", why)

    def test_legacy_node_without_mode_treated_as_content(self):
        """
        Узлы :Fingerprint, записанные до PERF-3, свойства `mode` не имеют.
        Считать их посчитанными по содержимому — единственно верно: так и
        было. Следствие — одна переиндексация после обновления.
        """
        old = {"value": "abc", "mode": FP_MODE_CONTENT}   # то, что вернёт _get_meta
        same, _ = fingerprint_matches(old, "abc", FP_MODE_CONTENT)
        self.assertTrue(same)


# ─── PERF-4: запись рёбер ─────────────────────────────────────────────


class TestEdgeQueriesUseLabels(unittest.TestCase):
    """
    Инвариант схемы: обе стороны MATCH обязаны быть с меткой.

    Именно его нарушение стоило 28 минут на записи слоя 1: все констрейнты
    привязаны к меткам, поэтому матч без метки не может воспользоваться
    индексом и вырождается в полный перебор на каждую строку UNWIND.
    """

    def _all_queries(self):
        for rel, q in EDGE_QUERIES.items():
            if isinstance(q, dict):
                for label, cypher in q.items():
                    yield f"{rel}:{label}", cypher
            else:
                yield rel, q

    def test_no_unlabeled_match(self):
        for name, cypher in self._all_queries():
            with self.subTest(edge=name):
                self.assertNotIn("MATCH (a {id:", cypher,
                                 f"{name}: источник без метки — индекс не будет использован")
                self.assertNotIn("(b {id:", cypher,
                                 f"{name}: приёмник без метки — индекс не будет использован")

    def test_every_match_operand_has_label(self):
        import re
        for name, cypher in self._all_queries():
            with self.subTest(edge=name):
                # Все шаблоны вида `(x ... {id: r.src})` должны иметь `:Метка`.
                for operand in re.findall(r"\((\w+)([^)]*)\{id:", cypher):
                    var, mid = operand
                    self.assertIn(":", mid,
                                  f"{name}: переменная {var} матчится по id без метки")

    def test_has_attribute_split_by_source_label(self):
        q = EDGE_QUERIES["HAS_ATTRIBUTE"]
        self.assertIsInstance(q, dict)
        self.assertEqual(set(q), {"MetadataObject", "TabularSection"})
        self.assertIn("(a:MetadataObject {id: r.src})", q["MetadataObject"])
        self.assertIn("(a:TabularSection {id: r.src})", q["TabularSection"])


class TestWriteEdgesGrouping(unittest.TestCase):
    """Группировка по паре (тип ребра, метка источника)."""

    def test_split_into_two_queries(self):
        neo = FakeNeo4j()
        edges = [
            {"rel": "HAS_ATTRIBUTE", "src": "Catalog.К", "dst": "Catalog.К.Attr.А",
             "src_label": "MetadataObject", "props": {"role": "attribute"}},
            {"rel": "HAS_ATTRIBUTE", "src": "Catalog.К.TS.Т", "dst": "Catalog.К.TS.Т.Attr.Б",
             "src_label": "TabularSection", "props": {"role": "attribute"}},
        ]
        counters = write_edges(neo, edges, log_progress=False)

        self.assertEqual(counters["HAS_ATTRIBUTE"], 2, "счётчик сводится к типу ребра")
        self.assertEqual(len(neo.calls), 2, "две метки — два запроса")
        used = {c[0] for c in neo.calls}
        self.assertTrue(any("(a:MetadataObject {id: r.src})" in c for c in used))
        self.assertTrue(any("(a:TabularSection {id: r.src})" in c for c in used))

    def test_src_label_not_written_as_edge_property(self):
        """
        `src_label` — служебное поле writer'а, не свойство ребра. Если бы оно
        попало в props, в графе появилось бы бессмысленное свойство на
        42 тысячах рёбер.
        """
        neo = FakeNeo4j()
        write_edges(neo, [
            {"rel": "HAS_ATTRIBUTE", "src": "Catalog.К", "dst": "d",
             "src_label": "MetadataObject", "props": {"role": "attribute"}},
        ], log_progress=False)
        rows = neo.calls[0][1]["rows"]
        self.assertEqual(set(rows[0]), {"src", "dst", "role"})

    def test_label_inferred_when_absent(self):
        """
        Старый вызывающий код метку не проставляет. Вывод по id должен
        работать, иначе рёбра ТЧ пойдут запросом для :MetadataObject и
        просто не запишутся — молча, потому что MATCH ничего не найдёт.
        """
        neo = FakeNeo4j()
        write_edges(neo, [
            {"rel": "HAS_ATTRIBUTE", "src": "Catalog.К.TS.Т", "dst": "d",
             "props": {"role": "attribute"}},
        ], log_progress=False)
        self.assertIn("(a:TabularSection {id: r.src})", neo.calls[0][0])

    def test_infer_src_label_rules(self):
        variants = EDGE_QUERIES["HAS_ATTRIBUTE"]
        self.assertEqual(
            _infer_src_label("HAS_ATTRIBUTE", "Catalog.К.TS.Товары", variants),
            "TabularSection")
        self.assertEqual(
            _infer_src_label("HAS_ATTRIBUTE", "Catalog.К", variants),
            "MetadataObject")
        # Объект со словом TS в имени, но без разделителя ".TS." — не ТЧ.
        self.assertEqual(
            _infer_src_label("HAS_ATTRIBUTE", "Catalog.TSКонтрагенты", variants),
            "MetadataObject")

    def test_bad_label_falls_back_to_inference(self):
        """Мусорная метка не должна ронять запись — только приводить к выводу."""
        neo = FakeNeo4j()
        write_edges(neo, [
            {"rel": "HAS_ATTRIBUTE", "src": "Catalog.К.TS.Т", "dst": "d",
             "src_label": "Ерунда", "props": {"role": "attribute"}},
        ], log_progress=False)
        self.assertIn("(a:TabularSection {id: r.src})", neo.calls[0][0])

    def test_batching(self):
        neo = FakeNeo4j()
        edges = [{"rel": "CONTAINS", "src": f"s{i}", "dst": f"d{i}", "props": {}}
                 for i in range(1200)]
        counters = write_edges(neo, edges, batch=500, log_progress=False)
        self.assertEqual(counters["CONTAINS"], 1200)
        self.assertEqual(len(neo.calls), 3)
        self.assertEqual([len(c[1]["rows"]) for c in neo.calls], [500, 500, 200])

    def test_unknown_rel_skipped_without_raising(self):
        neo = FakeNeo4j()
        counters = write_edges(neo, [
            {"rel": "НЕТ_ТАКОГО", "src": "a", "dst": "b", "props": {}},
            {"rel": "CONTAINS", "src": "a", "dst": "b", "props": {}},
        ], log_progress=False)
        self.assertNotIn("НЕТ_ТАКОГО", counters)
        self.assertEqual(counters["CONTAINS"], 1)
        self.assertEqual(len(neo.calls), 1)

    def test_empty_input(self):
        neo = FakeNeo4j()
        self.assertEqual(write_edges(neo, [], log_progress=False), {})
        self.assertEqual(neo.calls, [])

    def test_counters_sum_across_labels(self):
        neo = FakeNeo4j()
        edges = (
            [{"rel": "HAS_ATTRIBUTE", "src": f"Catalog.К{i}", "dst": f"d{i}",
              "src_label": "MetadataObject", "props": {"role": "attribute"}}
             for i in range(7)]
            + [{"rel": "HAS_ATTRIBUTE", "src": f"Catalog.К.TS.Т{i}", "dst": f"e{i}",
                "src_label": "TabularSection", "props": {"role": "attribute"}}
               for i in range(3)]
        )
        counters = write_edges(neo, edges, log_progress=False)
        self.assertEqual(counters, {"HAS_ATTRIBUTE": 10})


class TestBuildGraphMarksSourceLabel(unittest.TestCase):
    """build_graph обязан проставлять метку — вывод по id это только страховка."""

    def test_labels_present_on_has_attribute(self):
        from metadata_xml import (
            Attribute, MetaObject, TabularSection, TypeRef, build_graph,
        )
        obj = MetaObject(
            name="К", kind_eng="Catalog", kind_ru="Справочник",
            kind_ru_plural="Справочники",
            attributes=[Attribute(name="А", types=[TypeRef(kind="String")])],
            tabular_sections=[TabularSection(
                name="Т", attributes=[Attribute(name="Б", types=[TypeRef(kind="Number")])],
            )],
        )
        graph = build_graph([obj])
        ha = [e for e in graph["edges"] if e["rel"] == "HAS_ATTRIBUTE"]
        self.assertEqual(len(ha), 2)
        labels = {e["src"]: e.get("src_label") for e in ha}
        self.assertEqual(labels["Catalog.К"], "MetadataObject")
        self.assertEqual(labels["Catalog.К.TS.Т"], "TabularSection")

    def test_other_edges_have_no_label(self):
        """Лишнее поле на рёбрах, у которых источник однозначен, не нужно."""
        from metadata_xml import MetaObject, TabularSection, build_graph
        obj = MetaObject(
            name="К", kind_eng="Catalog", kind_ru="Справочник",
            kind_ru_plural="Справочники",
            tabular_sections=[TabularSection(name="Т")],
        )
        graph = build_graph([obj])
        for e in graph["edges"]:
            if e["rel"] != "HAS_ATTRIBUTE":
                self.assertNotIn("src_label", e, f"{e['rel']} не нуждается в метке")


if __name__ == "__main__":
    unittest.main(verbosity=2)
