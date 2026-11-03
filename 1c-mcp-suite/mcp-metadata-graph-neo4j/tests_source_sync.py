"""
Тесты SRC-7..12 — синхронизация графа с выгрузкой через git.
============================================================

Логика `source_sync.sync()` проверяется на НАСТОЯЩИХ git-репозиториях, а
граф и хранилище состояния подменены: `RecordingOps` записывает, что и в
каком порядке пришлось бы сделать с графом, `MemoryStore` хранит
состояние и аренду в памяти. Neo4j не нужен; Cypher-часть хранилища
проверяет tests_source_state.py против живой базы.

Сценарии — рабочий цикл из PLAN-11 («Проверка на стенде»), но в
миниатюре: pull, правка агента, возврат канона, круг через хранилище,
порог, сбой, занятая аренда.

Нужен git. Без него набор пропускается целиком (CI-5: `ПУСТО`).

Запуск:
    python tests_source_sync.py -v
"""
from __future__ import annotations

import ast
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from source_state import DELETED, MemoryStore, SourceState
from source_sync import (EXPECTED_SKIPS, OK_STATUSES, SourceContext,
                         _object_key, apply_paths, describe, mark_indexed,
                         plan_owners, sync)
from git_source import detect
from tests_git_source import sh, write

HAVE_GIT = shutil.which("git") is not None
HERE = Path(__file__).resolve().parent


class RecordingOps:
    """Граф-заглушка: записывает вызовы, умеет падать на заданном файле."""

    def __init__(self, fail_on: set | None = None, on_upsert=None):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = set(fail_on or ())
        self.on_upsert = on_upsert
        self.finished = 0

    def _do(self, action: str, rel: str, ok_status: str) -> dict:
        self.calls.append((action, rel))
        if rel in self.fail_on:
            return {"status": "error", "reason": "сбой по заказу теста"}
        return {"status": ok_status}

    def upsert(self, rel):
        if self.on_upsert:
            self.on_upsert(rel)
        return self._do("upsert", rel, "reindexed")

    def remove_object(self, rel):
        return self._do("remove_object", rel, "removed")

    def remove_module(self, rel):
        return self._do("remove_module", rel, "removed")

    def finish(self):
        """Итог в той же форме, что у Neo4jGraphOps.finish (её читает
        apply_changes: log_ownership ждёт настоящий отчёт)."""
        from source_sync import _graph_integrity
        self.finished += 1
        report = _graph_integrity().ownership_report(10, 5, 10, objects=3)
        return {"ownership": report, "stale_debt": 0}

    def touched(self) -> set:
        return {rel for _a, rel in self.calls}


# ─── Чистая раскладка (без git) ───────────────────────────────────────────


class TestPlanOwners(unittest.TestCase):

    def test_object_key(self):
        self.assertEqual(_object_key("Catalogs/Х.xml"), ("", "Catalogs", "Х"))
        self.assertEqual(_object_key("Catalogs/Х/Forms/Ф/Ext/Form.xml"),
                         ("", "Catalogs", "Х"))
        self.assertEqual(_object_key("tests-extension/CommonModules/Т/Ext/Module.bsl"),
                         ("tests-extension/", "CommonModules", "Т"))
        self.assertIsNone(_object_key("Configuration.xml"))
        self.assertIsNone(_object_key("Ext/SessionModule.bsl"))
        self.assertIsNone(_object_key("Catalogs/readme.txt"))

    def test_grouping(self):
        objects, standalone, ignored = plan_owners([
            "Catalogs/Х.xml",
            "Catalogs/Х/Ext/ObjectModule.bsl",
            "Catalogs/Х/Forms/Ф/Ext/Form.xml",
            "CommonModules/М/Ext/Module.bsl",
            "ConfigDumpInfo.xml",
            "Catalogs/Х/Commands/К/Ext/CommandModule.bsl",   # FIX-34 Б: в графе
            "Ext/SessionModule.bsl",                          # FIX-34 А: в графе
            "ExternalDataSources/И/Tables/Т/Ext/ManagerModule.bsl",  # не в графе
        ])
        self.assertEqual(set(objects), {("", "Catalogs", "Х"), ("", "CommonModules", "М")})
        self.assertTrue(objects[("", "Catalogs", "Х")]["xml"])
        self.assertIn("Catalogs/Х/Commands/К/Ext/CommandModule.bsl",
                      objects[("", "Catalogs", "Х")]["modules"])
        self.assertFalse(objects[("", "CommonModules", "М")]["xml"])
        self.assertEqual(standalone, {"Ext/SessionModule.bsl": "Configuration.SessionModule"})
        self.assertEqual(sorted(ignored), [
            "ConfigDumpInfo.xml",
            "ExternalDataSources/И/Tables/Т/Ext/ManagerModule.bsl"])

    def test_statuses_match_apply_changes(self):
        """
        OK_STATUSES и EXPECTED_SKIPS продублированы из apply_changes.py
        (тот модуль нельзя импортировать без побочных эффектов). Сторож
        расхождения: значения обязаны совпадать.
        """
        tree = ast.parse((HERE / "apply_changes.py").read_text(encoding="utf-8"))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                name = getattr(node.targets[0], "id", None)
                if name in ("OK_STATUSES", "EXPECTED_SKIPS"):
                    found[name] = ast.literal_eval(node.value)
        self.assertEqual(found.get("OK_STATUSES"), OK_STATUSES)
        self.assertEqual(found.get("EXPECTED_SKIPS"), EXPECTED_SKIPS)


# ─── Сценарии на репозитории ──────────────────────────────────────────────


@unittest.skipUnless(HAVE_GIT, "git не установлен — набор не проверяет ничего")
class SyncCase(unittest.TestCase):

    subdir = "src"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        sh(self.root, "init", "-q", "-b", "main")
        sh(self.root, "config", "core.autocrlf", "false")
        self.cfg = self.root / self.subdir if self.subdir else self.root
        write(self.root, "AGENTS.md", "служебное\n")
        write(self.cfg, "Configuration.xml", "<c/>\n")
        write(self.cfg, "ConfigDumpInfo.xml", "<d v='1'/>\n")
        write(self.cfg, "Catalogs/Товары.xml", "<cat/>\n")
        write(self.cfg, "Catalogs/Товары/Ext/ObjectModule.bsl", "// объект\n")
        write(self.cfg, "Catalogs/Товары/Ext/ManagerModule.bsl", "// менеджер\n")
        write(self.cfg, "Catalogs/Товары/Forms/Форма/Ext/Form/Module.bsl", "// форма\n")
        write(self.cfg, "Catalogs/Товары/Forms/Форма/Ext/Form.xml", "<form/>\n")
        write(self.cfg, "Documents/Заказ.xml", "<doc/>\n")
        write(self.cfg, "CommonModules/Общий.xml", "<cm/>\n")
        write(self.cfg, "CommonModules/Общий/Ext/Module.bsl", "// общий\n")
        self.c1 = self.commit("первый")
        det = detect(self.root, self.cfg)
        self.assertEqual(det.mode, "git", det.reason)
        self.ctx = SourceContext(det.mode, det.reason, self.root, self.cfg,
                                 det.subdir, det.repo)
        self.store = MemoryStore()
        self.ops = RecordingOps()

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, msg: str) -> str:
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "--allow-empty", "-m", msg)
        return sh(self.root, "rev-parse", "HEAD").strip()

    def adopt(self, sha: str | None = None):
        """Граф собран полностью при HEAD (как после индексера)."""
        self.store.acquire("indexer")
        mark_indexed(self.ctx, self.store, "indexer", sha or self.c1, full=True)
        self.store.release("indexer")

    def tick(self, **kw):
        kw.setdefault("owner", "watcher")
        return sync(self.ctx, self.store, kw.pop("ops", self.ops), **kw)

    def state(self) -> SourceState:
        return self.store.read()


class TestNoState(SyncCase):

    def test_no_state_does_nothing(self):
        r = self.tick()
        self.assertEqual(r.status, "no_state")
        self.assertEqual(self.ops.calls, [])
        self.assertIsNone(self.store.state)

    def test_other_subdir_is_no_state(self):
        self.adopt()
        st = self.state()
        st.config_subdir = "другой"
        self.store.state = st
        self.assertEqual(self.tick().status, "no_state")

    def test_legacy_context(self):
        ctx = SourceContext("legacy", "нет .git", self.root, self.cfg)
        self.assertEqual(sync(ctx, self.store, self.ops, owner="w").status, "legacy")


class TestTick(SyncCase):

    def test_unchanged(self):
        self.adopt()
        r = self.tick()
        self.assertEqual(r.status, "unchanged", r.message)
        self.assertEqual(self.ops.calls, [])
        self.assertEqual(self.state().indexed_sha, self.c1)
        self.assertEqual(self.store.lock_owner, "", "аренда не отпущена")

    def test_pull(self):
        """Pull: объект изменён, модуль удалён, общий модуль добавлен."""
        self.adopt()
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='2'/>\n")
        (self.cfg / "Catalogs/Товары/Ext/ManagerModule.bsl").unlink()
        write(self.cfg, "CommonModules/Новый.xml", "<cm/>\n")
        write(self.cfg, "CommonModules/Новый/Ext/Module.bsl", "// новый\n")
        write(self.cfg, "Documents/Заказ/Ext/ObjectModule.bsl", "// док\n")
        c2 = self.commit("pull")
        r = self.tick()
        self.assertEqual(r.status, "ok", r.message)
        calls = self.ops.calls
        # удаления раньше обновлений
        first_upsert = next(i for i, c in enumerate(calls) if c[0] == "upsert")
        self.assertEqual(calls[:first_upsert],
                         [("remove_module", "Catalogs/Товары/Ext/ManagerModule.bsl")])
        # XML объекта — раньше его модулей, и модули перезаливаются все
        i_xml = calls.index(("upsert", "Catalogs/Товары.xml"))
        for m in ("Catalogs/Товары/Ext/ObjectModule.bsl",
                  "Catalogs/Товары/Forms/Форма/Ext/Form/Module.bsl"):
            self.assertGreater(calls.index(("upsert", m)), i_xml, m)
        self.assertIn(("upsert", "CommonModules/Новый.xml"), calls)
        self.assertIn(("upsert", "CommonModules/Новый/Ext/Module.bsl"), calls)
        # изменился только модуль документа — XML документа не трогаем
        self.assertIn(("upsert", "Documents/Заказ/Ext/ObjectModule.bsl"), calls)
        self.assertNotIn(("upsert", "Documents/Заказ.xml"), calls)
        st = self.state()
        self.assertEqual(st.indexed_sha, c2)
        self.assertEqual(st.dirty, {})
        self.assertEqual(self.ops.finished, 1)

    def test_dump_info_only_commit(self):
        """SRC-1 в связке: коммит, где менялись только корневые файлы."""
        self.adopt()
        write(self.cfg, "ConfigDumpInfo.xml", "<d v='2'/>\n")
        write(self.cfg, "Configuration.xml", "<c v='2'/>\n")
        c2 = self.commit("выгрузка из хранилища")
        r = self.tick()
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.ops.calls, [])
        self.assertEqual(self.state().indexed_sha, c2)

    def test_files_outside_config_are_ignored(self):
        self.adopt()
        write(self.root, "AGENTS.md", "другое\n")
        write(self.root, "openspec/changes/1/impl/x.bsl", "// вне src\n")
        self.commit("служебное")
        self.assertEqual(self.tick().status, "ok")
        self.assertEqual(self.ops.calls, [])

    def test_deleted_object(self):
        self.adopt()
        shutil.rmtree(self.cfg / "Catalogs/Товары")
        (self.cfg / "Catalogs/Товары.xml").unlink()
        self.commit("удалили справочник")
        r = self.tick()
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn(("remove_object", "Catalogs/Товары.xml"), self.ops.calls)
        self.assertNotIn("upsert", {a for a, _ in self.ops.calls})


class TestAgent(SyncCase):

    def test_refresh_then_tick_then_restore_canon(self):
        self.adopt()
        rel = "Catalogs/Товары/Ext/ObjectModule.bsl"
        write(self.cfg, rel, "// правка агента\n")
        r = self.tick(owner="refresh", extra_paths=[rel])
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.ops.calls, [("upsert", rel)])
        self.assertEqual(r.paths, {rel: "reindexed"})
        self.assertIn(rel, self.state().dirty)

        # следующий такт: граф уже = диск, делать нечего
        self.ops.calls.clear()
        self.assertEqual(self.tick().status, "unchanged")
        self.assertEqual(self.ops.calls, [])

        # возврат канона — HEAD не двигается, но граф обязан вернуться
        sh(self.root, "checkout", "--", f"src/{rel}")
        r = self.tick()
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.ops.calls, [("upsert", rel)])
        self.assertEqual(self.state().dirty, {})

    def test_new_module_then_stash(self):
        """Агент добавил общий модуль; потом `git stash -u` убрал его."""
        self.adopt()
        xml = "CommonModules/ОтАгента.xml"
        bsl = "CommonModules/ОтАгента/Ext/Module.bsl"
        write(self.cfg, xml, "<cm/>\n")
        write(self.cfg, bsl, "// новый\n")
        r = self.tick(owner="refresh", extra_paths=[xml, bsl])
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.ops.calls, [("upsert", xml), ("upsert", bsl)])
        self.assertEqual(self.state().dirty.keys(), {xml, bsl})

        self.ops.calls.clear()
        sh(self.root, "stash", "push", "-u", "-q", "--", "src")
        r = self.tick()
        self.assertEqual(r.status, "ok", r.message)
        self.assertIn(("remove_object", xml), self.ops.calls)
        self.assertEqual(self.state().dirty, {})

    def test_unreported_change_found_by_reconcile(self):
        self.adopt()
        rel = "CommonModules/Общий/Ext/Module.bsl"
        write(self.cfg, rel, "// руками, без сигнала\n")
        self.assertEqual(self.tick().status, "unchanged")
        self.assertEqual(self.ops.calls, [])
        r = self.tick(reconcile=True)
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.ops.calls, [("upsert", rel)])
        st = self.state()
        self.assertIn(rel, st.dirty)
        self.assertFalse(st.needs_reconcile)
        self.assertGreater(st.last_reconcile_at, 0)

    def test_round_trip_through_storage(self):
        """
        Правка агента ушла в хранилище и вернулась коммитом с тем же
        содержимым: из dirty уходит, перезаливать нечего.
        """
        self.adopt()
        rel = "CommonModules/Общий/Ext/Module.bsl"
        write(self.cfg, rel, "// правка, ушедшая в хранилище\n")
        self.tick(owner="refresh", extra_paths=[rel])
        self.ops.calls.clear()
        c2 = self.commit("вернулось из хранилища")
        r = self.tick()
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.ops.calls, [])
        st = self.state()
        self.assertEqual((st.indexed_sha, st.dirty), (c2, {}))

    def test_round_trip_reformatted_by_designer(self):
        """Конфигуратор пересохранил файл иначе — перезаливается."""
        self.adopt()
        rel = "CommonModules/Общий/Ext/Module.bsl"
        write(self.cfg, rel, "//правка агента\n")
        self.tick(owner="refresh", extra_paths=[rel])
        self.ops.calls.clear()
        write(self.cfg, rel, "// правка агента (как сохранил конфигуратор)\n")
        self.commit("вернулось переформатированным")
        self.tick()
        self.assertEqual(self.ops.calls, [("upsert", rel)])
        self.assertEqual(self.state().dirty, {})

    def test_refresh_path_forms(self):
        self.adopt()
        rel = "Catalogs/Товары/Ext/ObjectModule.bsl"
        write(self.cfg, rel, "// правка\n")
        win = "D:\\projects\\ubd\\src\\" + rel.replace("/", "\\")
        r = self.tick(owner="refresh", extra_paths=[
            win, "AGENTS.md", "../вне.bsl", "Configuration.xml",
            "openspec/changes/x.md"])
        self.assertEqual(self.ops.calls, [("upsert", rel)])
        self.assertEqual(r.paths["AGENTS.md"], "outside_config")
        self.assertEqual(r.paths["../вне.bsl"], "outside_config")
        self.assertEqual(r.paths["Configuration.xml"], "not_in_graph")
        self.assertEqual(r.paths["openspec/changes/x.md"], "outside_config")
        self.assertEqual(r.paths[rel], "reindexed")

    def test_refresh_of_unchanged_file(self):
        self.adopt()
        r = self.tick(owner="refresh", extra_paths=["Documents/Заказ.xml"])
        self.assertEqual(r.paths, {"Documents/Заказ.xml": "unchanged"})
        self.assertEqual(self.ops.calls, [])

    def test_refresh_of_deleted_file(self):
        self.adopt()
        rel = "Catalogs/Товары/Ext/ManagerModule.bsl"
        (self.cfg / rel).unlink()
        r = self.tick(owner="refresh", extra_paths=[rel])
        self.assertEqual(self.ops.calls, [("remove_module", rel)])
        self.assertEqual(self.state().dirty[rel], DELETED)
        self.assertEqual(r.paths[rel], "removed")

    def test_edit_after_refresh_without_signal(self):
        """Агент поправил файл ещё раз и не сказал — такт заметит по dirty."""
        self.adopt()
        rel = "Documents/Заказ.xml"
        write(self.cfg, rel, "<doc v='1'/>\n")
        self.tick(owner="refresh", extra_paths=[rel])
        self.ops.calls.clear()
        write(self.cfg, rel, "<doc v='2'/>\n")
        self.tick()
        self.assertEqual(self.ops.calls, [("upsert", rel)])


class TestSafety(SyncCase):

    def _many_objects(self, n: int) -> str:
        self.adopt()
        for i in range(n):
            write(self.cfg, f"Catalogs/К{i}.xml", f"<k i='{i}'/>\n")
        return self.commit("много")

    def test_threshold_sets_needs_full_without_reading_files(self):
        c2 = self._many_objects(3)
        r = self.tick(max_partial=2)
        self.assertEqual(r.status, "needs_full", r.message)
        self.assertEqual(self.ops.calls, [])
        st = self.state()
        self.assertTrue(st.needs_full)
        self.assertEqual(st.indexed_sha, self.c1)
        # порог подняли — догоняет и снимает флаг
        r = self.tick(max_partial=10)
        self.assertEqual(r.status, "ok", r.message)
        st = self.state()
        self.assertFalse(st.needs_full)
        self.assertEqual(st.indexed_sha, c2)

    def test_failure_keeps_state(self):
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='3'/>\n")
        write(self.cfg, "CommonModules/Общий/Ext/Module.bsl", "// 3\n")
        self.commit("pull")
        ops = RecordingOps(fail_on={"Documents/Заказ.xml"})
        r = self.tick(ops=ops)
        self.assertEqual(r.status, "error")
        st = self.state()
        self.assertEqual(st.indexed_sha, self.c1, "сдвинули состояние после сбоя")
        self.assertIn("не обновлено", st.last_error)
        # починили — догоняет
        r = self.tick()
        self.assertEqual(r.status, "ok", r.message)
        self.assertEqual(self.state().last_error, "")

    def test_busy(self):
        self.adopt()
        self.store.acquire("indexer", ttl=3600)
        write(self.cfg, "Documents/Заказ.xml", "<doc v='4'/>\n")
        r = self.tick(extra_paths=["Documents/Заказ.xml"])
        self.assertEqual(r.status, "busy")
        self.assertEqual(r.lease_holder, "indexer")
        self.assertEqual(r.paths, {"Documents/Заказ.xml": "busy"})
        self.assertEqual(self.ops.calls, [])
        self.assertEqual(self.store.lock_owner, "indexer", "чужую аренду отпустили")

    def test_expired_lease_is_taken_over(self):
        self.adopt()
        self.store.acquire("упавший", ttl=-1)
        self.assertEqual(self.tick().status, "unchanged")

    def test_lease_lost_mid_apply(self):
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='5'/>\n")
        write(self.cfg, "CommonModules/Общий.xml", "<cm v='5'/>\n")
        self.commit("pull")

        def steal(_rel):
            self.store.force_unlock()
            self.store.acquire("другой")
        r = self.tick(ops=RecordingOps(on_upsert=steal))
        self.assertEqual(r.status, "busy", r.message)
        self.assertEqual(self.state().indexed_sha, self.c1)

    def test_dry_run_changes_nothing(self):
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='6'/>\n")
        self.commit("pull")
        writes = self.store.writes
        r = self.tick(dry_run=True, ops=None)
        self.assertEqual(r.status, "dry_run")
        self.assertEqual(r.planned, ["Documents/Заказ.xml"])
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(self.store.lock_owner, "")

    def test_rewritten_history(self):
        self.adopt("0" * 40)
        r = self.tick()
        self.assertEqual(r.status, "needs_full")
        self.assertIn("нет в репозитории", r.message)

    def test_dirty_entries_outside_graph_are_dropped(self):
        self.adopt()
        st = self.state()
        st.dirty = {"ConfigDumpInfo.xml": "abc"}
        self.store.state = st
        self.tick()
        self.assertEqual(self.state().dirty, {})


class TestLineEndings(SyncCase):

    def test_crlf_on_disk_is_not_a_change(self):
        self.adopt()
        rel = "CommonModules/Общий/Ext/Module.bsl"
        write(self.cfg, rel, "// общий\n", newline="\r\n")
        r = self.tick(owner="refresh", extra_paths=[rel])
        self.assertEqual(self.ops.calls, [])
        self.assertEqual(self.state().dirty, {})
        self.assertGreaterEqual(r.normalized_matches, 1)


class TestRootLayout(SyncCase):
    """Своя выгрузка: конфигурация в корне репозитория."""

    subdir = ""

    def test_pull_in_root_layout(self):
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='7'/>\n")
        write(self.root, "AGENTS.md", "изменён\n")
        self.commit("pull")
        self.tick()
        self.assertEqual(self.ops.calls, [("upsert", "Documents/Заказ.xml")])


class TestDescribe(SyncCase):

    def test_describe(self):
        d = describe(None, self.ctx)
        self.assertEqual(d["mode"], "git")
        self.assertIn("нет", d["state"])
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='8'/>\n")
        self.commit("pull")
        d = describe(self.store.read(), self.ctx)
        self.assertFalse(d["in_sync"])
        self.assertEqual(d["behind_files"], 1)
        self.assertTrue(d["needs_reconcile"])


class _LayerOnlyNeo:
    """
    Neo4j, который отвечает только на проверку «слой метаданных есть»
    (иначе incremental откажется обновлять BSL), а на остальное — пусто.
    Записи никуда не идут: проверяется связка source_sync → incremental
    (имена функций, форма ответов, статусы), а не сам граф.
    """

    LAYER = "MATCH (m:MetadataObject) RETURN count(m) AS n"

    def __init__(self):
        self.calls = 0

    def query(self, cypher, parameters=None, include_stats=False):
        self.calls += 1
        return {"results": [{"columns": [], "data": [], "stats": {}}]}

    def rows(self, cypher, parameters=None):
        self.calls += 1
        return [{"n": 5}] if cypher.strip() == self.LAYER else []

    def rows_with_stats(self, cypher, parameters=None):
        self.calls += 1
        return [], {}


class TestRealIncremental(SyncCase):
    """
    Сквозной прогон с боевыми Neo4jGraphOps и настоящим incremental.py на
    настоящих XML/BSL. Все остальные тесты здесь подменяют граф записью
    вызовов — и не заметили бы, если бы incremental поменял имена функций
    или форму ответа.
    """

    def setUp(self):
        super().setUp()
        from tests_metadata_xml import CATALOG_XML
        write(self.cfg, "Catalogs/АукАукционы.xml", CATALOG_XML)
        write(self.cfg, "Catalogs/АукАукционы/Ext/ObjectModule.bsl",
              "Процедура ПередЗаписью(Отказ)\n\tСообщить(\"x\");\nКонецПроцедуры\n")
        write(self.cfg, "CommonModules/Общий/Ext/Module.bsl",
              "Функция Сумма(А, Б) Экспорт\n\tВозврат А + Б;\nКонецФункции\n")
        self.c1 = self.commit("с настоящими XML/BSL")
        self.adopt(self.c1)
        self.neo = _LayerOnlyNeo()

    def real_tick(self, **kw):
        from source_sync import Neo4jGraphOps
        kw.setdefault("owner", "e2e")
        return sync(self.ctx, self.store, Neo4jGraphOps(self.neo, self.cfg), **kw)

    def test_pull_agent_and_delete(self):
        from tests_metadata_xml import CATALOG_XML
        write(self.cfg, "Catalogs/АукАукционы.xml",
              CATALOG_XML.replace("Тестовый", "Тестовый 2"))
        write(self.cfg, "CommonModules/Общий/Ext/Module.bsl",
              "Функция Сумма(А, Б) Экспорт\n\tВозврат А + Б + 1;\nКонецФункции\n")
        self.commit("pull")
        r = self.real_tick()
        self.assertEqual(r.status, "ok", r.as_dict())
        got = {(f["action"], f["rel"]): f["status"] for f in r.applied.files}
        self.assertEqual(got[("upsert", "Catalogs/АукАукционы.xml")], "reindexed")
        self.assertEqual(got[("upsert", "Catalogs/АукАукционы/Ext/ObjectModule.bsl")],
                         "reindexed")
        self.assertEqual(got[("upsert", "CommonModules/Общий/Ext/Module.bsl")],
                         "reindexed")
        self.assertIn("ownership", r.applied.finish)
        self.assertGreater(self.neo.calls, 0)

        new = "CommonModules/Новый/Ext/Module.bsl"
        write(self.cfg, new, "Процедура П() Экспорт\nКонецПроцедуры\n")
        r = self.real_tick(extra_paths=[str(self.cfg / new)])
        self.assertEqual(r.paths, {new: "reindexed"})
        self.assertEqual(r.dirty_count, 1)

        os.remove(self.cfg / new)
        r = self.real_tick()
        self.assertEqual([(f["action"], f["status"]) for f in r.applied.files],
                         [("remove_module", "removed")])
        self.assertEqual(r.dirty_count, 0)


class TestFix34InSync(SyncCase):
    """Правка XML справочника перезаливает модули его команд (FIX-34 Б)."""

    def test_object_xml_reupserts_command_module(self):
        cmd = "Catalogs/Товары/Commands/Печать/Ext/CommandModule.bsl"
        write(self.cfg, cmd, "&НаКлиенте\nПроцедура ОбработкаКоманды(П, ПВ)\nКонецПроцедуры\n")
        c2 = self.commit("команда")
        self.adopt(c2)
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='команды'/>\n")
        self.commit("pull")
        self.tick()
        calls = self.ops.calls
        self.assertIn(("upsert", cmd), calls)
        self.assertLess(calls.index(("upsert", "Catalogs/Товары.xml")),
                        calls.index(("upsert", cmd)))


class TestApplyPathsAlone(unittest.TestCase):

    def test_standalone_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root, "CommonModules/М.xml", "<x/>\n")
            ops = RecordingOps()
            res = apply_paths(ops, root, ["CommonModules/М.xml",
                                          "CommonModules/М/Ext/Module.bsl"])
            # XML есть, модуля на диске нет: удалить модуль, залить XML
            self.assertEqual(ops.calls, [
                ("remove_module", "CommonModules/М/Ext/Module.bsl"),
                ("upsert", "CommonModules/М.xml"),
            ])
            self.assertTrue(res.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
