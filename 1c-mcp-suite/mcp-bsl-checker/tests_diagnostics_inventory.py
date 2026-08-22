"""
CFG-5 / CFG-6. Состав правил, который никто не проверял.
=========================================================

Что было
────────
`CFG-4` научился отвечать, применён ли файл настроек. Что в нём написано —
не проверял никто. При `mode: ONLY` включены ТОЛЬКО перечисленные
диагностики, поэтому опечатка в имени (`UsingModalWindws`) означает не
лишнюю строку, а молча выключенную проверку: BSL LS игнорирует незнакомые
ключи и об этом не сообщает. `diagnostics_enabled` при этом показывает на
единицу больше правды.

Почему не JSON Schema и не stderr
──────────────────────────────────
`PLAN-9` предлагал ловить предупреждения BSL LS в stderr при прогреве и
сам признавал, что тот может ничего не писать — проверить это можно только
на стенде.

Проверять нечего: состав диагностик лежит в самом jar. BSL LS выводит код
диагностики из имени класса (`EmptyCodeBlockDiagnostic` → `EmptyCodeBlock`),
и это не наше соглашение, а его механизм разрешения кодов. Список,
собранный по именам классов, разойтись с анализатором не может.

Схему пришлось бы тянуть в образ и держать в соответствии с
`BSL_LS_VERSION` руками — то есть завести шестой рукописный список
проекта. Здесь источник и есть исполняемый файл: обновили версию — список
обновился сам. Это же закрывает `CFG-6`: новые диагностики следующих
версий видны числом `not_declared_count`, и оно меняется от обновления
само, без теста-напоминалки.

Здесь строится НАСТОЯЩИЙ zip — потому что проверяется чтение архива, а
подменённый `zipfile` проверял бы регулярное выражение.

Запуск:  python3 tests_diagnostics_inventory.py -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import bsl_config
from bsl_health import (
    _DIAG_SANITY_MIN, diagnostics_inventory, health_report, jar_diagnostics,
)

PKG = "com/github/_1c_syntax/bsl/languageserver/diagnostics/"

# Настоящие коды BSL LS: те, что стоят в bsl-config/bsl-language-server.json.
REAL_CODES = [
    "EmptyCodeBlock", "EmptyRegion", "EmptyStatement", "UnusedLocalMethod",
    "UsingModalWindows", "DeprecatedFind", "ExecuteExternalCode",
    "CommonModuleAssign", "DisableSafeMode", "ExportVariables",
]


def _make_jar(tmp: Path, codes, extra=(), name="bsl-ls.jar") -> str:
    """jar с классами диагностик — ровно той раскладки, что у настоящего."""
    path = tmp / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF",
                    "Manifest-Version: 1.0\nImplementation-Version: 0.28.5\n")
        for code in codes:
            zf.writestr(f"{PKG}{code}Diagnostic.class", b"\xca\xfe\xba\xbe")
        for name_ in extra:
            zf.writestr(name_, b"\xca\xfe\xba\xbe")
    return str(path)


def _pad(n: int) -> list[str]:
    """Добивка до порога вменяемости: реальный jar знает сотни диагностик."""
    return [f"Synthetic{i}" for i in range(n)]


class TestJarDiagnostics(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_codes_are_class_names_without_suffix(self):
        jar = _make_jar(self.dir, REAL_CODES + _pad(_DIAG_SANITY_MIN))
        found = jar_diagnostics(jar)
        for code in REAL_CODES:
            self.assertIn(code, found)
        self.assertNotIn("EmptyCodeBlockDiagnostic", found,
                         "суффикс Diagnostic — часть имени класса, не кода")

    def test_inner_classes_do_not_duplicate(self):
        """
        `XxxDiagnostic$1.class` — вложенный класс с тем же кодом. Без
        отсева он дал бы дубль, а через set — незаметно съел бы проверку
        на количество.
        """
        extra = [f"{PKG}EmptyCodeBlockDiagnostic$1.class",
                 f"{PKG}EmptyCodeBlockDiagnostic$Inner.class"]
        jar = _make_jar(self.dir, REAL_CODES + _pad(_DIAG_SANITY_MIN),
                        extra=extra)
        found = jar_diagnostics(jar)
        self.assertIn("EmptyCodeBlock", found)
        self.assertFalse([f for f in found if "$" in f])

    def test_other_classes_are_ignored(self):
        extra = [f"{PKG.replace('diagnostics/', '')}LanguageServer.class",
                 "com/other/DiagnosticHelper.class",
                 f"{PKG}infrastructure/DiagnosticsConfiguration.class"]
        jar = _make_jar(self.dir, REAL_CODES + _pad(_DIAG_SANITY_MIN),
                        extra=extra)
        found = jar_diagnostics(jar)
        self.assertNotIn("DiagnosticHelper", found)
        self.assertNotIn("LanguageServer", found)

    def test_too_few_means_we_did_not_understand_the_jar(self):
        """
        Раскладка архива могла смениться. Тогда честный ответ — «не знаю»,
        а не «все ваши 85 диагностик выдуманы»: сторож, который при
        непонимании обвиняет пользователя, хуже отсутствующего.
        """
        jar = _make_jar(self.dir, REAL_CODES[:3])
        self.assertEqual(jar_diagnostics(jar), set())

    def test_missing_jar_is_not_a_crash(self):
        self.assertEqual(jar_diagnostics(self.dir / "нет-такого.jar"), set())

    def test_not_a_zip_is_not_a_crash(self):
        p = self.dir / "мусор.jar"
        p.write_bytes("это не архив".encode("utf-8"))
        self.assertEqual(jar_diagnostics(p), set())


class TestInventory(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.jar = _make_jar(self.dir, REAL_CODES + _pad(_DIAG_SANITY_MIN))

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self, names):
        return {"applied": True, "diagnostics_names": sorted(names)}

    def test_clean_config_has_no_unknown(self):
        out = diagnostics_inventory(self.jar, self._config(REAL_CODES))
        self.assertEqual(out["unknown"], [])
        self.assertNotIn("warning", out)

    def test_typo_is_named(self):
        """Мера приёмки CFG-5: опечатка видна в bsl_stats, а не в логах."""
        names = [c for c in REAL_CODES if c != "UsingModalWindows"]
        names.append("UsingModalWindws")
        out = diagnostics_inventory(self.jar, self._config(names))
        self.assertEqual(out["unknown"], ["UsingModalWindws"])
        self.assertIn("UsingModalWindws", out["warning"])
        self.assertIn("mode: ONLY", out["warning"],
                      "надо сказать не только ЧТО, но и чем это грозит")

    def test_counts_what_is_not_declared(self):
        """CFG-6: сколько диагностик jar знает, а файл настроек — нет."""
        out = diagnostics_inventory(self.jar, self._config(REAL_CODES[:4]))
        self.assertEqual(out["known_in_jar"], len(REAL_CODES) + _DIAG_SANITY_MIN)
        self.assertEqual(out["not_declared_count"],
                         out["known_in_jar"] - 4)

    def test_names_of_not_declared_are_not_listed(self):
        """
        Их сотни. Список в каждом ответе `bsl_stats` — это контекст агента,
        потраченный на то, что лежит в файле рядом.
        """
        out = diagnostics_inventory(self.jar, self._config(REAL_CODES[:4]))
        self.assertNotIn("not_declared", out)

    def test_unreadable_jar_says_so_instead_of_accusing(self):
        bad = _make_jar(self.dir, REAL_CODES[:2], name="мал.jar")
        out = diagnostics_inventory(bad, self._config(REAL_CODES))
        self.assertEqual(out["known_in_jar"], 0)
        self.assertIn("note", out)
        self.assertNotIn("unknown", out)
        self.assertNotIn("warning", out)


class TestConfigExposesNames(unittest.TestCase):
    """`describe` обязан назвать состав — иначе сверять нечего."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bsl-language-server.json"
        self.path.write_text(json.dumps({
            "diagnostics": {
                "mode": "ONLY",
                "parameters": {"EmptyCodeBlock": True,
                               "UnusedLocalMethod": False,
                               "CyclomaticComplexity": {"maxComplexity": 15}},
            },
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_names_are_listed(self):
        info = bsl_config.describe(str(self.path))
        self.assertEqual(
            info["diagnostics_names"],
            ["CyclomaticComplexity", "EmptyCodeBlock", "UnusedLocalMethod"])

    def test_disabled_names_are_separate(self):
        info = bsl_config.describe(str(self.path))
        self.assertEqual(info["diagnostics_disabled_names"],
                         ["UnusedLocalMethod"])

    def test_counts_unchanged(self):
        """Правка добавляет имена, а не меняет прежние числа."""
        info = bsl_config.describe(str(self.path))
        self.assertEqual(info["diagnostics_declared"], 3)
        self.assertEqual(info["diagnostics_enabled"], 2,
                         "объект с параметрами — это включённая диагностика")


class TestWiredIntoStats(unittest.TestCase):
    """
    Сквозная проверка: сверка обязана доехать до `bsl_stats`. Тот же урок,
    что `FIX-30`, — механизм существовал, а до ответа не доходил.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.jar = _make_jar(self.dir, REAL_CODES + _pad(_DIAG_SANITY_MIN))

    def tearDown(self):
        self.tmp.cleanup()

    def _report(self, names, applied=True):
        return health_report(
            jar_path=self.jar,
            java_probe=lambda: {"available": True, "version": "17"},
            jar_probe=lambda: {"present": True, "version": "0.28.5"},
            config_report={"applied": applied, "requested": True,
                           "path": "/app/bsl-config/bsl-language-server.json",
                           "diagnostics_names": sorted(names)},
        )

    def test_check_lands_in_config_section(self):
        rep = self._report(REAL_CODES)
        self.assertIn("diagnostics_check", rep["config"])
        self.assertEqual(rep["config"]["diagnostics_check"]["unknown"], [])

    def test_typo_degrades_the_answer(self):
        """
        `OBS-1`: ответ пригоден, но хуже штатного. Молча выключенная
        проверка — это ровно тот случай, ради которого поле заводилось.
        """
        rep = self._report(REAL_CODES + ["ТакойНетНигде"])
        self.assertTrue(rep["degraded"])
        self.assertTrue(rep["config"]["diagnostics_check"]["unknown"])

    def test_full_name_lists_do_not_leak_into_the_answer(self):
        rep = self._report(REAL_CODES)
        self.assertNotIn("diagnostics_names", rep["config"])
        self.assertNotIn("diagnostics_disabled_names", rep["config"])

    def test_unapplied_config_is_not_inventoried(self):
        """
        У непринятого конфига объявлять нечего, а секция про его состав
        читалась бы как «правила всё-таки в силе».
        """
        rep = self._report(REAL_CODES, applied=False)
        self.assertNotIn("diagnostics_check", rep["config"])

    def test_missing_jar_skips_the_check_without_noise(self):
        rep = health_report(
            jar_path=str(self.dir / "нет.jar"),
            java_probe=lambda: {"available": True, "version": "17"},
            jar_probe=lambda: {"present": False, "error": "файла нет"},
            config_report={"applied": True, "requested": True,
                           "diagnostics_names": REAL_CODES},
        )
        self.assertNotIn("diagnostics_check", rep["config"])


class TestRealProjectConfig(unittest.TestCase):
    """
    Файл настроек проекта — не абстракция. Если он разъедется с тем, что
    умеет читать `describe`, узнать об этом лучше здесь.
    """

    def setUp(self):
        self.path = (Path(__file__).resolve().parents[2]
                     / "bsl-config" / "bsl-language-server.json")
        if not self.path.exists():
            self.skipTest("bsl-config/ рядом нет")

    def test_names_are_read(self):
        info = bsl_config.describe(str(self.path))
        self.assertTrue(info["applied"])
        self.assertEqual(len(info["diagnostics_names"]),
                         info["diagnostics_declared"])

    def test_the_only_disabled_one_is_known(self):
        """
        `FIX-30` проверяется примером в датасете именно на этой
        диагностике. Если её выключение уберут из файла, пример начнёт
        мерить не то, и покраснеть он должен здесь, а не на стенде.
        """
        info = bsl_config.describe(str(self.path))
        self.assertIn("UnusedLocalMethod", info["diagnostics_disabled_names"])
        self.assertIn("EmptyCodeBlock", info["diagnostics_names"])
        self.assertNotIn("EmptyCodeBlock", info["diagnostics_disabled_names"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
