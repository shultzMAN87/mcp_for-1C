"""
B-6. Доставка модулей в образы — одна проверка вместо пяти.
============================================================

Диагноз `AUDIT-2`
─────────────────
Приём «сверять исходники с исходниками» родился в `tests_graph_contract.py`
(`PERF-4` повторился в другом файле только потому, что тест смотрел в один
каталог) и с тех пор сработал ещё трижды — но каждый раз его приходилось
изобретать заново, в новом файле и со своим списком образов:

  tests_shortfall.py      — `COPY shortfall.py` в двух Dockerfile
  tests_refusal.py        — `COPY refusal.py` в трёх
  tests_bsl_health.py     — `COPY bsl_health.py` в одном
  tests_lockfile_pairs.py — два генератора лок-файлов

То есть проверка «модуль X импортируется сервером Y, значит COPY обязан
быть в его Dockerfile» была написана трижды, и каждый раз со своим списком
образов — сама став тем самым «списком, который надо помнить». Ровно тот
жанр, который в этом проекте расходился четырежды.

Что делает этот набор
─────────────────────
Строит соответствие «файл в образе → что он импортирует» автоматически:

  1. разбирает `COPY` во всех Dockerfile набора — что попадает в образ и
     под каким именем (`mcp-metadata-graph/server.py` приезжает как
     `/app/mcp_metadata_graph.py`, и импорты ссылаются на второе имя);
  2. разбирает импорты каждого попавшего файла через `ast`;
  3. оставляет только имена, которые являются модулями ЭТОГО проекта, —
     сторонние пакеты ставятся из requirements и к доставке отношения не
     имеют;
  4. требует, чтобы каждый такой модуль лежал в том же образе.

Списков руками здесь нет ни одного. Следующий общий модуль защищён по
факту появления: достаточно, чтобы его кто-то импортировал.

Чего набор НЕ проверяет
───────────────────────
Динамические импорты (`importlib`, `__import__` со строкой). В проекте их
нет, а появятся — эта проверка их не увидит, и это стоит помнить.

Запуск:  python3 tests_delivery.py
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SUITE = Path(__file__).resolve().parent

# Каталоги, чьи .py в образы не едут по устройству: наборы тестов и
# калибровочные скрипты живут на хосте.
TEST_PREFIXES = ("tests_", "calibrate_")


# ─── разбор Dockerfile ───────────────────────────────────────────────────


def _logical_lines(text: str):
    """Склеивает продолжения строк (`\\` в конце) — COPY бывает многострочным."""
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        yield (buffer + line).strip()
        buffer = ""
    if buffer.strip():
        yield buffer.strip()


def copies(dockerfile: Path) -> dict[str, str]:
    """
    Что этот Dockerfile кладёт в образ: {имя модуля в образе: путь в репозитории}.

    Имя в образе — это то, под которым файл импортируется. Оно не всегда
    совпадает с исходным: `mcp-metadata-graph/server.py` становится
    `mcp_metadata_graph`.
    """
    out: dict[str, str] = {}
    for line in _logical_lines(dockerfile.read_text(encoding="utf-8")):
        if not line.upper().startswith("COPY "):
            continue
        parts = [p for p in re.split(r"\s+", line)[1:] if not p.startswith("--")]
        if len(parts) < 2:
            continue
        *sources, dest = parts
        for src in sources:
            if not src.endswith(".py"):
                continue
            if dest.endswith("/") or dest.endswith("."):
                name = Path(src).stem
            else:
                name = Path(dest).stem
            out[name] = src
    return out


def dockerfiles() -> list[Path]:
    return sorted(SUITE.glob("Dockerfile*"))


# ─── разбор импортов ─────────────────────────────────────────────────────


def imported_names(path: Path) -> set[str]:
    """Модули верхнего уровня, которые импортирует файл, — все, включая ветки
    `try/except ImportError` и импорты внутри функций."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover — заметно и без теста
        raise AssertionError(f"{path}: не разбирается ({exc})") from exc

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # `from .x import y` — относительный, внутри проекта таких нет.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def project_modules() -> set[str]:
    """
    Имена, под которыми модули проекта могут импортироваться.

    Два источника: имена файлов на диске и имена, под которыми файлы
    приезжают в образы (переименование при `COPY` — обычное дело).
    """
    names = {p.stem for p in SUITE.rglob("*.py")
             if not p.name.startswith(TEST_PREFIXES)}
    for df in dockerfiles():
        names |= set(copies(df))
    return names


PROJECT_MODULES = project_modules()


def missing_copies(dockerfile: Path) -> list[str]:
    """Список претензий к одному образу. Пустой — всё на месте."""
    delivered = copies(dockerfile)
    problems = []
    for name, src in sorted(delivered.items()):
        path = SUITE / src
        if not path.exists():
            problems.append(f"{src}: COPY есть, файла нет — сборка упадёт")
            continue
        for imported in sorted(imported_names(path)):
            if imported not in PROJECT_MODULES or imported == name:
                continue
            if imported not in delivered:
                problems.append(
                    f"{src} импортирует {imported}, "
                    f"а COPY {imported}.py в {dockerfile.name} нет — "
                    f"контейнер упадёт на импорте при старте"
                )
    return problems


def assert_delivered(case: unittest.TestCase, module: str) -> None:
    """
    Хелпер для наборов конкретных модулей.

    Смысл: набор про `refusal.py` спрашивает «а меня-то довезли?», не заводя
    у себя списка образов. Список образов знает это место, и одно.
    """
    stem = Path(module).stem
    users = [df for df in dockerfiles()
             if any(imported_names(SUITE / src) & {stem}
                    for src in copies(df).values() if (SUITE / src).exists())]
    case.assertTrue(users, f"{module}: его никто не импортирует — проверять нечего")
    for df in users:
        case.assertIn(stem, copies(df),
                      f"{df.name}: модуль {module} импортируется, но не копируется")


# ─── сами проверки ───────────────────────────────────────────────────────


class TestDelivery(unittest.TestCase):

    def test_dockerfiles_found(self):
        """Пустой список Dockerfile означал бы, что набор проверяет воздух."""
        self.assertGreaterEqual(len(dockerfiles()), 3)

    def test_every_import_is_delivered(self):
        """
        Главная проверка. Забытый `COPY` — отдельный жанр в этом проекте:
        трижды за четыре захода, и каждый раз это стоило подъёма стека и
        чтения логов, потому что сервер падает на импорте при старте.
        """
        problems = []
        for df in dockerfiles():
            problems.extend(f"{df.name}: {p}" for p in missing_copies(df))
        self.assertEqual(problems, [], "\n  ".join([""] + problems))

    def test_copied_files_exist(self):
        for df in dockerfiles():
            for name, src in sorted(copies(df).items()):
                self.assertTrue((SUITE / src).exists(),
                                f"{df.name}: COPY {src} — такого файла нет")

    def test_shared_modules_are_covered(self):
        """
        Общие модули корня набора — те, ради которых всё затевалось.
        Проверка не на конкретный список, а на сам факт: если модуль в корне
        кем-то импортируется, он обязан ехать ко всем, кто его импортирует.
        """
        roots = [p for p in SUITE.glob("*.py")
                 if not p.name.startswith(TEST_PREFIXES)]
        self.assertTrue(roots)
        for path in roots:
            with self.subTest(module=path.name):
                stem = path.stem
                for df in dockerfiles():
                    delivered = copies(df)
                    if stem in delivered:
                        continue
                    for src in delivered.values():
                        f = SUITE / src
                        if f.exists() and stem in imported_names(f):
                            self.fail(f"{df.name}: {src} импортирует {stem}, "
                                      f"а COPY {stem}.py нет")


class TestParser(unittest.TestCase):
    """
    Проверка самой проверки. Тест доставки, который ничего не ловит, хуже
    отсутствующего: он даёт ложное спокойствие ровно там, где раньше был
    честный страх забыть строку.
    """

    def test_rename_on_copy_is_understood(self):
        found = copies(SUITE / "Dockerfile.python")
        self.assertEqual(found.get("mcp_metadata_graph"),
                         "mcp-metadata-graph/server.py")

    def test_non_python_is_ignored(self):
        self.assertNotIn("requirements", copies(SUITE / "Dockerfile.python"))

    def test_imports_inside_try_are_seen(self):
        """
        `refusal` импортируется в `try/except ImportError` с запасным путём
        через sys.path. Запасной путь работает на хосте и не работает в
        образе — значит, импорт обязателен и его надо видеть.
        """
        names = imported_names(SUITE / "mcp-bsl-checker" / "server.py")
        self.assertIn("refusal", names)
        self.assertIn("bsl_health", names)

    def test_detects_a_planted_gap(self):
        """Убираем строку COPY в копии Dockerfile — проверка обязана покраснеть."""
        import tempfile

        src = (SUITE / "Dockerfile.bsl").read_text(encoding="utf-8")
        broken = "\n".join(ln for ln in src.splitlines()
                           if "refusal.py" not in ln)
        self.assertNotEqual(src, broken, "строка COPY refusal.py не найдена")
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "Dockerfile.bsl"
            fake.write_text(broken, encoding="utf-8")
            # copies() читает файл, пути COPY остаются относительными SUITE —
            # именно это и нужно: подделан только Dockerfile, не исходники.
            self.assertNotIn("refusal", copies(fake))
            problems = missing_copies(fake)
            self.assertTrue(any("refusal" in p for p in problems),
                            f"пропажа не замечена: {problems}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
