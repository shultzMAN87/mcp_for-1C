# `PERF-6` — полнотекстовый поиск

Вторая задача Блока B (`PLAN-4.md` §3). Включает и отдельный пункт плана
«проверить: узлы модулей в результатах поиска».

```powershell
docker compose build metadata-indexer mcp-metadata-graph mcp-query-builder workspace-watcher
docker compose up -d --force-recreate mcp-metadata-graph
```

Полноценная переиндексация **не нужна**, но индексы создаются в
`ensure_schema`, то есть при запуске индексера. Быстрый способ их создать,
не трогая граф, — в конце этого файла.

| Файл | Что |
|---|---|
| `1c-mcp-suite/mcp-metadata-graph/search_fulltext.py` | новый |
| `1c-mcp-suite/mcp-metadata-graph/server.py` | `metadata_search` |
| `1c-mcp-suite/mcp-metadata-graph/tests_search_fulltext.py` | новый, 23 теста |
| `1c-mcp-suite/mcp-metadata-graph-neo4j/graph_writer.py` | создание индексов |
| `1c-mcp-suite/Dockerfile.python` | `COPY search_fulltext.py` |

**Про `Dockerfile.python`** — без строки `COPY` сервер падает на импорте при
старте. Четвёртый раз одна и та же грабля: `graph_state.py`,
`query_parser.py`, `progress_log.py`, теперь `search_fulltext.py`.

---

## Что было

`metadata_search` искал через `toLower(n.name) CONTAINS toLower($q)`.

Дело не в скорости — на 16 тысячах узлов полный перебор терпим. Дело в том,
что **`CONTAINS` не ранжирует**. Подстрока либо есть, либо нет, а сортировка
шла по имени. На слово «заказ» приходили сотни совпадений в порядке
алфавита, и «ЗаказКлиента» оказывался где-то посреди
«ВводОстатковПоЗаказам».

## Что стало

Полнотекстовый индекс Neo4j по `name`, `synonym`, `full_name_ru`, выдача по
`score DESC`. Решения внутри, каждое со своей причиной:

**Префиксный поиск для каждого слова** (`контраг*`). Полнотекстовый индекс
совпадает по целым токенам, поэтому без префикса «контраг» не находило бы
«Контрагенты» — а искать по началу слова это самый частый способ.

**Точное совпадение весит больше** (`Контрагенты^2`), опечаточное — меньше
(`~1^0.5`), и только для слов от четырёх букв: на трёх буквах нечёткий
поиск даёт шум, а не пользу.

**Слова соединяются через OR, а не AND.** «заказ клиента» должно находить и
«ЗаказКлиента», и «ЗаказПоставщику», отдав первому больший score. AND отсёк
бы половину полезного.

**Пользовательская строка экранируется.** Она приходит как текст, а не как
выражение Lucene: без экранирования «Контрагенты (ЕГРЮЛ)» или
«счёт:фактура» уронили бы поиск синтаксической ошибкой на пустом месте.

## Запасной путь

`CONTAINS` остаётся и включается сам, когда индекса нет: граф мог быть
собран старым индексером, а у пользователя может не оказаться нужной
процедуры. В ответ добавлено поле `search_engine`: `"fulltext"` или
`"contains"` — видно, какой путь сработал.

**Но глотается только «нет индекса».** Если Neo4j лёг, отказал в доступе
или запрос синтаксически неверен — ошибка поднимается как была. Иначе
поиск тихо деградировал бы и возвращал результат похуже, притворяясь, что
всё в порядке. Отдельный тест
`test_does_not_swallow_other_errors` это фиксирует.

## Узлы модулей в поиске

Пункт плана «проверить». Ваш запрос вернул `0, 0`, но это **не ответ**:
кириллица в аргументах не доезжает до `cypher-shell` через PowerShell —
той же природы артефакт, что превратил лог в `╤А╤С╨▒╤А╨░`. Проверьте
по латинице:

```powershell
@'
MATCH (o:MetadataObject) WHERE toLower(o.name) CONTAINS 'module'
RETURN count(*) AS vsego, sum(CASE WHEN o:Module THEN 1 ELSE 0 END) AS moduley;
'@ | docker exec -i neo4j cypher-shell -u neo4j -p $pw
```

Независимо от результата фильтр `NOT n:Module` добавлен в оба пути поиска.
Узлы модулей объекта и менеджера несут метку `:MetadataObject` (их 5 843),
но объектами метаданных в смысле поиска не являются: их `name` — это
«ObjectModule» и «ManagerModule». В выдаче они дают шум, а для кода есть
отдельные `code_*`-инструменты.

Чтобы кириллица в `cypher-shell` работала, один раз на сессию:

```powershell
$OutputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
```

## Создать индексы, не переиндексируя граф

```powershell
@'
CREATE FULLTEXT INDEX meta_fulltext IF NOT EXISTS
FOR (n:MetadataObject) ON EACH [n.name, n.synonym, n.full_name_ru];
CREATE FULLTEXT INDEX callable_fulltext IF NOT EXISTS
FOR (n:Callable) ON EACH [n.name, n.full_name];
'@ | docker exec -i neo4j cypher-shell -u neo4j -p $pw
```

Проверить, что построились (`state` должен стать `ONLINE`):

```powershell
'SHOW INDEXES YIELD name, type, state WHERE type = "FULLTEXT" RETURN name, state;' | docker exec -i neo4j cypher-shell -u neo4j -p $pw
```

Затем в Cursor спросите `metadata_search` что-нибудь широкое — например
«заказ» — и посмотрите на поле `search_engine` и на порядок выдачи. Если
`search_engine: "contains"` — индекс не подхватился, и это стоит разобрать.

## Тесты

`tests_search_fulltext.py` — 23 теста, офлайн. Всего 537.

Логика вынесена в отдельный модуль намеренно: `server.py` тянет
`mcp.server.fastmcp` и офлайн не импортируется, а подготовку запроса и
распознавание ошибки можно проверить без Neo4j и без MCP.
