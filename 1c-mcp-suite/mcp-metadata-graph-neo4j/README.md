# Индексер метаданных 1С → Neo4j (v3, XML)

Сервис `metadata-indexer` в стеке. Парсит XML-выгрузку конфигурации в `/data/1c-src`,
строит двухслойный граф и сохраняет в Neo4j через HTTP REST.

## Что это даёт (задача 4.6.1)

До v3 граф состоял только из узлов `:MetadataObject` и одного типа рёбер
`:Подсистема -[:СОДЕРЖИТ]-> :Объект`. На полной Котировке это ~69 узлов / 137 рёбер,
из которых **нельзя** ответить на вопросы вроде «какие документы ссылаются на
справочник X». Это закрывало <5% реальных запросов агента.

v3 строит:

- **Узлы:** `:MetadataObject` + `:Attribute` + `:TabularSection` + `:Form` + `:EnumValue` + `:Type`
- **Рёбра:** `HAS_ATTRIBUTE`, `HAS_TABULAR_SECTION`, `HAS_FORM`, `HAS_VALUE`, `OF_TYPE`,
  `RESOLVES_TO`, `CONTAINS`, `PARENT_OF`, `OWNED_BY`, `BASED_ON`, `REGISTERS`

На частичной выгрузке Котировок (25 директорий метаданных): **415 узлов / 588 рёбер**.
На полной — ожидаются тысячи узлов и десятки тысяч рёбер.

## Архитектура слоя

```
workspace/                      Neo4j
  Configuration.xml              (n:MetadataObject:Catalog {id, name, ...})
  Catalogs/X.xml          ─┐     (n)-[:HAS_ATTRIBUTE {role:'attribute'}]->(a:Attribute)
  Catalogs/X/Forms/Y.xml   │     (a)-[:OF_TYPE]->(t:Type {kind, target})
  Documents/Z.xml         ─┤     (t)-[:RESOLVES_TO]->(other:MetadataObject)
  Enums/E.xml             ─┤     (n)-[:HAS_TABULAR_SECTION]->(:TabularSection)
  InformationRegisters/...  │     (n)-[:HAS_FORM {is_main}]->(:Form)
  Subsystems/S.xml        ─┘     (s:Subsystem)-[:CONTAINS]->(n)
                                 (s)-[:PARENT_OF]->(s_child:Subsystem)
                                 (c:Catalog)-[:OWNED_BY]->(owner:MetadataObject)
                                 (d:Document)-[:BASED_ON]->(src:MetadataObject)
                                 (j:DocumentJournal)-[:REGISTERS]->(d:Document)
```

Атрибут — отдельный узел. Это позволяет писать запрос «какой реквизит-связка между
X и Y», находя путь `HAS_ATTRIBUTE → OF_TYPE → RESOLVES_TO`. В прежней схеме реквизиты
хранились внутри `:MetadataObject` как `attributes_json` — для запросов это плохо.

Канонический идентификатор объекта — **`Kind.Name` на английском** (`Catalog.АукАукционы`).
Это согласуется с тем, как пишутся ссылки в `cfg:CatalogRef.X` и подсистемах
(`<xr:Item xsi:type="xr:MDObjectRef">Catalog.X</xr:Item>`). Старый русский формат
(`Справочники.X`) сохранён как `full_name_ru` для обратной совместимости.

## Файлы

```
mcp-metadata-graph-neo4j/
├── indexer.py            v3.1 — точка входа сервиса (двухфазный pipeline)
├── metadata_xml.py       XML-парсер (чистый, без Neo4j) — слой 1
├── graph_writer.py       UNWIND-батч запись в Neo4j (слой 1 + слой 2)
├── bsl_parser.py         BSL-парсер (чистый, без Neo4j) — слой 2 [4.6.2]
├── bsl_resolver.py       резолвер call-site'ов — слой 2 [4.6.2]
├── tests_metadata_xml.py юнит-тесты XML-парсера (40)
├── tests_bsl_parser.py   юнит-тесты BSL-парсера (58) [4.6.2]
├── tests_bsl_resolver.py юнит-тесты резолвера (33) [4.6.2]
├── tests_graph_writer.py интеграционные тесты writer'а на живом Neo4j (28)
└── README.md             этот файл

mcp-metadata-graph/
├── server.py                  старые tools + регистрация v3 и v3-code tools
├── server_v3_tools.py         tools слоя 1 (metadata_attribute_type, find_link_path, ...)
└── server_v3_code_tools.py    tools слоя 2 (code_callers, code_call_path, ...) [4.6.2]
```


## Setup с нуля

```bash
# 1. Положить XML-выгрузку конфигурации в ./data/1c-src/ (на хосте)
#    Должны быть Configuration.xml + Catalogs/, Documents/, Enums/, ...

# 2. (Первый раз) дождаться, пока поднимется Neo4j
docker compose up -d neo4j
docker compose logs -f neo4j  # ждём "Started"

# 3. Запустить индексер. Он сам ждёт Neo4j до 120с, потом индексирует
docker compose up metadata-indexer

# 4. Проверить
curl -s -u neo4j:password1c http://localhost:7474/db/neo4j/tx/commit \
     -H "Content-Type: application/json" \
     -d '{"statements":[{"statement":"MATCH (n:MetadataObject) RETURN count(n)"}]}'
```

Лог индексера на штатном прогоне выглядит так:

```
[INFO] Источник:        /data/1c-src
[INFO] Neo4j:           http://neo4j:7474
[INFO] FORCE_REINDEX:   False
[INFO] Ожидание Neo4j…
[INFO]   ✓ доступен
[INFO] Считаем fingerprint workspace…
[INFO]   ✓ a3b8...c91e… (за 0.42 с)
[INFO]   fingerprint отсутствует — первая индексация
[INFO] Парсим XML…
[INFO]   ✓ объектов: 69 (за 0.18 с)
[INFO] Собираем граф…
[INFO]   ✓ узлов 69 + 182 + 16 + 58 + 61 + 29; рёбер 588 (за 0.05 с)
[INFO] Очищаем прежний слой метаданных в Neo4j…
[INFO]   ✓ удалено: 0
[INFO] Пишем в Neo4j (батчи UNWIND)…
[INFO]   ✓ записано за 1.34 с
[INFO]   узлы: {'MetadataObject': 69, 'Attribute': 182, ...}
[INFO]   рёбра: {'HAS_ATTRIBUTE': 182, 'OF_TYPE': 182, ...}
[INFO]   fingerprint сохранён
[INFO] ✓ Готово!
```

## Переиндексация

Индексер **идемпотентен**: при перезапуске считает fingerprint всех XML и сравнивает
со значением, записанным в Neo4j (нода `:Fingerprint {kind: 'metadata_xml'}`).
Если совпадает — выходит за <1 секунды.

### Когда нужно принудительно переиндексировать

```bash
# Вариант A: через env (только этот прогон)
docker compose run -e METADATA_FORCE_REINDEX=true metadata-indexer

# Вариант B: ручная очистка (полная)
curl -X POST http://localhost:7474/db/neo4j/tx/commit -u neo4j:password1c \
     -H "Content-Type: application/json" \
     -d '{"statements":[{"statement":"MATCH (n) DETACH DELETE n"}]}'
docker compose restart metadata-indexer
```

### Когда **не** нужно

- Изменился только BSL — индексер не парсит BSL (это задача 4.6.2).
- Изменился `ОтчётПоКонфигурации.txt` — индексер v3 не читает его (это был источник v2).

## Среды и пути

| Env                       | Default                | Назначение |
|---------------------------|------------------------|------------|
| `METADATA_SRC_DIR`        | `/data/1c-src`         | корень XML-выгрузки в контейнере |
| `NEO4J_URL`               | `http://neo4j:7474`    | HTTP-REST эндпоинт |
| `NEO4J_USER` / `NEO4J_PASS` | `neo4j` / `password1c` | креды |
| `METADATA_FORCE_REINDEX`  | `false`                | пропустить fingerprint-проверку |
| `METADATA_CONFIG_NAME`    | `Конфигурация`         | имя для узла `:Configuration` |
| `LOG_LEVEL`               | `INFO`                 | `DEBUG` для подробного дампа |

## Новые MCP-tools (что добавилось к существующим)

В `mcp-metadata-graph/server_v3_tools.py`. Регистрируются автоматически
через import в конце `server.py`. Старые tools (`metadata_search`,
`metadata_object_details`, `metadata_list_objects`, `metadata_subsystems`,
`metadata_references_to/from`, `metadata_cypher`) — без изменений.

| Tool | Параметры | Что возвращает |
|---|---|---|
| `metadata_v3_stats` | — | nodes/edges по новым меткам + top-10 «горячих» типов и объектов |
| `metadata_attribute_type` | object, attribute | тип реквизита с резолвом; ищет и в ТЧ |
| `metadata_referrers` | object, limit, offset | кто ссылается через реквизиты (включая ТЧ) |
| `metadata_find_link_path` | from, to, max_depth | shortestPath через `HAS_ATTRIBUTE/OF_TYPE/RESOLVES_TO` |
| `metadata_object_attributes` | object, role?, include_tabular | реквизиты в виде узлов, с фильтром по роли |
| `metadata_subsystem_tree` | root?, max_depth | дерево подсистем через `PARENT_OF` |
| `metadata_dead` | kind?, limit, offset | объекты, не входящие ни в одну подсистему |

## Smoke-тест после деплоя

```bash
python3 scripts/smoke_metadata_v3.py --local
```

Проверяет 6 семантических кейсов на реальном графе (после индексации Котировок).

```
======================================================================
Smoke v3 metadata graph — 6/6 passed
======================================================================
✅ metadata_v3_stats — расширенная схема ушла от v2
   Attr=182, Type=29, Form=58, EnumValue=61
✅ metadata_attribute_type — Catalog.АукАукционы.ВидАукциона → CatalogRef
   типы: [('CatalogRef', 'Catalog.АукВидыАукционов')]
✅ metadata_referrers — кто ссылается на АукВидыАукционов
   найдено 4 ссылающихся реквизитов
✅ metadata_find_link_path — путь между двумя справочниками
   путей: 1; первый длиной 3
✅ metadata_object_attributes — регистр имеет Dimension и Resource
   роли: ['dimension', 'resource'], всего 4 реквизитов
✅ metadata_subsystem_tree — корневые подсистемы видны
   найдено 8 элементов поддерева
```

Exit-code 0 при `6/6 passed` — пригодно для CI.

## Совместимость и миграция с v2

| Аспект | v2 (ОтчётПоКонфигурации.txt) | v3 (XML) |
|---|---|---|
| Источник | один большой .txt | дерево XML в `/data/1c-src` |
| Канонический ключ | `Справочники.X` (рус, plural) | `Catalog.X` (англ, singular) + `full_name_ru` для совместимости |
| Реквизиты | в `attributes_json` свойстве объекта | отдельные узлы `:Attribute` |
| Типы | резолв через regex и набор префиксов | через узел `:Type`, MERGE по `(kind, target)` |
| Composite types | теряются | сохраняются полностью (несколько `OF_TYPE` от одного `:Attribute`) |
| Подсистемы | имя `:Подсистема` (рус) | метка `:Subsystem` (англ) + `:MetadataObject`, `kind_eng='Subsystem'` |
| Идемпотентность | «граф уже существует — выйти» | fingerprint workspace |
| Форматы выгрузки | требует ручной генерации DESIGNER | работает прямо на XML, выгружаемых DESIGNER при сохранении в файлы |

Старые MCP-tools (`metadata_search`, `metadata_object_details`) написаны так, что
читают и старую, и новую схему: `metadata_search` ищет по `full_name`, который
в v3 = `full_name_eng`, а `metadata_object_details` использует `attributes_json`,
который v3-writer всё ещё заполняет (см. `build_compat_attrs` в `graph_writer.py`).
Это позволяет переключаться без поломки uplevel-агента.

## Troubleshooting

### «Граф уже существует» в логах

Это v2-поведение. v3-индексер вместо этого пишет «fingerprint совпал — данные актуальны».
Если вы видите старое сообщение — значит сервис ещё работает на v2-коде. Проверьте,
что `Dockerfile.python` копирует `metadata_xml.py` и `graph_writer.py` в `/app`
(см. патч `Dockerfile.python.patch`).

### `metadata_v3_stats` отвечает «Attribute узлов: 0»

Индексер v3 не отработал, но соседний v2 успел заполнить `:MetadataObject`-узлы.
Запустите:
```bash
docker compose run -e METADATA_FORCE_REINDEX=true metadata-indexer
```

### Неразрешённые ссылки в логе индексера

```
[WARNING] Неразрешённых ссылок: 17
[WARNING]   RESOLVES_TO→Document.НеЗнаюКто: 3
```

Это значит: реквизит ссылается на объект `Document.НеЗнаюКто`, которого нет в выгрузке.
Причины:
1. Выгрузка частичная — отсутствует часть документов/справочников. Обычное явление
   для подмножества конфы.
2. Опечатка в XML (редко).
3. Тип ссылается на встроенный объект подсистемы 1С (не нашей конфы) — это норма.

Если вас не устраивает количество — увеличьте охват выгрузки или примите как факт.

### `mcp-metadata-graph` после `docker compose up -d --force-recreate` отвечает 503 на v3 tools

Та же причина, что описана в `PLAN.md` для `mcp-sonarqube` (5.1): MCP не успевает
обработать `initialize`-handshake до того, как opencode шлёт `tools/call`.
Лечится `docker restart opencode-dev`.

## Что НЕ входит в v3 (задачи 4.6.2+)

- BSL-парсер для `:Procedure`/`:Function`/`:Parameter`/`:CALLS` — **сделано в 4.6.2**, см. ниже.
- Type inference по dataflow в теле процедур (inter-procedural) — это 4.6.4.
- Watcher для инкрементального обновления при правке XML — это 4.6.5.
  Сейчас индексер запускается заново через `docker compose run` (либо рестартом сервиса).

---

## Слой 2 (задача 4.6.2): call graph

После закрытия 4.6.2 в графе появляется второй слой — структура **поведения**
конфигурации: кто кого зовёт, какие справочники/документы/регистры читаются и
пишутся.

### Граф

```
:MetadataObject (existing)
  ├── HAS_METHOD ──→ :Callable:Procedure ─── HAS_PARAM ──→ :Parameter
  │                  или :Callable:Function
  │                       │
  │                       ├── CALL_SITE ──→ :CallSite ─── RESOLVES_TO_CALLEE ─→ :Callable
  │                       │                  (resolved=bool, reason=str)
  │                       │
  │                       ├── CALLS ──→ :Callable
  │                       │    (прямое ребро для path-traversal,
  │                       │     создаётся когда CallSite разрешён)
  │                       │
  │                       └── OPERATES_ON {via, access} ──→ :MetadataObject

:MetadataObject:Module {module_role: "ObjectModule"|"ManagerModule"|"Form"}
  (для CommonModule отдельный узел не создаётся — он совпадает с :CommonModule из слоя 1)

:Fingerprint {kind: 'bsl_source'}    — отдельный fingerprint для BSL
```

### Идентификаторы

| Тип | id-шаблон | Пример |
|---|---|---|
| `:Callable` | `<KindEng>.<ModuleName>.<MethodName>` | `CommonModule.АукОбщийКлиент.Факториал` |
| `:Callable` (форма) | `<KindEng>.<Object>.Form.<FormName>.<Method>` | `Catalog.АукАукционы.Form.ФормаЭлемента.ПриСозданииНаСервере` |
| `:Parameter` | `<callable.id>.Param.<name>` | `CommonModule.АукОбщийКлиент.Факториал.Param.пЧисло` |
| `:CallSite` | `<caller.id>:<line>:<col>` | `CommonModule.X.Y:42:8` |
| `:MetadataObject:Module` (Form) | `<KindEng>.<Object>.Form.<FormName>` (тот же id, что у :Form из слоя 1 — узел слит) | `Catalog.АукАукционы.Form.ФормаЭлемента` |
| `:MetadataObject:Module` (Object/Manager) | `<KindEng>.<Object>.<Role>` | `Catalog.АукАукционы.ObjectModule` |

### Цифры на Котировках после 4.6.2

| Метрика | Значение |
|---|---|
| `:Module` (Object/Manager/Form, без CommonModule) | 90 |
| `:Callable` всего | **1 545** (957 процедур + 588 функций) |
| `:Parameter` | 2 358 |
| `:CallSite` | ~4 750 (built-in отсеяны) |
| `:CALLS` | ~2 140 (уникальные пары) |
| `:OPERATES_ON` | ~320 (Catalog/Document/Enum/InformationRegister) |
| Покрытие резолва | **~51%** (resolved / total CallSite) |

Остальные ~49% — это методы объектов и коллекций (`пСтк.Вставить()`,
`Запрос.Выполнить()`, `Выборка.Следующий()`), которые не разрешимы статически
без inter-procedural inference (задача 4.6.4).

### MCP-tools (8 шт, после 4.6.3 регистрации)

| Tool | Назначение |
|---|---|
| `code_callers(full_name, depth, limit)` | Кто (транзитивно) зовёт процедуру |
| `code_callees(full_name, depth, limit)` | Кого (транзитивно) зовёт процедура |
| `code_call_path(from, to, max_depth)` | Кратчайший путь вызовов между двумя процедурами |
| `code_procedures_operating_on(metadata_full_name, via?)` | Процедуры, работающие с метаобъектом |
| `code_dead_procedures(module_id?, exclude_handlers, include_exports)` | Процедуры без входящих :CALLS |
| `code_method_signature(full_name)` | Параметры процедуры и метаданные о ней |
| `code_unresolved_callsites(module_id?, reason?, limit)` | Аудит покрытия резолва |
| `code_v3_stats()` | Статистика слоя 2 + топ-callee/caller |

### Запуск

Архитектура индексера двухфазная. При запуске `docker compose run metadata-indexer`:

1. **Фаза 1 (XML)** — то же что было в 4.6.1: парсит `*.xml`, строит layer-1 граф.
2. **Фаза 2 (BSL)** — после фазы 1 (или при изменении только BSL-кода):
   - читает индекс `:CommonModule` и `:MetadataObject` из Neo4j;
   - парсит все `*.bsl` (`bsl_parser.py`);
   - резолвит call-site'ы (`bsl_resolver.py`);
   - очищает слой кода через `clear_code_layer()`;
   - пишет узлы и рёбра через `write_code_graph()`.

Идемпотентность через два отдельных fingerprint'а:
`:Fingerprint {kind: 'metadata_xml'}` и `:Fingerprint {kind: 'bsl_source'}`.
Если меняется только BSL — пересобирается только слой 2. Если меняется XML —
переиндексируются оба (т.к. `clear_metadata_layer` сносит и `:Form`-узлы, к которым
крепятся методы форм).

```powershell
# Принудительная переиндексация обеих фаз
docker compose run --rm -e METADATA_FORCE_REINDEX=true metadata-indexer

# Пропустить фазу 2 (например, для R&D или быстрой проверки слоя 1)
docker compose run --rm -e METADATA_SKIP_BSL=true metadata-indexer

# DEBUG-логи фазы 2 (показывает причины skip для каждого callsite)
docker compose run --rm -e METADATA_BSL_LOG_LEVEL=DEBUG metadata-indexer
```

### Примеры Cypher

```cypher
// Сколько процедур типа Procedure vs Function?
MATCH (c:Callable) RETURN c.kind, count(c)

// Покрытие резолва
MATCH (cs:CallSite) RETURN cs.resolved, count(cs)

// Кто зовёт Факториал? (один уровень)
MATCH (caller:Callable)-[:CALLS]->(t:Callable {full_name: 'CommonModule.АукОбщийКлиент.Факториал'})
RETURN caller.full_name

// Транзитивно — до глубины 5
MATCH (caller:Callable)-[:CALLS*1..5]->(t:Callable {full_name: 'CommonModule.АукОбщийКлиент.Факториал'})
RETURN DISTINCT caller.full_name

// Кратчайший путь от формы к серверной функции
MATCH p = shortestPath((a:Callable {name: 'ПередЗаписьюНаСервере'})-[:CALLS*..5]->(b:Callable {name: 'НормализоватьСтрокуФормулы'}))
RETURN [n IN nodes(p) | n.full_name]

// Все процедуры, работающие с АукАукционы
MATCH (c:Callable)-[r:OPERATES_ON]->(:MetadataObject {id: 'Catalog.АукАукционы'})
RETURN c.full_name, r.via, r.access

// Мёртвый код (без входящих :CALLS), исключая стандартные обработчики формы
MATCH (c:Callable) WHERE NOT EXISTS { MATCH ()-[:CALLS]->(c) }
  AND NOT c.name IN ['ПриСозданииНаСервере', 'ПриОткрытии', 'ПередЗаписью', ...]
RETURN c.full_name, c.kind, c.is_export
```

### Ограничения слоя 2

1. **~49% CallSite остаются `resolved=false`**. Это методы локальных переменных, чей
   тип нельзя вывести без знания типа аргументов вызывающей процедуры (inter-procedural
   inference, задача 4.6.4). Пример: `пСткПараметры.Вставить("ключ", 1)`.

2. **Запросы в строках** не парсятся (`Запрос.Текст = "ВЫБРАТЬ … ИЗ Справочник.X"`).
   `:OPERATES_ON` собирается только из прямых обращений к коллекциям метаданных
   и из `ПредопределенноеЗначение(...)`.

3. **Подписки на события** и `&Вместо`-перехваты не моделируются как `:CALLS`.
   Это задача 4.4 в большом плане.

4. **Динамические вызовы** (`Выполнить(...)`, `Вычислить(...)`) сохраняются как
   `:CallSite resolved=false reason='dynamic'`, без `:CALLS`-ребра. Статически
   нерезолвимо.

5. **`:INFERRED_TYPE` на параметрах** в этом релизе не создаётся. Узел и edge query
   в схеме есть (задел на 4.6.4).

6. **Built-in функции** (`НСтр`, `СтрШаблон`, `Новый`, и ~150 других) НЕ создают
   `:CallSite`-узлов вовсе. Это снижает мусор в графе на ~35%. Список —
   `BUILTIN_FUNCS` в `bsl_resolver.py`.

7. **Объектные методы под видом cross-module** (`пСтк.Вставить(...)`). Парсер
   синтаксически выдаёт их как cross-module match, резолвер помечает
   `reason='unknown_module'`. Это правильное поведение — без typing они
   неотличимы от настоящих module-вызовов.

