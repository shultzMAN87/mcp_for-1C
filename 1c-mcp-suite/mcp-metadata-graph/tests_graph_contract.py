"""
Контрактный тест: запросы серверов против того, что реально пишет writer.
=========================================================================

Зачем. За два дня подряд поймано ЧЕТЫРЕ дефекта одного класса — запрос
Cypher ссылается на то, чего в графе нет:

  PERF-4  — MATCH без метки: индекс не использовался, слой 1 писался 28 мин.
  FIX-14  — MATCH по :MetadataObject там, где узел :Form: молча потеряно
            69 % рёбер :HAS_METHOD.
  FIX-16  — ребро с русским именем СОДЕРЖИТ вместо CONTAINS: три инструмента
            подсистем возвращали пустоту.
  FIX-17  — свойства n.kind и n.full_name, которых у узлов нет: поиск отдавал
            строки с пустыми видом и полным именем.

Общая черта: **Neo4j не считает это ошибкой**. Несуществующее свойство даёт
null, несуществующий тип ребра — пустой результат. Поэтому ни один дефект не
проявился отказом, все четыре нашлись случайно, при ручной сверке.

Обычные тесты такое не ловят: чтобы увидеть разницу, нужна живая база с
боевыми данными. Этот тест ловит иначе — сверяет ТЕКСТ запросов с тем, что
writer объявляет в схеме. Список разрешённых свойств не захардкожен: он
извлекается из `write_meta_nodes`, поэтому не разойдётся с кодом при
следующей правке.

Запуск:
    python tests_graph_contract.py -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

HERE = Path(__file__).parent
NEO4J_DIR = HERE.parent / "mcp-metadata-graph-neo4j"

# Файлы серверов, чьи запросы проверяем.
SERVER_FILES = ["server.py", "server_v3_tools.py", "server_v3_code_tools.py",
                "search_fulltext.py", "subsystem_scope.py"]

# Файлы со стороны индексатора. Их проверка добавлена после того, как
# PERF-4 повторился в partial_fingerprint.py: запрос без метки прошёл мимо
# теста просто потому, что тест смотрел только в каталог серверов. Дефект,
# от которого защищаемся, метку каталога не различает.
INDEXER_FILES = ["partial_fingerprint.py", "incremental.py", "graph_writer.py"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_lines(src: str) -> list[tuple[int, str]]:
    """
    Строки без комментариев, с номерами.

    Комментарии выбрасываем намеренно: в них ЦИТИРУЮТСЯ прежние, неверные
    запросы — ровно чтобы объяснить, что было не так. Проверять их значило
    бы запретить документировать исправленные дефекты.
    """
    out = []
    for num, line in enumerate(src.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        out.append((num, line))
    return out


def meta_node_properties() -> set[str]:
    """
    Какие свойства writer реально пишет узлам :MetadataObject.

    Извлекаются из SET-блока write_meta_nodes, а не из списка в этом файле:
    список рано или поздно разошёлся бы с кодом, а извлечение — нет.
    """
    src = _read(NEO4J_DIR / "graph_writer.py")
    i = src.index("def write_meta_nodes(")
    block = src[i:src.index("def ", i + 10)]
    props = set(re.findall(r"n\.(\w+)\s*=\s*r\.", block))
    props.add("id")          # ключ MERGE
    return props


def relationship_types() -> set[str]:
    """Типы рёбер, которые writer умеет писать (ключи EDGE_QUERIES)."""
    src = _read(NEO4J_DIR / "graph_writer.py")
    # Именно ОБЪЯВЛЕНИЕ, а не первое упоминание: имя встречается и в тексте
    # предупреждения FIX-15, которое стоит в файле раньше.
    i = src.index("EDGE_QUERIES: dict")
    block = src[i:src.index("\ndef ", i)]
    return set(re.findall(r'^\s{4}"(\w+)":', block, re.M))


class TestMetaNodeProperties(unittest.TestCase):
    """
    Свойства узлов метаданных, к которым обращаются серверы, должны
    существовать. FIX-17: `n.kind` и `n.full_name` не существовали, и Cypher
    молча отдавал null.
    """

    # Свойства узлов ДРУГИХ меток (:Callable, :Type, :CallSite, :Attribute…),
    # которые тоже пишутся через переменные n/m. Их наличие проверяет не этот
    # тест — здесь важно не поймать их за ложные срабатывания.
    OTHER_LABEL_PROPS = {
        "full_name", "kind", "target", "module_id", "line", "line_start",
        "line_end", "is_export", "returns", "directive", "role", "type",
        "value", "position", "callsite", "is_main", "main_kind", "count",
        "module_kind", "is_server", "is_client", "path", "name_lower",
        # :Fingerprint и служебные узлы кеша.
        "updated_at", "mode", "objects", "edges",
    }

    def setUp(self):
        self.allowed = meta_node_properties()

    def test_writer_declares_expected_properties(self):
        """Опора теста: убеждаемся, что извлечение вообще сработало."""
        for p in ("name", "synonym", "kind_eng", "kind_ru",
                  "full_name_eng", "full_name_ru"):
            self.assertIn(p, self.allowed, "не удалось извлечь схему из writer")

    def test_no_bare_kind_or_full_name_on_metadata(self):
        """
        Точечная проверка на уже случившийся дефект. `kind` и `full_name`
        принадлежат :Callable, а на :MetadataObject их нет — обращение к ним
        через `n.` в запросах по метаданным давало null.
        """
        for fname in SERVER_FILES:
            for num, line in _code_lines(_read(HERE / fname)):
                m = re.search(r"\bn\.(kind|full_name)\b(?!_)", line)
                if m and "coalesce" not in line:
                    self.fail(
                        f"{fname}:{num}: n.{m.group(1)} — такого свойства у "
                        f":MetadataObject нет, Cypher вернёт null молча (FIX-17)")

    def test_metadata_property_names_are_known(self):
        """
        Все `n.<свойство>` в запросах по метаданным — из схемы writer либо из
        явного списка свойств других меток.
        """
        known = self.allowed | self.OTHER_LABEL_PROPS
        for fname in SERVER_FILES:
            for num, line in _code_lines(_read(HERE / fname)):
                for m in re.finditer(r"\b[nm]\.(\w+)\b", line):
                    prop = m.group(1)
                    if prop in known or prop.endswith("_json"):
                        continue
                    self.fail(f"{fname}:{num}: свойство '{prop}' не пишется "
                              f"ни одним writer'ом — запрос молча вернёт null")


class TestRelationshipTypes(unittest.TestCase):
    """
    FIX-16: запрос искал ребро с русским именем, которого writer не пишет.
    Neo4j ответил пустым результатом, а не ошибкой.
    """

    def setUp(self):
        self.known = relationship_types()

    def test_writer_declares_expected_relationships(self):
        for rel in ("CONTAINS", "HAS_METHOD", "HAS_ATTRIBUTE", "PARENT_OF"):
            self.assertIn(rel, self.known, "не удалось извлечь EDGE_QUERIES")

    def test_every_queried_relationship_exists(self):
        # Рёбра, которые пишет не graph_writer, а отдельные механизмы.
        extra = {"RESOLVES_TO", "OF_TYPE", "INFERRED_TYPE", "OPERATES_ON"}
        # `[:имя]` в Python — это срез списка (`items[:preview]`), поэтому
        # требуем ведущий дефис: в Cypher связь всегда пишется как `-[:REL]`.
        pattern = r"-\[:\s*([A-Za-zА-Яа-я_]\w*)\s*(?:\]|\*)"
        for fname in SERVER_FILES:
            for num, line in _code_lines(_read(HERE / fname)):
                for m in re.finditer(pattern, line):
                    rel = m.group(1)
                    if rel in self.known or rel in extra:
                        continue
                    self.fail(f"{fname}:{num}: ребро '{rel}' не пишется "
                              f"writer'ом — запрос молча вернёт пустоту (FIX-16)")

    def test_no_cyrillic_relationship_names(self):
        """
        Отдельная проверка, потому что дефект был именно такой: рёбра
        пишутся латиницей, метки — и латиницей, и кириллицей. Легко перепутать.
        """
        for fname in SERVER_FILES:
            for num, line in _code_lines(_read(HERE / fname)):
                if re.search(r"-\[:\s*[А-Яа-я]", line):
                    self.fail(f"{fname}:{num}: тип ребра кириллицей — "
                              f"таких рёбер writer не пишет")


class TestLabelledMatches(unittest.TestCase):
    """
    PERF-4/FIX-14: матч по id без метки не использует индекс (все констрейнты
    привязаны к меткам), а матч по НЕ ТОЙ метке молча ничего не находит.
    """

    def test_no_id_match_without_label(self):
        for fname in SERVER_FILES:
            for num, line in _code_lines(_read(HERE / fname)):
                if re.search(r"\(\w+\s*\{\s*id\s*:", line):
                    self.fail(f"{fname}:{num}: матч по id без метки — индекс "
                              f"не будет использован (PERF-4)")

    def test_indexer_side_has_no_unlabelled_match(self):
        """
        То же самое со стороны индексатора.

        Здесь дефект уже случался дважды: PERF-4 в EDGE_QUERIES и повтор в
        partial_fingerprint.write_stored, где запрос `MATCH (n {id: r.id})`
        перебирал 1,3 млн узлов на каждую из 24 795 строк и отвалился по
        таймауту, не записав ничего.
        """
        for fname in INDEXER_FILES:
            path = NEO4J_DIR / fname
            if not path.exists():
                continue
            for num, line in _code_lines(_read(path)):
                # `MATCH (n {id: …})` — без метки. Шаблон и с переменной,
                # и без неё: обход через анонимный узел ловится тоже.
                if re.search(r"MATCH\s*\(\w*\s*\{\s*id\s*:", line):
                    self.fail(f"{fname}:{num}: матч по id без метки — полный "
                              f"перебор узлов на каждую строку (PERF-4)")


class TestRussianNameForms(unittest.TestCase):
    """
    FIX-18. `full_name_ru` хранится во МНОЖЕСТВЕННОМ числе вида —
    «Справочники.Контрагенты» (см. metadata_xml: full_name_ru строится из
    kind_ru_plural). А в языке запросов 1С пишут единственное:
    «Справочник.Контрагенты». Инструменты принимали только множественное,
    и обращение к объекту в самой естественной для разработчика форме
    молча возвращало «не найдено».

    Нашлось датасетом EVAL-1 — ради такого он и заводился.
    """

    def _files_matching_by_full_name(self):
        for fname in SERVER_FILES:
            src = _read(HERE / fname)
            if "full_name_ru" in src:
                yield fname, src

    def test_singular_form_accepted_everywhere(self):
        """
        Везде, где ищут по full_name_ru, должна приниматься и форма
        «<ВидЕдинственное>.<Имя>».
        """
        for fname, src in self._files_matching_by_full_name():
            for num, line in _code_lines(src):
                if "full_name_ru = $" not in line:
                    continue
                # Условие может занимать несколько строк — смотрим окно.
                window = "\n".join(src.split("\n")[num - 1:num + 4])
                self.assertIn(
                    "kind_ru + '.' + ", window,
                    f"{fname}:{num}: поиск по русскому имени не принимает "
                    f"единственное число вида (FIX-18)")


class TestFoundFlagSymmetry(unittest.TestCase):
    """
    FIX-19. `found` возвращался только при неудаче, а на успешном ответе
    отсутствовал: агент, проверяющий это поле, получал null и мог решить,
    что объекта нет, — притом что состав лежал рядом в том же ответе.

    Асимметричный признак хуже отсутствующего: он выглядит надёжным.
    Нашлось на EVAL-1, где пример вернул реквизиты и одновременно
    «не найден».
    """

    FILES = ["server_v3_tools.py", "server_v3_code_tools.py"]

    def test_negative_branch_has_positive_counterpart(self):
        for fname in self.FILES:
            src = _read(HERE / fname)
            neg = len(re.findall(r'"found":\s*False', src))
            pos = len(re.findall(r'"found":\s*[Tt]rue', src))
            self.assertGreaterEqual(
                pos, neg,
                f"{fname}: веток «не найдено» {neg}, а «найдено» лишь {pos} — "
                f"часть успешных ответов молчит о found (FIX-19)")



class TestDiagnosticToolsAreNotCached(unittest.TestCase):
    """
    OBS-1, находка приёмки 15 августа.

    `metadata_stats` был обёрнут в `@cached(ttl=600)`. При остановленной
    Neo4j пятнадцать инструментов честно отвалились по таймауту за 3,85 с
    каждый, а `metadata_stats` ответил за 15 мс — из кеша, снятого на живом
    стенде несколькими минутами раньше. Счётчики объектов, отпечатки
    индекса, `answerable: true`: полная картина здоровья графа в момент,
    когда графа нет.

    К диагностическому инструменту приходят с вопросом «жив ли он прямо
    сейчас». Кешированный ответ на такой вопрос не устарел, а перевёрнут.

    Проверка по исходнику: декоратор `@cached` не должен стоять на
    инструментах, чьё назначение — сообщать состояние.
    """

    DIAGNOSTIC_TOOLS = ("metadata_stats",)

    def test_no_cache_on_diagnostics(self):
        src = _read(HERE / "server.py")
        lines = src.splitlines()
        for name in self.DIAGNOSTIC_TOOLS:
            idx = next((i for i, l in enumerate(lines)
                        if l.startswith(f"def {name}(")), None)
            self.assertIsNotNone(idx, f"не найден инструмент {name}")
            # Декораторы идут непосредственно перед def, сплошным блоком
            # вперемешку с комментариями.
            decorators = []
            for l in reversed(lines[:idx]):
                stripped = l.strip()
                if stripped.startswith("@"):
                    decorators.append(stripped)
                elif stripped.startswith("#") or not stripped:
                    continue
                else:
                    break
            self.assertNotIn(
                "@cached", " ".join(decorators),
                f"{name} — диагностический инструмент, кеш на нём означает, "
                f"что во время аварии он отвечает картинкой здоровья",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
