"""
DOC-3 / DOC-4 / DOC-5. Канон корня.
====================================

Что чинилось
────────────
В корне лежало 59 файлов `.md`. Пришедший со стороны видел их все сразу и
не мог отличить действующую документацию от разбора правки, сделанной в
мае.

Хуже, чем шум: два из трёх файлов, которые новичок откроет по названию, не
были тем, чем назывались. `00-ЧИТАТЬ-ПЕРВЫМ.md` — сопроводиловка к архиву
захода про стандарты. `УСТАНОВКА.md` — сопроводиловка к архиву
`FIX-3 + FIX-4.2`, начинающаяся словами «разворачивается поверх
D:\\Docker\\30_mcp_cursor». Настоящая установка описана в `README.md`.

Почему одной уборки мало
────────────────────────
Уборка — событие, а зарастание — процесс. Корень зарастал пять заходов
подряд, каждый раз по одному файлу, и каждый раз это было разумно в тот
момент: разбор надо было куда-то положить. Значит, уборка без сторожа
продержится ровно до следующего разбора.

Сторож простой: **список того, что в корне разрешено, а не того, что
запрещено**. Запретительный список пришлось бы пополнять на каждый новый
файл, то есть он молчал бы про всё, чего в нём нет, — а именно новые файлы
и составляют проблему.

Заодно проверяется, что переезд не оставил битых ссылок: документ,
ссылающийся на `PLAN-5.md` из корня, после `DOC-3` показывает в пустоту.

Запуск:  python3 scripts/tests_docs_canon.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "docs" / "archive"

# Канон. Держится здесь, а не в скрипте переноса, потому что спрашивают
# об этом списке отсюда — а скрипт запускается один раз в жизни проекта.
#
# DOC-7 добавил сюда `ОТКРЫТОЕ.md`. Пополнять канон можно, но только так:
# осознанным решением, с ответом на вопрос «почему этот файл обязан лежать
# в корне». Здесь ответ такой: открытые пункты читают ровно так же часто,
# как статус, и класть их в архив нельзя — архив для того, что закончилось.
CANON = {"README.md", "ARCHITECTURE.md", "PROMPTS.md", "СТАТУС.md",
         "ОТКРЫТОЕ.md"}

# Текущий план захода. Ровно один: два плана в корне означают, что
# предыдущий забыли отправить в архив, и читатель не знает, какой из них
# действующий.
PLAN_RE = re.compile(r"^PLAN(-\d+)?\.md$")


class TestCanonListsAgree(unittest.TestCase):
    """
    FIX-34. Канон корня описан ДВАЖДЫ: здесь и в `scripts/archive_docs.py`.
    Списки разошлись — в скрипте не было `ОТКРЫТОЕ.md`, и он унёс бы его в
    архив; сторож после этого упал бы на «канон неполон», то есть уборка
    ломала бы проверку, которая её стережёт.

    Седьмое расхождение производного списка с источником в проекте.
    Слить в один список нельзя: скрипт хранит рядом с именем пояснение
    «зачем файл нужен», а тест — только имена. Поэтому сверяем множества,
    как в `FIX-32` и `LOCK-1`.
    """

    def script_canon(self) -> set:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import archive_docs
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"archive_docs.py не импортируется: {e}")
        # LICENSE каноничен для скрипта (он переносит .md, а лицензия —
        # не .md), тесту он не интересен: тот смотрит только на *.md.
        return {n for n in archive_docs.CANON if n.endswith(".md")}

    def test_the_two_lists_match(self):
        missing = sorted(CANON - self.script_canon())
        self.assertFalse(
            missing,
            f"канон требует эти файлы в корне, а archive_docs.py считает их "
            f"лишними и унесёт в архив: {missing}",
        )

    def test_script_does_not_keep_extra(self):
        extra = sorted(self.script_canon() - CANON)
        self.assertFalse(
            extra,
            f"archive_docs.py оставляет в корне то, чего канон не знает: "
            f"{extra}",
        )

    def test_plan_is_not_hardcoded(self):
        """
        В скрипте стояло прошитое `PLAN-6.md`: встретив `PLAN-9.md`, он
        счёл бы действующий план лишним. Теперь план выводится из корня —
        проверяем, что выводится тот же, что видит сторож.
        """
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import archive_docs
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"archive_docs.py не импортируется: {e}")
        in_root = {p.name for p in ROOT.glob("*.md")
                   if p.is_file() and PLAN_RE.match(p.name)}
        self.assertEqual(archive_docs.current_plans(), in_root)
        self.assertFalse(
            [p.name for p in archive_docs.to_move() if PLAN_RE.match(p.name)],
            "действующий план в списке на перенос в архив",
        )


class TestRootIsClean(unittest.TestCase):

    def root_md(self) -> set[str]:
        return {p.name for p in ROOT.glob("*.md") if p.is_file()}

    def test_only_canon_and_current_plan(self):
        extra = sorted(n for n in self.root_md()
                       if n not in CANON and not PLAN_RE.match(n))
        self.assertFalse(
            extra,
            f"в корне завелось лишнее: {extra}\n\n"
            f"Корень — оглавление, которое читается целиком за один "
            f"взгляд. Разборы правок и итоги заходов живут в "
            f"docs/archive/; перенести и пересобрать указатель:\n"
            f"  python3 scripts/archive_docs.py\n"
            f"  python3 scripts/gen_archive_index.py",
        )

    def test_canon_files_all_exist(self):
        missing = sorted(CANON - self.root_md())
        self.assertFalse(missing, f"канон неполон, нет файлов: {missing}")

    def test_exactly_one_plan_in_root(self):
        plans = sorted(n for n in self.root_md() if PLAN_RE.match(n))
        self.assertEqual(
            len(plans), 1,
            f"планов в корне {len(plans)} ({plans}) — должен остаться "
            f"только текущий, остальные в docs/archive/. Иначе читателю "
            f"нечем отличить действующий план от прошлого",
        )

    def test_status_and_open_name_the_current_plan(self):
        """
        DOC-9. Сторож `test_exactly_one_plan_in_root` был зелёным, когда
        `СТАТУС.md` и `ОТКРЫТОЕ.md` оба называли действующим планом
        `PLAN-8.md`, — потому что файл существовал. В архиве.

        Проверка «файл есть» отвечает не на тот вопрос. Читатель приходит в
        статус за ответом «где мы сейчас», и ссылка на прошлый заход даёт
        ему устаревший ответ, выглядящий действующим. Ровно тот жанр, из-за
        которого `DOC-5` завёл предупреждение в шапке `PLAN.md`.
        """
        plans = sorted(n for n in self.root_md() if PLAN_RE.match(n))
        if len(plans) != 1:
            self.skipTest("планов в корне не один — про это отдельный тест")
        current = plans[0]
        for name in ("СТАТУС.md", "ОТКРЫТОЕ.md"):
            doc = ROOT / name
            if not doc.exists():
                continue
            head = doc.read_text(encoding="utf-8", errors="replace")[:600]
            named = set(re.findall(r"`(PLAN(?:-\d+)?\.md)`", head))
            with self.subTest(doc=name):
                self.assertIn(
                    current, named,
                    f"{name} в шапке называет {sorted(named) or 'ничего'}, "
                    f"а действующий план — {current}. Файл из архива "
                    f"существует, поэтому проверка ссылок молчит: "
                    f"устаревший указатель выглядит рабочим",
                )

    def test_archive_exists_and_is_not_empty(self):
        self.assertTrue(ARCHIVE.is_dir(), "docs/archive/ нет")
        files = [p for p in ARCHIVE.glob("*.md") if p.name != "README.md"]
        self.assertGreater(len(files), 20,
                           "в архиве подозрительно пусто — переезд не прошёл?")


class TestArchiveIndexMatchesArchive(unittest.TestCase):
    """
    Указатель обязан совпадать с тем, на что указывает.

    Шестой такой список в проекте. Предыдущие пять расходились молча:
    `LOCK-1` (пара есть в `.sh`, нет в `.ps1`), `B-6` (три набора со
    своими списками образов), `HYG-2`, `HYG-4` (конфиг с четырьмя
    серверами из пяти). Здесь список порождается скриптом, но сверять его
    всё равно надо: порождённое тоже стареет, если генератор не запустили.
    """

    def setUp(self):
        self.index = ARCHIVE / "README.md"
        if not self.index.exists():
            self.skipTest("указателя нет — DOC-3 не выполнен")
        self.text = self.index.read_text(encoding="utf-8")

    def test_every_archived_file_is_listed(self):
        files = {p.name for p in ARCHIVE.glob("*.md") if p.name != "README.md"}
        missing = sorted(n for n in files if f"`{n}`" not in self.text)
        self.assertFalse(
            missing,
            f"в архиве есть, в указателе нет: {missing}\n"
            f"Пересобрать: python3 scripts/gen_archive_index.py",
        )

    def test_index_does_not_list_ghosts(self):
        listed = set(re.findall(r"`([^`]+\.md)`", self.text))
        on_disk = {p.name for p in ARCHIVE.glob("*.md")}
        # Указатель ссылается и на файлы канона — они лежат в корне.
        ghosts = sorted(n for n in listed
                        if n not in on_disk and not (ROOT / n).exists())
        self.assertFalse(ghosts, f"указатель ссылается на несуществующее: {ghosts}")


class TestNoBrokenLinksAfterMove(unittest.TestCase):
    """
    Переезд 55 файлов ломает ссылки молча: markdown не проверяется ничем.

    Проверяются только те упоминания, которые выглядят как указание пути
    (`` `имя.md` ``) в действующих документах корня. Архив не проверяем —
    там документы ссылаются друг на друга и лежат рядом.
    """

    def test_root_docs_point_to_existing_files(self):
        broken = []
        for doc in sorted(ROOT.glob("*.md")):
            text = doc.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"`([А-Яа-яA-Za-z0-9_./-]+\.md)`", text):
                target = m.group(1)
                if "*" in target:
                    continue
                if (ROOT / target).exists() or (ARCHIVE / target).exists():
                    continue
                if (doc.parent / target).exists():
                    continue
                line = text[:m.start()].count("\n") + 1
                broken.append(f"{doc.name}:{line} → {target}")
        self.assertFalse(
            broken,
            "ссылки в корне показывают в пустоту:\n  " + "\n  ".join(broken),
        )


class TestHistoricalPlanIsMarked(unittest.TestCase):
    """
    DOC-5. `PLAN.md` описывает проект, которого нет: девять серверов,
    SonarQube, оркестратор, клиент opencode, статус трёхмесячной давности.

    Опасность не в устаревании, а в том, что он **выглядит действующим** —
    правильное имя, живой тон, галочки у закрытых задач. Такой файл читают
    как инструкцию. Поэтому предупреждение обязано стоять первым, до
    любого содержания: читатель, дошедший до второго экрана, уже поверил.
    """

    def setUp(self):
        self.plan = ARCHIVE / "PLAN.md"
        if not self.plan.exists():
            self.skipTest("PLAN.md не в архиве")
        self.text = self.plan.read_text(encoding="utf-8")

    def test_warning_is_the_very_first_thing(self):
        first = next(l for l in self.text.splitlines() if l.strip())
        self.assertTrue(
            first.startswith(">"),
            f"первая строка PLAN.md — не предупреждение, а {first[:50]!r}",
        )

    def test_warning_says_it_is_historical(self):
        head = self.text[:2000]
        self.assertIn("Исторический документ", head)
        self.assertIn("Не руководство", head)

    def test_warning_points_to_the_living_docs(self):
        """
        Сказать «это устарело» и не сказать «а где актуальное» — значит
        оставить читателя там же, где он был, только без документа.
        """
        head = self.text[:2500]
        for name in ("СТАТУС.md", "ARCHITECTURE.md"):
            self.assertIn(name, head, f"в шапке нет указателя на {name}")

        # Номер действующего плана здесь НЕ проверяется по букве. Он стоял
        # прибитым (`PLAN-6.md`) и покраснел в первый же следующий заход —
        # то есть тест ловил не устаревший указатель, а смену числа. Правило
        # честнее: указатель на план обязан быть, и то, на что он показывает,
        # обязано существовать.
        plans = set(re.findall(r"PLAN(?:-\d+)?\.md", head))
        self.assertTrue(plans, "в шапке нет указателя на действующий план")
        for name in sorted(plans):
            if "*" in name:
                continue
            self.assertTrue(
                (ROOT / name).exists() or (ARCHIVE / name).exists(),
                f"шапка PLAN.md показывает на {name}, которого нет ни в "
                f"корне, ни в архиве",
            )


class TestStatusFileIsUsable(unittest.TestCase):
    """
    DOC-4. `СТАТУС.md` появился потому, что ответ на «где мы сейчас»
    собирался из семи файлов `ИТОГИ-ЗАХОДА-*`, а последний из них — итог
    одной части одного захода.

    Главное свойство такого файла — он не растёт. Разросшийся статус
    превращается в восьмой файл итогов, то есть в ту же болезнь.
    """

    def setUp(self):
        self.path = ROOT / "СТАТУС.md"
        if not self.path.exists():
            self.skipTest("СТАТУС.md ещё не написан")
        self.text = self.path.read_text(encoding="utf-8")

    def test_it_stays_short(self):
        lines = len(self.text.splitlines())
        # DOC-7: порог опущен со 200 до 150. Прежний давал вдвое больше
        # места, чем нужно на «где мы сейчас», и статус успел набрать три
        # таблицы открытых пунктов, не покраснев ни разу. Порог, который
        # никогда не срабатывает, не сторож.
        self.assertLess(
            lines, 150,
            f"СТАТУС.md разросся до {lines} строк — в нём завелась "
            f"история (её место в docs/archive/) или открытые пункты "
            f"(их место в ОТКРЫТОЕ.md)",
        )

    def test_it_answers_where_we_are(self):
        for word in ("Открытое", "Что это", "проверяется"):
            self.assertIn(word, self.text,
                          f"в статусе нет раздела про «{word}»")

    def test_it_points_at_the_canon(self):
        for name in ("ARCHITECTURE.md", "README.md", "PROMPTS.md"):
            self.assertIn(name, self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
