"""
HYG-4. Клиентские конфиги не должны расходиться с составом стека.
==================================================================

Что нашлось
───────────
`opencode-config.json` перечислял четыре сервера из пяти: `v8std` не
появился там ни при интеграции в августе, ни позже. Файл лежит в корне
ради одного утверждения — «серверы набора не привязаны к Cursor», — и
конфиг, в котором сервера не хватает, доказывает ровно обратное.

Заметить это глазами нельзя: оба файла выглядят правильными по
отдельности, разница видна только при сличении. Это тот же класс, что
`FIX-16` (writer пишет `CONTAINS`, сервер спрашивает `СОДЕРЖИТ`) и
`LOCK-1` (пара есть в `.sh`, нет в `.ps1`) — два места описывают одно, и
расходятся молча.

Что проверяется
───────────────
Не «файлы одинаковые» — они и не должны быть одинаковыми: Cursor ходит с
хоста по `127.0.0.1:порт`, opencode живёт внутри docker-сети и ходит по
именам сервисов. Проверяется НАБОР серверов: сколько их и те ли.

Источник истины — `docker-compose.yml`: сервер существует, если у него
есть сервис с публикацией порта. Список руками не ведётся, поэтому
следующий сервер попадёт под проверку сам, а забыть дописать его в конфиг
станет нельзя.

Запуск:  python3 tests_client_configs.py
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

for _stream in __import__("sys").stdout, __import__("sys").stderr:
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

# Порты MCP-серверов набора. Пятый (8765) — чужой v8std-mcp, он тоже
# подключается агентом и обязан быть в обоих конфигах.
SERVER_PORTS = (8001, 8002, 8003, 8009, 8765)


def ports_in(path: Path) -> set[int]:
    """Все порты, встречающиеся в url-ах конфига."""
    text = path.read_text(encoding="utf-8")
    return {int(p) for p in re.findall(r"://[^\"'\s]+?:(\d{4,5})/", text)}


def compose_published_ports() -> set[int]:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    found = set()
    for m in re.finditer(r'"(?:127\.0\.0\.1:)?(\d{4,5}):\d{4,5}"', text):
        found.add(int(m.group(1)))
    return found


class TestClientConfigsCoverAllServers(unittest.TestCase):

    def setUp(self):
        self.cursor = ROOT / ".cursor" / "mcp.json"
        self.opencode = ROOT / "opencode-config.json"
        for p in (self.cursor, self.opencode):
            if not p.exists():
                self.skipTest(f"{p.name} нет — проверять нечего")

    def test_both_configs_list_the_same_servers(self):
        cursor, opencode = ports_in(self.cursor), ports_in(self.opencode)
        only_cursor = sorted(cursor - opencode)
        only_opencode = sorted(opencode - cursor)
        self.assertFalse(
            only_cursor or only_opencode,
            f"конфиги разошлись: только в Cursor {only_cursor}, "
            f"только в opencode {only_opencode}.\n"
            f"opencode-config.json лежит в корне, чтобы показать, что "
            f"серверы не привязаны к Cursor; неполный список показывает "
            f"обратное.",
        )

    def test_all_known_servers_are_present(self):
        for path in (self.cursor, self.opencode):
            missing = sorted(set(SERVER_PORTS) - ports_in(path))
            self.assertFalse(
                missing, f"{path.name}: не хватает серверов на портах {missing}")

    def test_server_ports_match_compose(self):
        """
        Список портов в этом наборе — тоже список, и он тоже может отстать.
        Сверяем его с тем, что реально публикует compose.
        """
        published = compose_published_ports()
        missing = sorted(set(SERVER_PORTS) - published)
        self.assertFalse(
            missing,
            f"SERVER_PORTS числит порты {missing}, которых compose не "
            f"публикует — либо сервер удалён, либо список отстал",
        )

    def test_opencode_config_is_valid_json(self):
        """
        В конфиг добавлен ключ `_why_this_file_exists` — пояснение, зачем
        файл вообще лежит (HYG-4). JSON комментариев не знает, поэтому
        пояснение живёт ключом, и файл обязан остаться разбираемым.
        """
        data = json.loads(self.opencode.read_text(encoding="utf-8"))
        self.assertIn("mcp", data)
        self.assertIn(
            "_why_this_file_exists", data,
            "пояснение убрали — файл снова читается как забытый хвост",
        )


class TestNoDeadServerReferences(unittest.TestCase):
    """
    HYG-4: удалённые серверы не должны упоминаться как живые.

    `mcp-code-rag` был удалён в Заходе 2, но `workspace_watcher.py`
    упоминал его девять раз и держал выключенный по умолчанию фанаут.
    Выключенный флаг к несуществующему сервису читается как недоделка, и
    каждый следующий читатель тратит время, выясняя, чего тут не хватает.
    """

    DEAD = ("mcp-code-rag", "mcp-rest-proxy", "mcp-sonarqube",
            "mcp-naparnik", "mcp-code-templates", "mcp-testing")

    # Файлы, где упоминание допустимо: история и планы описывают прошлое.
    #
    # Исключение "cleanup" убрано в Заходе 8 вместе с самими скриптами:
    # `cleanup.ps1` и `cleanup.sh` чистили стек по имени ПРЕЖНЕГО проекта
    # (`27_1c-mcp-suite-full-stack`) и соседнего `yaxunit-stack`, которых
    # здесь нет. Запущенные в этом проекте они не делали ничего и об этом
    # не сообщали — а скрипт очистки, молча не чистящий, хуже отсутствия
    # скрипта. Каждое исключение в этом списке — место, куда мёртвые имена
    # заползают обратно; чем их меньше, тем сторож честнее.
    HISTORY = ("PLAN", "ИТОГИ", "README-", "docs/archive")

    def test_live_code_does_not_reference_deleted_servers(self):
        offenders = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if any(h in rel for h in self.HISTORY):
                continue
            if path.suffix not in (".py", ".yml", ".yaml", ".json", ".example"):
                continue
            if "__pycache__" in rel or rel.startswith("v8std-data/"):
                continue
            if path.name == Path(__file__).name:
                continue  # сам список имён — не ссылка на живой код
            text = path.read_text(encoding="utf-8", errors="replace")
            for dead in self.DEAD:
                for i, line in enumerate(text.splitlines(), 1):
                    if dead not in line:
                        continue
                    # Разбор происхождения — не ссылка на живой код.
                    if any(w in line for w in ("больше нет", "удалён",
                                               "HYG-4", "по образцу")):
                        continue
                    offenders.append(f"{rel}:{i}: {dead}")
        self.assertFalse(
            offenders,
            "упоминания удалённых серверов в рабочих файлах:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
