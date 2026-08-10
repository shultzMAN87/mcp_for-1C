# План работы по проекту 27_1c-mcp-suite-full-stack

## Проект

AI-ассистент для разработки на 1С (платформа 8.3.27) на базе MCP-серверов, Qdrant, Neo4j, SonarQube. Доступ через `opencode`, который ходит в 9 MCP-серверов напрямую через нативный Tool Use API.

*До апреля 2026 в стеке был ещё `mcp-orchestrator` — обёртка из 5 субагентов на OpenRouter. Удалён в задаче 1.4 после бенчмарка, показавшего overhead 24× по медиане против прямых вызовов. См. `docs/historical/orchestrator_benchmark.py`.*

---

## Статус на 16.05.2026

**Закрыто: 20 задач** (4.6 закрыта в этой сессии — двухслойный граф метаданных + код в Neo4j, инкрементальный апдейт через MCP-tools, 29 tool'ов на `mcp-metadata-graph`).
**Осталось: 12 задач** (7 функциональных + 5 в новом Wave 6 «Agent decoupling»).

**Главный результат сессии:** задача 4.6 закрыта целиком за один сеанс — все шесть подзадач (4.6.1–4.6.6). Граф схемы данных (Reference attributes, типы связей) и call graph + type inference оба построены и доступны агенту через MCP. 242 OK + 6 env-skipped юнит-тестов на хосте, smoke 4.6.5 на живом стеке 8/8, smoke 4.6.6 (list_tools) 7/7. Полный отчёт — в `HANDOFF_4_6.md` (и предшествующий по 4.6.5 — в `HANDOFF_4_6_5.md`).

При фактическом исполнении выяснилось, что подзадача 4.6.6 (новые MCP-tools) была реализована в ходе предыдущих подзадач — что-то всплывало при ручной проверке слоёв и сразу обрастало tool'ами. Сверх плановых 7 tool'ов сделано ещё 8 диагностических. Это объясняет, почему 4.6 закрылась быстрее верхней оценки из плана (5–7 дней).

---

## ✅ Закрытые задачи

### Из ранних сессий (до детального плана): 0.1, 1.1, 1.2, 1.3, 2.4, 3.1, 4.8, 4.9, 4.10

### 2.3 — File Watcher

Автоматическая переиндексация `workspace/` (BSL) при изменении файлов.

- Контейнер `workspace-watcher` на `Dockerfile.python`.
- `1c-mcp-suite/workspace_watcher.py` (~430 строк): `watchdog.Observer` + `PollingObserver` (env `WATCHER_USE_POLLING` — нужно для Docker Desktop / Windows), `DebouncedQueue` с дедупликацией, `AsyncLoop` (долгоживущий event loop в отдельном треде через `run_coroutine_threadsafe`).
- Новые tools в `mcp-code-rag/server.py`: `code_reindex_file(filepath)` и `code_remove_file(filepath)` — инкрементальный Qdrant-апдейт по `payload.file`.
- Env: `WATCHER_ENABLED`, `WATCHER_DEBOUNCE_SEC`, `WATCHER_USE_POLLING`, `METADATA_WATCH_ENABLED`.
- Реиндексация файла за ~0.4 сек.

### 3.2 — Shared secret на MCP SSE

Защита 11 MCP-серверов от анонимного доступа в docker-network.

- `1c-mcp-suite/mcp_auth.py`: ASGI-middleware `SharedSecretMiddleware` проверяет `Authorization: Bearer <secret>` или `X-MCP-Secret`. Пропускает `/`, `/health`, `OPTIONS *`. Отдаёт 401 с `WWW-Authenticate: Bearer realm="mcp"`. `wrap_sse_app()` — no-op при пустом env (fail-open для dev). `build_client_headers()` — клиентский хелпер.
- Middleware применена: `start.py` (8 серверов), `mcp-bsl-checker/server.py`, `mcp-sonarqube/server.py`, `mcp-rest-proxy/server.py`. *(Изначально применялась и в `orchestrator/server.py`, но оркестратор удалён в 1.4.)*
- Клиентские патчи: `workspace_watcher`, `code_reindex_trigger` передают Bearer в `sse_client`. *(Также были применены к оркестратору до его удаления.)*
- `mcp-config.json` + `opencode-config.json`: `"headers": {"Authorization": "Bearer {env:MCP_SHARED_SECRET}"}` всем 11 серверам.
- `docker-compose.yml`: `MCP_SHARED_SECRET` в environment 14 сервисов.
- `.env.example` с инструкцией `openssl rand -hex 32`.
- Проверено curl'ом: без заголовка → 401, с правильным → 200 text/event-stream. В логах всех 11 контейнеров `[mcp-auth] <name>: auth ENABLED (secret length=64)`.
- ⚠ Известный дефект: `scripts/smoke_auth.py` содержит баг в детекции 401 (ложно-FAIL). Требует правки перед использованием в CI.

### 3.3 — Аудит-лог в mcp-rest-proxy

JSONL-лог всех вызовов, ходящих в живую 1С.

- `1c-mcp-suite/audit_log.py` (~25 KB) с `record_http_call()`.
- Интеграция в `mcp-rest-proxy/server.py` на всех выходах (odata_*, http_service_call, test_connection).
- Опциональный `AUDIT_INCLUDE_BODY=true` для отладки (по умолчанию off — защита от персданных).
- Ротация через `RotatingFileHandler`.

### 3.4 — Evaluation harness

Объективные метрики качества для поиска по справке платформы и других tool'ов.

- Папка `evals/`: `schema.md`, `datasets/platform_help.jsonl` (10 примеров смешанного ground truth — hard `name_in_top_k`/`non_empty` + soft `any_hit_kind`/`full_name_contains`), `runner/` (`predicates.py`, `mcp_client.py` на MCP SDK, `metrics.py`, `report.py`, `run_eval.py`, `tests.py`), `reports/`.
- Runner — самодостаточный контейнер `python:3.12-slim + mcp[cli]`, поднимается только по профилю evals: `docker compose --profile evals run --rm eval-runner`.
- Метрики: hard/soft pass-rate, Recall@{1,5,10}, MRR, p95 латентности, распределение `search_type`.
- Отчёты: JSON + markdown с таблицей, summary и Failures (топ-3 реальных хитов) — диффаются между прогонами.
- Архитектурные решения: (1) MCP SDK (`sse_client` + `ClientSession`), не ручной httpx. (2) Одна SSE-сессия на прогон — platform-help не переинициализирует dense/BM25 между примерами. (3) Профиль evals вместо постоянного сервиса.
- Хост-обёртка `scripts/eval.py` поверх `docker compose run`, режим `--local` для отладки против `localhost:8003`. Exit-code runner'а: 0 только при 100% hard + 0 transport errors — пригодно для CI.
- Тесты `evals/runner/tests.py`: predicates (20 кейсов + ранги), metrics на 5-примерной матрице, парсер датасета, mrr-info, run_one + generate_reports через FakeSession.

### 4.7 — Поддержка нового формата `.hbk` для платформы 8.3.22+

На 8.3.22+ `.hbk` перестал быть обычным ZIP: 16-байтовый заголовок V8 + префикс с ASCII-hex оглавлением (~1700 байт) + поток Local File Headers со стандартной deflate-компрессией, без центрального каталога. Старый `indexer.py` давал 7 пустышек в коллекции.

- Папка `1c-mcp-suite/mcp-platform-help-embeddings/`:
  - `hbk_reader.py` — ручной парсер LFH без central directory.
  - `hbk_parser.py` — структурный HTML-парсер классов `V8SH_*` (имя RU/EN, parent, синтаксис, параметры с типами, возвращаемое значение, описание, доступность, пример, since_version, deprecated). Балансный парсер скобок для сложных имён.
  - `hbk_chunker.py` — чанки: `card` / `params` / `syntax` / `example` / `description`. В `card` добавлены `Имена:` и `Слова:` (CamelCase-разложение) для BM25.
  - `hbk_indexer.py` — hybrid (dense e5 + sparse BM25 + RRF), fingerprint через точку `id=0`, `HBK_INDEX_LANG=ru|en|both`.
- Переписан `mcp-platform-help/server.py` (703 → 1084 строк), 7 MCP-tools: `platform_help_search` (+ фильтр `kind`), `platform_help_lookup` (новый, без векторов), `platform_help_details`, `platform_help_kinds` (новый), `platform_help_stats`, `its_search`, `search_all`. Удалена `platform_help_categories`.
- Результаты: 40 976 точек, 25 509 страниц, 7 199 методов, 13 660 свойств, 693 события, 3 222 типа объектов, 725 таблиц. Индексация ~2 часа на CPU.
- Стейдж-смоук-тест `stage3_smoke.py` — 9/9 зелёные.
  *(Удалён в апреле 2026 — устарел после задачи 3.5; функциональность перекрывается общим `scripts/smoke_mcp.py`.)*

### 3.5 — Smoke-тесты MCP через SSE

End-to-end проверка, что все 10 MCP-серверов реально отвечают tool'ами через SSE + MCP SDK (а не просто слушают порт). Раньше `smoke_auth.py` проверял только HTTP-уровень auth, реальную доступность tools никто не валидировал.

- `scripts/smoke_mcp.py` (508 строк): `MCPSession` (sse_client + ClientSession.initialize) + один безопасный read-only tool на каждый сервер. Валидация: не isError, непустой ответ, для JSON-tools — `json.loads` + семантические проверки. Пример семантики: для `platform_help_stats` проверяется `collection_kind ∈ {hybrid, legacy_dense}` и `total_points > 0`.
- Подбор tool'ов (10 проб): `metadata_stats`, `bsl_check_code` (минимальный BSL), `platform_help_stats`, `naparnik_check_connection` (без реального HTTP в code.1c.ai), `templates_count`, `query_validate` (`ВЫБРАТЬ 1`), `test_runner_health`, `code_search` (limit=1), `connection_info` (читает конфиг, НЕ ходит в 1С), `sonar_list_projects`. Все идемпотентны.
- `scripts/_smoke_common.py` — общая инфраструктура: `MCP_SERVERS`, `resolve_secret()`, `verify_against_json()` (санити-чек соответствия с `mcp-config.json`).
- Compose-профиль `smoke`: `docker compose --profile smoke run --rm smoke-runner` — переиспользует образ `mcp-eval-runner`. Внутри контейнера ходит по docker-DNS (`MCP_USE_DOCKER_NAMES=1`), с хоста — по `localhost`.
- Exit-code 0 только при всех pass — пригодно для CI.
- Попутный фикс бага в `smoke_auth.py` (3.2): `_probe` переписан на `client.stream("GET", ...)` вместо `.get()`. В исходной версии `httpx` иногда давал `ReadTimeout` ДО чтения `status_code=401` — false-FAIL 33/33. Теперь все три пробы стабильны.

### 1.4 — Решение по оркестратору (закрыто удалением)

`mcp-orchestrator` удалён из стека после объективного бенчмарка. Изначально в плане было «прогнать eval-датасеты на разных LLM-провайдерах», но при изучении кода оказалось, что eval harness меряет только retrieval-tools, LLM в нём не участвует. Поэтому фокус сместился на главный вопрос: **нужен ли вообще оркестратор**.

- `docs/historical/orchestrator_benchmark.py` — бенчмарк Direct-vs-Agent: 5 парных задач, каждая выполняется (a) прямым вызовом MCP-tool и (b) через `ask_agent`. Меряется latency, размер ответа, успешность.
- Результат на живом стеке (24.04.2026):

| Задача | Direct | Agent | Overhead |
|---|---|---|---|
| metadata_stats | 138 ms | 3.36 s | 24.4× |
| platform_help_search | 253 ms | 5.09 s | 20.2× |
| query_validate | 110 ms | 3.61 s | 32.9× |
| bsl_check_code | 6.74 s | 8.86 s | 1.3× |
| templates_search | 89 ms | 5.50 s | 61.6× |

  **Медиана 24×, среднее 28×.** Overhead почти константный (~3–5 секунд на вызов, независимо от tool под ним) — это стоимость одной-двух LLM-итераций.

- Корни проблемы (видны в коде `orchestrator/server.py`): regex-парсинг `<tool_call>` вместо нативного Tool Use API (TODO на строке 479), неэффективный prompt caching внутри одной задачи. Главное — **opencode уже умеет всё то же самое нативно через Tool Use API**, оркестратор просто дублирует его поверх и в 20–30 раз медленнее.
- Решение: **удалить полностью**. Альтернатива «переписать на нативный Tool Use» отброшена — это 1–2 дня работы ради сохранения ненужного слоя.

**Удалено:**
- Папка `1c-mcp-suite/orchestrator/` (~900 строк кода, 5 субагентов).
- Сервис `mcp-orchestrator` из `docker-compose.yml` (порт 8012). Стек: 23 → 22 сервиса.
- Секции `1c-orchestrator` из `mcp-config.json`, `mcp-config-localhost.json`, `opencode-config.json`.
- Probe для оркестратора в `_smoke_common.py` и `smoke_mcp.py`. Список MCP_SERVERS: 11 → 10.
- `COPY orchestrator/server.py` из `Dockerfile.python`.
- Раздел «Orchestrator» и `orchestrated-task`/`architect-design` skills из `workspace/AGENTS.md` и `README.md`.
- Env-переменные `LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL_STRONG`, `LLM_MODEL_FAST`, `LLM_PROMPT_CACHE`, `LLM_PROMPT_CACHE_TTL` из `.env.example` (использовались только оркестратором).

**Перенесено в `docs/historical/`:**
- `orchestrator_benchmark.py` — артефакт принятия решения, остаётся в репо как доказательство что замер реально проводился.

### 5.1 — End-to-end YAxUnit на проде (Котировки)

Сетап агентного цикла «код → bsl_check → SonarQube → YAxUnit» на реальной конфе пользователя (не demo-конфе). Изначальный HANDOFF от прошлой сессии указывал базовые шаги, но в нескольких местах был неточен — реальные значения параметров расширения выводились эмпирически по ошибкам пайплайна.

**Что сделано:**

- **Создан скелет расширения `КотировкиТесты`** в `workspace/tests-extension/` (5 файлов, ~9 KB). Привязка к основной конфе Котировок через `<ExtendedConfigurationObject>fbe37d37-...</ExtendedConfigurationObject>` в `Languages/Русский.xml` (UUID языка из `workspace/Languages/Русский.xml`). UUID объектов расширения сгенерированы свежими (uuid4) — никаких чужих UUID из demo-tests.
- **Эмпирически выяснены три критичных параметра, которые HANDOFF указывал неверно:**
  - `<ConfigurationExtensionCompatibilityMode>` должен быть `Version8_3_23`, **не** `Version8_3_24`. На платформе 8.3.24.1819 реальный DESIGNER при выгрузке расширения ставит именно `8_3_23` — так же делает и пользовательская выгрузка из живой 1С. Со значением `8_3_24` пайплайн падает на step 7 с exit=101.
  - Блок `<InternalInfo>` с `<xr:ContainedObject>` записями **обязателен** в корневом `Configuration.xml` расширения (HANDOFF говорил «не копировать» — это применимо к интерактивному DESIGNER, но headless `LoadConfigFromFiles` без него падает с `Internal data ... is not available`). `<xr:ClassId>` копируются из demo (это идентификаторы классов платформы), `<xr:ObjectId>` генерируются свежими.
  - У `Languages/Русский.xml` обязателен пустой тег `<InternalInfo/>` сразу после `<Language uuid="...">`.
- **Кодировка/формат:** UTF-8 с BOM + CRLF — как в реальной выгрузке DESIGNER. На 8.3.24 без BOM в headless-режиме `LoadConfigFromFiles` ведёт себя нестабильно.
- **Доработан `workspace/.opencode/skills/test-generate/SKILL.md`** — добавлен раздел «Г. Workflow для расширения КотировкиТесты» (~280 строк): схема каталогов, соглашение об именах модулей `Тест_<Подсистема>_<ИмяОбъекта>` (с таблицей префиксов под подсистемы Котировок: Аукционы/УБД/Эдо/Логирование/Общие), 6-шаговый цикл добавления нового тестового модуля (создать .xml + .bsl, зарегистрировать в `<ChildObjects>` И в `ConfigDumpInfo.xml`, прогон в server-mode async), таблица «Типичные галлюцинации агента» с 5 разобранными кейсами из реальной сессии, секция «Антипаттерны BSL» с примерами «❌ так падает / ✅ так работает».
- **Прогнаны два прода-теста:**
  - `Тест_Общие_Пример.ПайплайнЖив_ДваПлюсДваРавно4` — стартовая заглушка, проверяет, что пайплайн доходит от `LoadConfigFromFiles` до выполнения BSL. Прошёл `passed` за 67.7 сек (server-mode, полная Котировка).
  - `Тест_Аукционы_АукОбщийКлиент.Тест_Факториал0` — реальный тест на функцию `Факториал`, добавленную агентом в `АукОбщийКлиент.Module.bsl`. Прошёл `passed` за 75.4 сек.

**Уроки про работу агента, зафиксированные в SKILL:**

- Агент при ошибках любит сочинять «инфраструктурные» причины («extension visibility», «проблема совместимости», «MCP сервер недоступен»), хотя реальные причины обычно в коде на диске. SKILL теперь содержит таблицу «Типичные галлюцинации агента» с разбором конкретных эпизодов.
- **`mode="server"` — дефолт для проекта, не `mode="file"`.** Агент после первого таймаута попытался обобщить «file стабильнее работает» — это противоречит фактам сессии (server-mode прошёл и до, и после). SKILL фиксирует: server для всех тестов, кроме чисто-арифметических unit-тестов; переключение на file без явной причины — антипаттерн.
- **`wait=False` (async) — обязательно для server-mode на Котировках.** MCP-клиент OpenCode имеет таймаут 60 сек, а полный прогон занимает 70+ сек. С `wait=True` гарантирован `MCP error -32001 "Request timed out"`, причём раннер при этом продолжает работу — но агент видит таймаут как «ошибку» и идёт строить ложные теории.
- **YAxUnit API на платформе с `<ScriptVariant>Russian</ScriptVariant>`** — только русские имена методов. `.ДобавитьТест(...)`, `.ОжидаетЧто().Равно()`. Английских синонимов (`.AddTest`, `.AssertEqual`) **нет**, ошибка выглядит как `Object method not found`. Зафиксировано в SKILL с антипримером.

**Известные хвосты (отдельные задачи, не блокируют основной цикл):**

- ~~В SonarQube при сканировании функции `Факториал` обнаружено **0 issues**.~~ **Закрыто задачей 5.2 (см. ниже).**
- Раннер YAxUnit чистит каталоги `/tmp/yaxunit-runs/<run_id>/` и `/tmp/pipeline_logs/<run_id>/` после прогонов — лог не доступен задним числом. Диагностику нужно делать **сразу после неудачного прогона**, пока артефакты живы. Вариант с retention TTL `>1 час` — кандидат на улучшение раннера, но не приоритет.

### 5.2 — SonarQube: BSL-плагин + Quality Gate + Quality Profile + CE-task wait

Закрытие хвоста из 5.1: на любом BSL-коде SonarQube возвращал 0 issues, анализировал `.bsl` как plain text. В исходной формулировке задача звучала «полдня и забыли» — на практике под капотом обнаружилось **пять** разных слоёв проблем, ни один из которых не был очевиден до начала. Раздел оформлен подробно намеренно: следующая сессия с похожей задачей должна не угадывать заново.

**Корневой состав проблем (по слоям, в порядке обнаружения):**

1. **Плагин не установлен.** В `sonar-plugins/` лежал только `README.md`. Старая инструкция в README предлагала ручное скачивание `sonar-bsl-plugin-community-*.jar`, но артефакт давно переименован в `sonar-communitybsl-plugin-*.jar` (та же репа `1c-syntax/sonar-bsl-plugin-community`, ребрендинг внутри организации).
2. **Quality Gate бесполезен как валидатор.** Дефолтный `Sonar way` завязан на `new_coverage<80` / `new_violations` — у нас одноразовых проектов агента без CI «новый код» = весь код, и условия либо вечно зелёные, либо вечно красные.
3. **Quality Profile для BSL не активирован у проектов.** Это **отдельная** сущность от Quality Gate, и я её изначально упустил. Без него SonarQube видит файл, понимает что плагин его поддерживает, но **не запускает ни одного правила** → `0 issues`. Засветилось только при детальной диагностике через `_run_scanner` и логи sonar-scanner'а.
4. **`sonar-scanner` работает асинхронно.** Когда scanner печатает `ANALYSIS SUCCESSFUL` — это означает «отчёт залит на сервер», а не «issues в БД». Сервер обрабатывает отчёт фоновой Compute Engine task'ой, которая может занять 5–60 секунд. `sonar_scan_code` сразу после возврата сканера дёргал `issues/search` и получал пусто. **Это была главная причина** «0 issues» в smoke даже после фиксов 1–3.
5. **Системный HTTP-прокси перехватывает localhost.** Та же проблема, что описана в `Известных проблемах` про httpx (v2rayN/Clash слушает 127.0.0.1:10809) — но и `urllib` тоже подхватывает `HTTP_PROXY` через trust-by-default. Без обхода — `TimeoutError` на любых вызовах к локальному SonarQube.

**Что сделано:**

- **`scripts/install_sonar_bsl_plugin.py`** (~280 строк, stdlib-only) — установщик плагина. Распознаёт уже установленный jar по обоим именам артефакта (`sonar-communitybsl-plugin-*.jar` — текущее, `sonar-bsl-plugin-community-*.jar` — историческое); если ничего нет — качает latest с GitHub Releases. Опции `--version X.Y.Z`, `--force`, `--check` (exit 0/1 для CI). Атомарное скачивание (`.part` → `rename`), sanity-check минимального размера jar (`<100KB` → подозрение на HTML-заглушку прокси), `User-Agent` обязателен — без него GitHub API отдаёт 403.

- **`scripts/provision_sonar_quality_gate.py`** (~520 строк) — создаёт Quality Gate `1C BSL` через Web API. RULESET: 7 условий (`bugs=0`, `vulnerabilities=0`, `code_smells≤10`, `duplicated_lines_density≤5%`, рейтинги reliability/security/sqale ≥ A/A/B). На любых порогах эти условия дают realistic-сигнал: на лёгком сниппете `code_smells=6` → QG=OK; на функции с десятками smells или хоть одним багом → QG=ERROR.
  Идемпотентный (повторный запуск → reconciliation: `added/updated/removed/kept`), defensive по метрикам (если сервер не знает — SKIP с warning, не падение). Опции:
  - `--set-default` — сделать дефолтным gate'ом сервера. **Quality Gate в Community Edition не умеет привязки по префиксу проекта**, поэтому либо default, либо ручная привязка через `qualitygates/select`. Поскольку на нашем стенде BSL-проектов больше никаких не предполагается — `--set-default` рекомендуется.
  - `--purge-foreign` — удалить условия с метриками вне TARGET_CONDITIONS. Нужно потому что **SonarQube 9+ при `qualitygates/create` копирует условия из built-in `Sonar way`** (`new_coverage<80`, `new_violations>0`, `new_security_hotspots_reviewed<100`, `new_duplicated_lines_density>3`) — этого поведения я не ожидал, оно не описано в большинстве туториалов. На свежем create включается автоматически; на уже существующем gate'е — только по явному флагу, чтобы не убить пользовательские кастомизации.
  - `--update`, `--delete`, `--dry-run`.
  Бонус-фикс: `SonarClient` для localhost-URL'ов использует `urllib.request.build_opener(ProxyHandler({}))` — обходит системный прокси.

- **`scripts/provision_sonar_quality_profile.py`** (~250 строк) — **третья сущность, которая не была в первоначальной формулировке задачи**. Делает default profile для языка `bsl`. На нашем стенде с community-плагином 1.18 это `BSL Language Server rules` (167 активных правил). Без этого пункта Quality Gate бесполезен: правила не запускаются → нет issues → нет данных для условий. Опции: `--set-default`, `--report` (показать список профилей и количество активных правил), `--dry-run`. Идемпотентный.

- **`scripts/smoke_sonar_bsl.py`** (~330 строк) — end-to-end smoke. Шлёт через `mcp-sonarqube` маркерный BSL-сниппет (функция с вложенными `Если`, пустым блоком, неиспользуемой переменной, magic numbers — заведомо триггерит несколько правил) и проверяет два hard-условия: `issues_total > 0` и среди rule-id есть BSL-правила. Префиксы поддерживаются: `bsl-language-server:` (фактический префикс community-плагина 1.18+, верифицирован эмпирически), `bsl-language:`, `bsl:`, `communitybsl:` — последние три как hedge на случай переименования. Засветилось эмпирически: первый прогон smoke упал на «issues есть, но среди них НЕТ BSL-правил», потому что в моём списке префиксов не было `bsl-language-server:` — реальный rule-id `bsl-language-server:UnusedLocalVariable`. Префикс добавлен. Использует `MCPSession` из `evals/runner/mcp_client.py`. **Проактивно выставляет `NO_PROXY=localhost,127.0.0.1,::1` в env ДО импорта `mcp_client`** — иначе httpx подхватывает системный прокси и даёт 503.

- **`1c-mcp-suite/mcp-sonarqube/server.py`** — три ключевых изменения:
  - `_run_scanner()` после успешного analysis читает `ceTaskId` из `.scannerwork/report-task.txt` (этот файл sonar-scanner всегда пишет после `INFO ANALYSIS SUCCESSFUL`).
  - Новая функция `_wait_for_ce_task(task_id, timeout_sec)` — поллинг `/api/ce/task?id=X` до `SUCCESS|FAILED|CANCELED`, дефолтный poll-interval 0.7 сек. Возвращает `{status, duration_ms, error_message}`.
  - `sonar_scan_code` ждёт CE-task с timeout 60с (для одного файла этого хватает с большим запасом — реальные значения 5–15 сек). `sonar_scan_directory` — с timeout 180с. Если CE-task провалился — возвращаем отдельный `status: ce_task_failed` с `error_message`, не маскируя под successful scan. В JSON-ответе появилось поле `ce_task: {id, status, duration_ms, timed_out}` — для дебага видно, действительно ли дождались.

- **`scripts/check_prereqs.py`** — обновлён `check_sonar_plugin()` (новое сообщение про эффект «0 issues» + отсылка к установщику) и добавлен `check_sonar_plugin_loaded()` — ходит в `/api/plugins/installed` живого SonarQube и проверяет, что плагин не просто лежит как jar, но и **подгружен**. Soft-skip: если SonarQube недоступен или нет токена — WARN, не FAIL. Тоже с фиксом обхода прокси.

- **`sonar-plugins/README.md`** — переписан: секции «установщик», «Quality Gate», «Quality Profile», «smoke-тест», обновлено имя артефакта плагина.

**Подводные камни и архитектурные решения (зафиксировать для будущих сессий):**

- **Quality Gate ≠ Quality Profile.** Это **две независимые сущности**, и обе нужны. Gate — что считать падением (метрики и пороги). Profile — какие правила запускать вообще. Дефолтный профиль для языка ставится отдельным API `qualityprofiles/set_default`. Пропуск profile'а → 0 issues независимо от gate'а. Это самая частая ошибка в туториалах по setup'у Sonar — обычно описывают только Gate.
- **Sonar API асинхронен.** Без `_wait_for_ce_task` любой setup-скрипт, который запускает scan и сразу читает результат — будет работать «через раз», в зависимости от того, успел ли CE-task. Это касается не только нашего `sonar_scan_code` — это касается любого внешнего интегратора. Pattern с `report-task.txt` + `/api/ce/task` — стандартный, документирован Sonar, но в большинстве wrapper'ов опускается.
- **SonarQube 9+ копирует built-in conditions при create QG.** Это не описано как явное поведение в API-доках. На свежесозданном gate'е оказывается 4 унаследованных `new_*` условия. Если их не вычистить — наши целевые условия станут неэффективными (вечно красные `new_coverage<80` маскируют наш `code_smells>10`).
- **Имена правил BSL-плагина — `bsl-language-server:*`** (на 1.18.0). Не `bsl:`, не `communitybsl:`, не `1c-bsl:`. Если плагин когда-либо переименует — smoke начнёт падать с понятным сообщением «issues есть, но не BSL-правила», а не молча.
- **Метрика `cyclomatic_complexity` per-function — это правило плагина (issue), а не QG-метрика.** На уровне QG в Community Edition агрегатной `complexity` достаточно; per-function complexity улавливается через `code_smells`. Если в будущем плагин экспонирует свою QG-метрику — добавим в RULESET.
- **Системный HTTP-прокси на Windows.** v2rayN/Clash/Shadowsocks слушают 127.0.0.1:10809 и подхватываются httpx через `trust_env=True` И urllib через переменные окружения. Любой скрипт стека, который ходит на localhost, должен либо явно ставить `NO_PROXY=localhost,127.0.0.1,::1` (для httpx-based), либо использовать `ProxyHandler({})` (для urllib-based). Это уже было известно для smoke (см. ниже в `Известных проблемах`), но мне пришлось наступить ещё раз в provision-скриптах.

**Empirical findings, которые стоят упоминания:**

- Время CE-task для одного BSL-файла на 1c-syntax 1.18 + SonarQube Community Edition — **~14–24 секунды** (BSL Core Sensor сам по себе ~8с, плюс инициализация кэша bsl-language-server). На больших проектах — десятки секунд. Дефолтный 60-сек timeout в `sonar_scan_code` — комфортный.
- На пустом стенде после `docker compose up -d` SonarQube готов к работе примерно через **60–90 секунд** после начала старта контейнера. До этого Web API отдаёт `503` или `SonarQube is starting up`.
- Профиль `BSL Language Server rules` версии 1.18 содержит **167 активных правил** из коробки. Их можно отключать/настраивать, но дефолт уже разумный.
- Параметр `sonar.bsl.file.suffixes=.bsl,.os` в `sonar-project.properties` **не нужен** на 1.18 — плагин регистрирует расширения сам через `Declared patterns of language 1C (BSL) → sonar.lang.patterns.bsl: **/*.bsl,**/*.os`. Оставлен в `_run_scanner()` для совместимости с возможными старыми версиями плагина, но реально игнорируется.

**Итоговый сценарий применения (с нуля, на чистом стенде):**

```bash
# 1. Плагин
python3 scripts/install_sonar_bsl_plugin.py
docker compose restart sonarqube
# Ждём ~60 сек

# 2. Quality Profile (default для bsl)
python3 scripts/provision_sonar_quality_profile.py --set-default

# 3. Quality Gate (default для сервера)
python3 scripts/provision_sonar_quality_gate.py --set-default

# 4. Smoke
python3 scripts/smoke_sonar_bsl.py --verbose
# ✅ PASS: issues_total=6, BSL-правил уникальных: 5
```

После этого `sonar_scan_code` возвращает осмысленные issues с rule_id вида `bsl-language-server:UnusedLocalVariable`, `qualityGate` — реальный `OK` или `ERROR` по нашим порогам, `ce_task` показывает `status: SUCCESS, duration_ms: ~10000`. **Задача 4.2 (Sonar fixer) разблокирована.**

### 4.6 — Двухслойный граф метаданных + код в Neo4j

Заменён архитектурно-недостаточный indexer (69 узлов / 137 рёбер для Котировок, только `:Подсистема -[:СОДЕРЖИТ]-> :Объект`) на двухслойный граф со всеми сопутствующими MCP-tool'ами. Закрыта в одной сессии (16.05.2026) — фактически быстрее верхней оценки 5–7 дней, потому что 4.6.6 органически случилась в ходе 4.6.1–4.6.4.

- **4.6.1 — Парсер XML метаданных (слой 1):** `metadata_xml.py`. Узлы `:MetadataObject`, `:Attribute`, `:TabularSection`, `:Form`, `:Type`, `:Subsystem`, `:EnumValue`. Рёбра `:HAS_ATTRIBUTE`, `:HAS_TABULAR_SECTION`, `:OF_TYPE`, `:RESOLVES_TO`, `:HAS_FORM`, `:CONTAINS`, `:HAS_VALUE`, `:OWNED_BY`. Резолв ссылочных типов через `:Type → :RESOLVES_TO → :MetadataObject`. 40 юнит-тестов.
- **4.6.2 — Парсер BSL (слой 2, декларации):** `bsl_parser.py`. Регексп-парсер с препроцессингом (вырезание комментариев и строк с сохранением длин), извлечение `:Procedure` / `:Function` с директивами `&НаКлиенте/&НаСервере/...`, флагом `is_export`, параметрами. Классификация модуля по пути. 78 юнит-тестов (76 + 2 env-skipped).
- **4.6.3 — Резолвер вызовов:** `bsl_resolver.py`. `:CALLS`-рёбра, резолвер `Модуль.Метод`, фолбэк на `CallSite` для неразрешимых, `:OPERATES_ON` для `Документы.<Имя>` / `Справочники.<Имя>` / ... 59 юнит-тестов.
- **4.6.4 — Type inference (минимум):** локальный data-flow (`Х = Документы.<Имя>.СоздатьДокумент()` → `Х: DocumentObject.<Имя>`) + inter-procedural fix-point (типы параметров через агрегацию по callsite'ам). Engine монотонный, завершается на recursive functions.
- **4.6.5 — Watcher + инкрементальный апдейт:** `incremental.py` (~600 строк) + MCP-tools `metadata_upsert_file` / `metadata_remove_file`. Watcher fan-out: при `METADATA_WATCH_ENABLED=true` .bsl идёт в оба таргета (code-rag Qdrant + metadata-graph Neo4j), ключ дедупликации очереди `(path, target)`. 55 + 16 тестов. Прогон на 115 .bsl Котировок: 111 reindexed / 0 errors, Σ callables инкремента = Σ полного reindex = 1545.
  - **Граница актуальности:** исходящие связи правленного файла корректны; входящие могут устаревать до полного reindex. Документировано в docstrings tool'ов. Это ожидаемое поведение v1.
- **4.6.6 — Новые MCP-tools:** 7 целевых + 8 диагностических. На `mcp-metadata-graph` 29 tool'ов всего.
  - Слой 1: `metadata_attribute_type`, `metadata_find_link_path`, `metadata_referrers`, `metadata_object_attributes`, `metadata_subsystem_tree`, `metadata_dead`, `metadata_v3_stats`.
  - Слой 2: `code_callers`, `code_callees`, `code_call_path`, `code_procedures_operating_on`, `code_dead_procedures`, `code_method_signature`, `code_unresolved_callsites`, `code_v3_stats`.
  - 4.6.5: `metadata_upsert_file`, `metadata_remove_file`.

Verification суммарно: **242 OK + 6 env-skipped** юнит-тестов на хосте (env-skipped — тесты против реальной выгрузки и живой Neo4j, отрабатывают только на соответствующих стендах). **Smoke 4.6.5: 8/8** (ΔCallable=0, ΔParameter=0, ΔMetadataObject=0). **Smoke 4.6.6: 7/7** (list_tools).

Известные ограничения (вынесены в 4.6.7 / другие задачи, не блокеры v1): полный reindex по правке `Configuration.xml`; каскад `Forms/*.xml → Catalogs/X.xml`; GC висящих `:Type`-узлов; каскад `remove_xml_file(CommonModule) → remove_bsl_file(...Module.bsl)`; глобальная inter-procedural сходимость только при полном reindex; парсинг запросов в строках; `&Вместо` (это уже задача 4.4). Подробности — `HANDOFF_4_6.md`, раздел «Известные ограничения».

---

## 🔜 Осталось реализовать

### P1 — важные доработки

| ID | Задача | Оценка | Ценность |
|---|---|---|---|
| 2.1 | Автовыгрузка XML через ibcmd/DESIGNER | 1 день | Снимает барьер входа для metadata-графа. |
| 2.2 | Скрейпер ИТС с авторизацией | 1–2 дня | Пополняет коллекцию (сейчас всего 14 точек). |

### P2 — новый функционал

| ID | Задача | Оценка | Ценность |
|---|---|---|---|
| 4.1 ⭐ | MCP техжурнала 1С | 5–7 дней | **Самый высокий ROI.** LLM даёт 10× ускорение там, где эксперт читает логи часами. |
| 4.2 ⭐ | Активный Sonar fixer | 3–4 дня | Автономный цикл `sonar_get_issues → naparnik_fix → bsl_check → code_reindex_file → sonar_scan_code`. Все кирпичи готовы, нужен клей. ~~Зависит от 5.2~~ — после закрытия 5.2 разблокирована. |
| 4.3 | CI/CD-бот для MR | 5–7 дней | Превращает IDE-ассистента в командный инструмент. |
| 4.4 | Анализ конфликтов расширений (`&Вместо`) | 3–4 дня | Узкая, но больная тема 1С — цепочки перехватов. |
| 4.5 | Cross-session memory | 2–3 дня | Opencode перестанет стартовать с нуля. |

**Всего осталось:** 7 задач (2×P1 + 5×P2). Нижняя граница ~20 дней, верхняя ~28 дней.

### Wave 6 — Agent decoupling (новый, P1+P2)

Развязка стека от OpenCode. Сейчас стек поднимается с расчётом на то, что агент — это OpenCode-в-контейнере (`opencode-dev` в той же docker-сети, скиллы в `workspace/.opencode/skills/`, абсолютные пути в промптах). Архитектурно ничего не мешает использовать Claude Code, Cursor, Cline, Continue — все они говорят на MCP. На практике мешает обвязка: нет готовых конфигов, пути захардкожены, скиллы в OpenCode-специфичной папке.

Это **отдельная ось** от Wave 4/5 — не функциональность, а дистрибуция и доступность того, что уже сделано. Имеет смысл начинать после 4.1 / параллельно с любыми P2.

| ID | Задача | Оценка | Приоритет |
|---|---|---|---|
| 6.1 | Конфиги MCP-клиента для Claude Code, Cursor, Cline, Continue | 0.5–1 день | **P1** — несоразмерный выигрыш в discoverability за полдня работы. |
| 6.2 | Параметризация путей через `.env` (`ONEC_XML_PATH`, `WORKSPACE_PATH`, ...) | 1–2 дня | P2 — снимает барьер «положи всё по дефолтным путям». |
| 6.3 | Обогащение `description` у MCP-tools | 1 день | P2 — бесплатная замена скиллов для клиентов без skill-механики. |
| 6.4 | Standalone-режим без контейнера агента (профиль `agent-in-container`) | 0.5 дня | P2 — снимает психологический барьер «надо ставить OpenCode». |
| 6.5 | Скиллы → нейтральный источник + генераторы под клиента | 2–3 дня | P3 — отложить до фидбэка от внешних пользователей по 6.1+6.3. |

**Всего Wave 6:** 5–7 дней без 6.5, 7–10 дней с 6.5. Сравнимо с одной крупной P2-задачей.

#### 6.1 — Конфиги MCP-клиента для популярных IDE (P1)

Сейчас в корне `opencode-config.json`, внутри `1c-mcp-suite/` — `mcp-config.json`. Оба заточены под OpenCode-загрузчик. Любой другой клиент видит стек как «непонятный набор контейнеров».

**Что сделать:**

- Создать `agent-configs/` с подпапками `opencode/` (перенести существующее), `claude-code/`, `cursor/`, `cline/`, `continue/`.
- В каждой подпапке: рабочий MCP-конфиг под формат конкретного клиента + локальный `README.md` с 5-шаговой инструкцией «как подключить».
- Транспорт — **SSE через опубликованные порты** (`http://localhost:8001/sse` и т.д.). Не stdio через `docker exec`. Stdio несовместим с архитектурой проекта: FastMCP-сервисы с большим state в памяти (загруженные fastembed-модели в `mcp-platform-help` ~1.1 ГБ, коннекшн-пул Neo4j в `mcp-metadata-graph`, BM25 IDF-кэш Qdrant), `MCP_SHARED_SECRET` middleware из 3.2 на HTTP-заголовках, аудит rest-proxy из 3.3 на длинноживущем процессе. Каждый `docker exec` — холодный старт + сломанный auth + сломанный аудит.
- Авторизация — через шаблон `${MCP_SHARED_SECRET}` в HTTP-заголовке. Все целевые клиенты (Claude Code, Cursor, Cline, Continue) умеют env-substitution в MCP-конфигах.
- В корневой README — секция «Поддерживаемые AI-клиенты» с таблицей: клиент / транспорт / куда положить конфиг / ссылка на `agent-configs/<name>/README.md`.

**Известный нюанс:** Claude Desktop исторически плохо работает с прямым SSE — для него потребуется stdio-прокси (`mcp-remote` или `supergateway`). Это **отдельный кейс**, не входит в 6.1. В 6.1 — четыре клиента, которые умеют SSE из коробки.

**Acceptance:**
- Smoke-чек на 4 клиентах (вручную, в issue со скриншотами): после подключения по конфигу из `agent-configs/<name>/` агент видит все 10 MCP-серверов и успешно дёргает `metadata_stats`, `platform_help_search`, `bsl_check_code`.
- Раздел «Поддерживаемые клиенты» в README с явным указанием, что OpenCode — референсная реализация, не единственная.

#### 6.2 — Параметризация путей через .env (P2)

Сейчас в `docker-compose.yml` пути захардкожены: `./workspace`, `./1c-config-xml`, `./its-articles`, `./platform-help-data`, `./custom-templates`, `./sonar-plugins`. Работает для случая «склонировал репо, всё положил по дефолтным путям». Не работает для случая «у меня выгрузка лежит в `~/projects/Kotirovki/src/`, и я хочу указать на неё, не копируя».

Связано с фолбэком `_norm_rel` с tail-suffix matching на `mcp-metadata-graph` (4.6.5) — фолбэк решает разъезд *внутри* контейнеров, но не разъезд *между* хостом пользователя и контейнерами.

**Что сделать:**

- В `.env.example` добавить переменные:
  ```
  ONEC_XML_PATH=./1c-config-xml          # XML-выгрузка конфигурации
  WORKSPACE_PATH=./workspace             # рабочая папка агента + тесты-расширения
  ITS_ARTICLES_PATH=./its-articles       # PDF статей ИТС
  PLATFORM_HELP_PATH=./platform-help-data # .hbk файлы синтакс-помощника
  TEMPLATES_PATH=./custom-templates
  SONAR_PLUGINS_PATH=./sonar-plugins
  ```
- В `docker-compose.yml` все mount'ы — через эти переменные с фолбэком: `${WORKSPACE_PATH:-./workspace}:/workspace:ro`.
- В `scripts/check_prereqs.py` — добавить проверку, что все пути существуют и не пусты. Сейчас он проверяет только наличие docker и валидность `.env`.

**Что НЕ делать:** не сводить всё в один путь типа `.:/workspace`. Для 1С-выгрузки это контрпродуктивно — индексаторы будут засасывать README, .git, скрипты CI, любой не-1С-мусор; watcher будет дёргаться на каждое изменение в репо. Логически разделённые пути — правильно, нужно только сделать их конфигурируемыми.

**Acceptance:**
- На свежем стенде: пользователь правит только `.env`, указывает реальные пути на хосте, `docker compose up -d --build` поднимает стек без правок compose.
- `scripts/check_prereqs.py` сообщает осмысленную ошибку при пустых/несуществующих путях, не падает в индексаторе.

#### 6.3 — Обогащение `description` у MCP-tools (P2)

На `mcp-metadata-graph` сейчас 29 tool'ов, суммарно по стеку — порядка 60–80. Любой MCP-клиент получает их `description` через `tools/list` при инициализации. Качественные описания — бесплатная замена скиллам OpenCode для клиентов без skill-механики (Cursor, Continue, частично Cline).

Сейчас описания скорее всего короткие — название + однострочник (видно по списку tool'ов в smoke 3.5).

**Что сделать:**

- Пройтись по всем зарегистрированным tool'ам, в `description` добавить:
  - что tool делает (1 строка, уже есть);
  - когда уместно вызывать (1–2 строки) — выжимка из соответствующего скилла;
  - что возвращает и в каком формате (для JSON-tools — какие ключи в ответе);
  - типичные ошибки и их интерпретацию (например, для `metadata_upsert_file` — что значит `path_outside_src_root` из watcher-логики 4.6.5);
  - **stateful-нюансы.** `test_run_path` возвращает `run_id` и требует polling через `test_run_status`. С `wait=True` гарантирован таймаут — это уже зафиксировано в скилле `test-generate`, но в `description` сейчас, скорее всего, нет. Агент без скилла не догадается.
- Идиоматический шаблон в каждой подсистеме (`mcp-metadata-graph`, `mcp-platform-help`, ...) — единый стиль.
- Особое внимание: 8 диагностических tool'ов на `mcp-metadata-graph` (сверх плановых 7 в 4.6.6) — у них description должен явно говорить «диагностика, не для прямого вызова из workflow», иначе агент без скиллов будет ими шуметь.

**Acceptance:**
- `tools/list` от любого MCP-сервера возвращает description'ы средней длиной не менее ~300 символов с осмысленным содержанием.
- Тест на «холодном» клиенте без скиллов (Cursor): агент способен решить типовой кейс из README («допиши YAxUnit-тесты для функции Факториал») без существенно худшего результата, чем OpenCode со скиллами.

#### 6.4 — Standalone-режим без контейнера агента (P2)

Сейчас типичный путь установки: `docker compose up`, потом `docker exec -it opencode-dev`. Это создаёт впечатление, что OpenCode — обязательная часть стека. Это не так — `opencode` в compose это один из 14 сервисов.

**Что сделать:**

- В основном `docker-compose.yml` повесить на сервис `opencode` профиль `agent-in-container`. Тогда:
  - `docker compose up -d` — поднимает 13 сервисов без агента, для внешних клиентов.
  - `docker compose --profile agent-in-container up -d` — все 14 сервисов, как сейчас.
- В README — два равноправных пути установки:
  - **Путь A (для новичков):** `docker compose --profile agent-in-container up -d`, всё включено, OpenCode внутри.
  - **Путь B (для пользователей Claude Code / Cursor / Cline):** `docker compose up -d`, потом конфиг из `agent-configs/<name>/`.

**Acceptance:**
- `docker compose up -d` поднимает 13 сервисов, контейнера `opencode-dev` среди них нет, остальное работает.
- `docker compose --profile agent-in-container up -d` — текущее поведение, 14 сервисов.

#### 6.5 — Скиллы → нейтральный источник + генераторы (P3, опционально)

Скиллы сейчас лежат в `workspace/.opencode/skills/` — формат OpenCode (и Claude Code, у них похожая модель). Cursor использует `.cursorrules` или `.cursor/rules/*.mdc`, Continue — `slash commands` + `prompts`, Cline — `.clinerules`.

Задача оправдана только если 6.1+6.3 покажут, что внешние клиенты подключаются, но без скиллов работают заметно хуже. До этого момента — преждевременно: поддерживать один формат всегда дешевле, чем поддерживать четыре через генератор.

**Что сделать (когда созреет):**

- Переместить скиллы в `skills/` в корне с frontmatter:
  ```markdown
  ---
  name: query-build
  description: Построение и валидация запросов 1С
  required_tools: [query_validate, metadata_search, metadata_attribute_type]
  applies_to: [query_authoring]
  ---
  ```
- Скрипты:
  - `scripts/generate_opencode_skills.py` → `workspace/.opencode/skills/`.
  - `scripts/generate_claude_md.py` → конкатенация в `CLAUDE.md` для Claude Code.
  - `scripts/generate_cursor_rules.py` → `.cursor/rules/*.mdc`.
  - `scripts/generate_cline_rules.py` → `.clinerules`.
- В `Makefile` — таргет `make sync-skills`, чтобы после правки `skills/` все варианты пересобирались одной командой.

**Зависимость:** имеет смысл делать после 6.1+6.3, когда понятно, что́ именно из скиллов реально нужно вынести, а что уже покрылось `description` у tools.

#### Что в Wave 6 **не делать**, даже если будет соблазн

- **Не переводить транспорт на stdio через `docker exec`.** Сломает архитектуру SSE-сервисов с состоянием (загруженные модели, коннекшн-пулы, метрики), сломает `MCP_SHARED_SECRET` middleware из 3.2 (она на HTTP-заголовках), сломает аудит rest-proxy из 3.3 (он завязан на длинноживущий процесс). Stdio для Claude Desktop — компромисс через прокси-обёртку, не общее решение.
- **Не монтировать корень репозитория пользователя в `/workspace`.** Сломает индексаторы (засосут не-1С-мусор), сломает watcher (будет дёргаться на любое изменение в репо), сломает абсолютные пути в скиллах. Параметризация через несколько `.env`-переменных по логическим источникам (6.2) — правильный путь.

---

## Рекомендация на следующий шаг

Wave 3 (инфраструктура) полностью закрыта: 3.1, 3.2, 3.3, 3.4, 3.5 ✅. Оркестратор удалён (1.4 ✅). Wave 5: 5.1 (E2E YAxUnit на проде) ✅, 5.2 (SonarQube BSL) ✅. Wave 4 (граф): 4.6 ✅. Осталось два P1 и пять P2.

**Приоритет:**

1. **4.1 ⭐ — MCP техжурнала (5–7 дней).** Теперь это P1 в плане де-факто. Самый высокий ROI на задачах класса «эксперт читает логи часами». 4.6 уже закрыта, граф данных и call graph доступны — 4.1 ни на что не блокирована. Парсер `*.log` техжурнала 1С, хранилище (ClickHouse или DuckDB поверх файлов), MCP-tools для query'ев («ошибки авторизации за час», «долгие транзакции», «корреляция событий A и B»).
2. **6.1 + 6.4 — Конфиги MCP-клиента + standalone-режим (1–1.5 дня).** Дёшево и кратно расширяет аудиторию. Можно делать параллельно 4.1 в любое свободное окно. После этого пользователи Claude Code / Cursor / Cline могут подключаться к стеку без OpenCode внутри.
3. **4.2 — Активный Sonar fixer (3–4 дня).** Теперь имеет полный контекст благодаря 4.6: fixer сможет смотреть на `code_callers` перед правкой, понимать blast radius. До 4.6 он бы работал «на ощупь».
4. **2.1 — Автовыгрузка XML (1 день).** Снимает последний барьер входа для metadata-графа на чужих стендах: пока выгрузка делается руками через DESIGNER. Короткая задача.
5. **6.2 — Параметризация путей (1–2 дня).** Логически парная к 2.1: 2.1 убирает шаг «руками выгрузить XML», 6.2 убирает шаг «руками положить XML по дефолтному пути». Делать после 2.1.
6. **6.3 — Обогащение `description` у tools (1 день).** Самостоятельная зона, можно делать любым свободным окном. Хорошо ложится между крупными задачами.
7. **2.2, 4.5, 4.3, 4.4, 6.5** — как было, по убыванию ценности. 6.5 в самом конце, и только если 6.1+6.3 покажут реальный спрос на nativeскиллы для не-OpenCode клиентов.

**Почему 4.1 раньше 4.2:** 4.2 — это интеграция готовых кирпичей (sonar + naparnik + bsl_check уже есть). 4.1 — новый функционал, который **сейчас не покрыт никем**. На задачах «разобраться, почему вчера в проде упало» агент пока бесполезен, и это самый частый класс срочных обращений в реале. 4.2 же даёт улучшение качества кода в режиме «не торопясь, посмотрел отчёт sonar за неделю» — ценное, но менее срочное.

**Почему 6.1 в топ-3:** полдня работы, эффект — потенциальная аудитория проекта расширяется в десятки раз. У проекта 0 stars / 0 forks на момент составления плана; OpenCode-only дистрибуция этому способствует. Claude Code, Cursor и Cline вместе покрывают подавляющее большинство пользователей AI-IDE.

**Возможные альтернативы:**

- `4.1 → 6.1 → 4.2 → 2.1` — фокус «быстрее накатить главную фичу + раскрыть аудиторию».
- `2.1 → 6.1+6.2 → 4.1 → 4.2` — сначала закрываем инфраструктурные долги (выгрузка XML + конфигурируемые пути + конфиги клиентов), потом фичи. Разумно, если регулярно накатываем стек на чужие стенды.
- `6.1 → 6.4 → 4.1 → 4.2` — «сначала открываемся миру, потом строим». Подходит, если хочется собрать ранний фидбэк от внешних пользователей до того, как вкладываться в крупные новые фичи.

---

## Архитектура задачи 4.6

> **Статус:** ✅ закрыта 16.05.2026. Раздел сохранён как справочное описание архитектуры графа — для будущих доработок (4.6.7) и для задач, которым нужно опираться на структуру графа (например, парсинга текста запросов 1С — это раздел знает, какие узлы и рёбра доступны). Фактические имена tool'ов, файлов и тестов смотри в `HANDOFF_4_6.md`.

Задача 4.6 — это **не одна** задача «переписать парсер», а **две независимые подсистемы в одном indexer'е**. Их разделение пришло из реального диалога с пользователем (05.05.2026): водораздел между «графом схемы данных» (для написания запросов) и «графом вызовов» (для type inference и рефакторинга).

### Граф 1 — Граф метаданных (схема данных)

**Что это.** Карта связей между объектами конфигурации: справочники, документы, регистры, перечисления, реквизиты, типы.

**Источник правды.** XML-файлы в `workspace/`. В каждом XML типа справочника описаны его реквизиты с типами:

```xml
<Reference uuid="...">
  <Properties><Name>АукАукционы</Name></Properties>
  <ChildObjects>
    <Attribute>
      <Properties>
        <Name>ВидАукциона</Name>
        <Type><Type>cfg:CatalogRef.АукВидыАукционов</Type></Type>
      </Properties>
    </Attribute>
  </ChildObjects>
</Reference>
```

**Текущий indexer этим не занимается** — он парсит `ОтчётПоКонфигурации.txt` (текстовый файл, выгружаемый вручную из DESIGNER), извлекает оттуда только `:Подсистема -[:СОДЕРЖИТ]-> :Объект`. Реквизиты, табчасти, ссылочные типы — **полностью теряются**.

**Что должно быть в графе после 4.6:**

- **Узлы объектов:** `Catalog`, `Document`, `InformationRegister`, `AccumulationRegister`, `Enum`, `CommonModule`, `DataProcessor`, `Report`, `Subsystem`, `ChartOfCharacteristicTypes`, `DocumentJournal`.
- **Узлы вложенных метаданных:** `Attribute`, `TabularSection`, `EnumValue`, `Form`, `Command`, `Template`.
- **Узлы типов:** `Type` с подтипами `CatalogRef`, `DocumentRef`, `EnumRef`, `Number`, `String`, `Date`, `Boolean`, `UUID`, `ValueStorage`, `CompositeType` (составные типы).
- **Рёбра:**
  - `(:Catalog)-[:HAS_ATTRIBUTE]->(:Attribute)`
  - `(:Catalog)-[:HAS_TABULAR_SECTION]->(:TabularSection)`
  - `(:TabularSection)-[:HAS_ATTRIBUTE]->(:Attribute)`
  - `(:Attribute)-[:OF_TYPE]->(:Type)` — для составных типов несколько рёбер
  - `(:Type {kind: "CatalogRef", target: "АукВидыАукционов"})-[:RESOLVES_TO]->(:Catalog {name: "АукВидыАукционов"})`
  - `(:Subsystem)-[:CONTAINS]->(:Catalog | :Document | …)`
  - `(:Catalog)-[:HAS_FORM]->(:Form)` (с признаком `isMain`)

**Что это даёт агенту:**

- **«Через какой реквизит и тип `Справочник.АукАукционы` связан со `Справочник.АукВидыАукционов`?»** — cypher по `:HAS_ATTRIBUTE → :OF_TYPE → :RESOLVES_TO`.
- **«Какие документы имеют ссылку на `Справочник.Контрагенты`?»** — обратный traversal `MATCH (d:Document)-[:HAS_ATTRIBUTE]->()-[:OF_TYPE]->(:Type {target: "Контрагенты"})`.
- **«Покажи все типы, которые могут лежать в реквизите `Контрагент` документа `Заказ`»** (составной тип) — выходящие рёбра `:OF_TYPE` к нескольким `:Type` сразу.
- **«Дай мне готовое INNER JOIN между АукАукционы и АукВидыАукционов»** — агент пишет cypher, получает имя реквизита-связки (`ВидАукциона`), генерирует BSL-запрос.

**Сложность построения — низкая.** XML-формат у 1С стандартизирован и стабилен (мы это видели в задаче 5.1 при сравнении выгрузок). Парсер пишется на `xml.etree.ElementTree` или `lxml` за один-два дня.

### Граф 2 — Граф вызовов (поток управления + типы)

**Что это.** Карта «кто кого зовёт» на уровне процедур, и через какие параметры передаются какие значения.

**Источник правды.** Тела `*.bsl`:
- `workspace/CommonModules/<имя>/Ext/Module.bsl` — общие модули.
- `workspace/Catalogs/<имя>/Ext/ObjectModule.bsl` — модули объектов справочников.
- `workspace/Catalogs/<имя>/Ext/ManagerModule.bsl` — модули менеджеров.
- `workspace/Catalogs/<имя>/Forms/<форма>/Ext/Form/Module.bsl` — модули форм.
- Аналогично для `Documents`, `DataProcessors`, `Reports`, `InformationRegisters`, и т.д.

**Что должно быть в графе:**

- **Узлы:** `Procedure`, `Function` с свойствами `full_name` (`CommonModule.АукОбщийКлиент.Факториал`), `is_export`, `directive` (`&НаКлиенте`/`&НаСервере`/`&НаСервереБезКонтекста`/`&НаКлиентеНаСервереБезКонтекста`/`&НаКлиентеНаСервере`).
- **Узлы параметров:** `Parameter` с свойствами `name`, `position`, `is_by_value` (есть ли `Знач`), `default_value`, **`inferred_type`** (выведенный из use-sites).
- **Рёбра:**
  - `(:Procedure)-[:CALLS {site_line: 47, callee_module: "АукОбщийКлиент"}]->(:Procedure)`
  - `(:Procedure)-[:HAS_PARAM]->(:Parameter)`
  - `(:Parameter)-[:INFERRED_TYPE]->(:Type)` — связь с **тем же** `:Type` из графа 1!

**Что это даёт агенту (то, что описал пользователь):**

- **Type inference по цепочке вызовов.** Есть экспортная процедура в общем модуле, в параметрах приходит `пЭлемент`. Локально не понятно, какого он типа — может, структура. Агент идёт по `[:CALLS]` обратно (кто зовёт `ОбработатьЭлемент`?), смотрит callsite (`ОбработатьЭлемент(СпрСсылка)`), резолвит тип `СпрСсылка` через окружающий контекст — например, она пришла из `СпрСсылка = Справочники.АукАукционы.НайтиПоКоду(...)`, значит её тип `CatalogRef.АукАукционы`. Записывает в `:Parameter {name: "пЭлемент"}-[:INFERRED_TYPE]->(:Type)`. Постепенно граф наполняется.
- **«Кто зовёт АукОбщийКлиент.Факториал?»** — `MATCH (caller:Procedure)-[:CALLS]->(callee:Procedure {full_name: "CommonModule.АукОбщийКлиент.Факториал"}) RETURN caller.full_name`.
- **«Покажи путь вызовов от `ОбработкаПроведения` документа `Заказ` до `ОбщегоНазначения.СтрокаВЧисло`»** — `MATCH path = shortestPath((a:Procedure {full_name: "..."})-[:CALLS*]->(b:Procedure {full_name: "..."})) RETURN path`. Используется для оценки blast radius перед рефакторингом.
- **Поиск мёртвого кода.** Процедуры/функции без входящих `:CALLS` (если не экспортные обработчики событий формы/документа). Это ровно то, что делал образец BSL-кода от пользователя для аудита конфы.
- **Контекст для написания тестов.** Перед тем как писать тест на функцию — пройтись по `:CALLS` обратно, увидеть 3-5 callsite'ов, понять, какие реальные значения туда передают, написать осмысленный тест с правдоподобными входными данными.

**Сложность построения — средняя.** BSL — простой синтаксически язык, но динамически-типизированный, и type inference — это R&D. Грубая оценка покрытия:

- **90% случаев** — прямые вызовы `Модуль.Метод(...)` или `СамМодуль.Метод(...)`. Покрываются регуляркой.
- **5%** — вызовы методов объектов с локально известным типом (`Заказ = Документы.Заказ.СоздатьДокумент(); Заказ.Записать();` — тип `Заказ` известен из присваивания). Требует простейшего дата-флоу анализа.
- **3%** — вызовы методов объектов с типом из параметров/глобалов. Требуют рекурсивного inference через граф.
- **2%** — динамические вызовы через `Выполнить()`/`Вычислить()` или передача функции как переменной. **Принципиально не разрешимы** статически. Записываем как `:Procedure-[:DYNAMIC_CALL_SITE]->()` с текстом.

Для первой версии достаточно покрыть **90%** через регулярки + простой парсер. Дата-флоу анализ — на потом, в 4.6.1 или 4.6.2.

### Регулярки для парсинга BSL — что взять из примера пользователя

В присланном пользователем BSL-коде (анализатор неиспользуемых процедур из его прошлого проекта) есть несколько отлично работающих регулярок. Адаптация для Python:

```python
# Объявления экспортных процедур/функций
RE_DECL_EXPORT = re.compile(
    r'^\s*(Процедура|Функция)\s+([А-Яа-я_][А-Яа-я0-9_]*)\s*\([^)]*\)\s*Экспорт',
    re.MULTILINE | re.IGNORECASE
)

# Объявления НЕ-экспортных процедур/функций
RE_DECL_INTERNAL = re.compile(
    r'^\s*(Процедура|Функция)\s+([А-Яа-я_][А-Яа-я0-9_]*)\s*\([^)]*\)(?!\s*Экспорт)',
    re.MULTILINE | re.IGNORECASE
)

# Межмодульный вызов: Модуль.Метод(...)
RE_CROSSMODULE_CALL = re.compile(
    r'(?<![А-Яа-я0-9_])([А-Яа-я_][А-Яа-я0-9_]*)\s*\.\s*([А-Яа-я_][А-Яа-я0-9_]*)\s*\('
)

# Вызов метода в текущем модуле (без префикса)
RE_LOCAL_CALL = re.compile(
    r'(?<![А-Яа-я0-9_.])([А-Яа-я_][А-Яа-я0-9_]*)\s*\('
)

# Директива модуля/процедуры
RE_DIRECTIVE = re.compile(
    r'^\s*&\s*(НаКлиенте|НаСервере|НаСервереБезКонтекста|'
    r'НаКлиентеНаСервереБезКонтекста|НаКлиентеНаСервере)',
    re.MULTILINE | re.IGNORECASE
)

# Обработчики событий формы из Form.xml
RE_FORM_EVENT_HANDLER = re.compile(r'(?<=<Event>).+(?=</Event>)')

# Параметры процедуры с признаком "Знач"
RE_PARAM = re.compile(
    r'(Знач\s+)?([А-Яа-я_][А-Яа-я0-9_]*)\s*(?:=\s*([^,)]+))?'
)
```

**Важные нюансы из практики анализатора:**

- **Игнорировать комментарии и строки.** Нельзя просто применять regex к raw-тексту — `// Метод()` не должен ловиться. Препроцессинг: построчно вырезать `//.*$`, заменять строковые литералы `"..."` на placeholder. Это не идеально (многострочные литералы), но 99% случаев покрывает.
- **Не путать объявление с вызовом.** В образце пользователя для этого используется отрицательный lookbehind `(?<!Функция|Процедура)`. В Python с переменной длиной lookbehind — проще проверять контекст после нахождения матча.
- **Englsh/Russian alias.** В BSL можно писать и `Procedure`/`Function`, и `Процедура`/`Функция`. Регулярка должна ловить оба (через `(Процедура|Функция|Procedure|Function)`).

### Карта соответствий имён метаданных (из образца пользователя)

```python
METADATA_KIND_MAP = {
    "Catalogs": "Справочники",
    "CommonForms": "ОбщиеФормы",
    "Documents": "Документы",
    "Enums": "Перечисления",
    "Reports": "Отчеты",
    "DataProcessors": "Обработки",
    "ChartsOfCharacteristicTypes": "ПланыВидовХарактеристик",
    "InformationRegisters": "РегистрыСведений",
    "AccountingRegisters": "РегистрыБухгалтерии",
    "AccumulationRegisters": "РегистрыНакопления",
    "DocumentJournals": "ЖурналыДокументов",
    "CommonModules": "ОбщиеМодули",
    "ExchangePlans": "ПланыОбмена",
    "ChartsOfAccounts": "ПланыСчетов",
    "ChartsOfCalculationTypes": "ПланыВидовРасчета",
    "BusinessProcesses": "БизнесПроцессы",
    "Tasks": "Задачи",
    "Constants": "Константы",
    "Sequences": "Последовательности",
    "WebServices": "WebСервисы",
    "HTTPServices": "HTTPСервисы",
    "ScheduledJobs": "РегламентныеЗадания",
}
```

### Связь между двумя графами

Графы **сосуществуют в одной Neo4j-базе** и могут быть запрошены вместе:

```cypher
// Найти все процедуры, которые работают со Справочником АукАукционы
MATCH (p:Procedure)-[:OPERATES_ON]->(c:Catalog {name: "АукАукционы"})
RETURN p.full_name, c.name

// Найти все справочники, для которых нет ни одной серверной процедуры (мёртвая бизнес-логика)
MATCH (c:Catalog)
WHERE NOT EXISTS {
    (p:Procedure {directive: "НаСервере"})-[:OPERATES_ON]->(c)
}
RETURN c.name
```

`:OPERATES_ON` строится в графе 2 при парсинге BSL: если в теле процедуры найден паттерн `Документы.<Имя>` или `Справочники.<Имя>` или `РегистрыСведений.<Имя>` — пишется ребро от процедуры к соответствующему объекту графа 1.

### Декомпозиция работ по 4.6

| Этап | Длительность | Содержание |
|---|---|---|
| 4.6.1 — Парсер XML метаданных | 1.5 дня | Обход `workspace/`, парсинг `Configuration.xml` + `<Тип>/*.xml` для всех типов. Извлечение реквизитов, табчастей, форм, типов ссылок (включая составные). Создание узлов и рёбер `:HAS_ATTRIBUTE`, `:HAS_TABULAR_SECTION`, `:OF_TYPE`, `:RESOLVES_TO`, `:HAS_FORM`, `:CONTAINS`. |
| 4.6.2 — Парсер BSL для процедур | 1 день | Обход всех `*.bsl`, извлечение объявлений (regex из примера пользователя). Создание узлов `:Procedure`, `:Function`, `:Parameter`. Учёт директив `&НаКлиенте/&НаСервере/...`. Препроцессинг (вырезание комментариев и строк). |
| 4.6.3 — Резолвер вызовов | 1.5 дня | Парсинг callsite'ов, создание `:CALLS`-рёбер. Резолвер `Модуль.Метод`: ищет `:Procedure` с `full_name = "CommonModule.<Модуль>.<Метод>"`. Для неразрешимых — фолбэк на `DYNAMIC_CALL_SITE`. Парсинг `Документы.<Имя>` / `Справочники.<Имя>` для `:OPERATES_ON`. |
| 4.6.4 — Type inference (минимум) | 1 день | Простейший дата-флоу: `Х = Документы.<Имя>.СоздатьДокумент()` → у переменной `Х` локальный тип `DocumentObject.<Имя>`. Затем при `Х.<Метод>(...)` — резолв `:CALLS` к `(:DocumentObject)-[:HAS_METHOD]->(:Procedure)`. Для параметров — record use-sites, потом aggregate. |
| 4.6.5 — Watcher + инкрементальное обновление | 0.5 дня | При изменении `*.bsl` или `*.xml` через `workspace-watcher` — точечно перестраивать узлы соответствующего файла. Аналогично существующему `code_reindex_file` для Qdrant. |
| 4.6.6 — Новые MCP-tools | 0.5 дня | В `mcp-metadata-graph/server.py` добавить: `code_callers(full_name, depth=1)`, `code_callees(full_name, depth=1)`, `code_call_path(from, to)`, `find_link_path(catalog_a, catalog_b)`, `attribute_type(catalog, attribute_name)`, `dead_procedures()`, `procedures_operating_on(metadata_object)`. |

**Итого: 6 дней.** Можно ужать до 5, если фазу 4.6.4 (type inference) отложить как 4.6.x — первая версия графа без inference уже сильно полезнее текущей.

### Что НЕ входит в 4.6 (отдельные задачи будущего)

- **Полноценный type inference** с inter-procedural data-flow analysis — полноценное R&D, не нужно делать с 4.6.
- **Анализ запросов в строках.** В коде `Запрос.Текст = "ВЫБРАТЬ ..."` лежит SQL-подобный текст, который ссылается на справочники/документы. Парсинг этих строк — отдельная подзадача (можно сделать после 4.6, использует тот же граф 1 для проверки корректности имён).
- **Анализ `&Вместо` и цепочек перехватов** — это задача 4.4 в плане, и она отдельная.

---

## Технические детали для онбординга следующего чата

### Hybrid-схема Qdrant

```python
vectors_config = {"dense": VectorParams(size=768, distance=COSINE)}
sparse_vectors_config = {"sparse": SparseVectorParams(modifier=IDF)}
```

**Модели:** `intfloat/multilingual-e5-base` (dense, 768d) + `Qdrant/bm25` (sparse). Префикс `passage:` при индексации, `query:` при поиске.

**Fusion:** RRF через `query_points(prefetch=[dense, sparse], query=FusionQuery(fusion=RRF))`.

**Fingerprint:** служебная точка `id=0` с payload `{_type: "fingerprint", fingerprint: <sha256>}`. Режим `if_files_changed` сверяет его перед переиндексацией.

### Коллекции

- `platform_help` — справка платформы (40 976 точек, hybrid).
- `its_articles` — статьи ИТС (14 точек, hybrid — наполняется в рамках 2.2).
- `project_code` — код проекта (динамически через watcher).

### Аутентификация MCP SSE (после 3.2)

- Env `MCP_SHARED_SECRET` должен быть задан во всех сервисах стека + opencode (после удаления оркестратора — 13 сервисов вместо 14).
- Клиенты (`workspace_watcher`, `code_reindex_trigger`, opencode) используют `Authorization: Bearer <secret>` или `X-MCP-Secret`.
- При пустом секрете — fail-open (warning в логе).

### Аудит rest-proxy (после 3.3)

- JSONL в `/data/audit/rest-proxy.jsonl`, ротация через `RotatingFileHandler`.
- Тела ответов по флагу `AUDIT_INCLUDE_BODY=true`.
- Сервисы, которым нужно touch-ить в живую 1С, должны проходить через rest-proxy — иначе обход аудита.

### Eval runner (после 3.4)

- `docker compose --profile evals run --rm eval-runner` — прогон против полного стека.
- `scripts/eval.py --local` — против `localhost:8003` для быстрой отладки.
- Датасеты: `evals/datasets/*.jsonl`. Добавить новый — положить файл, runner подхватит.
- Отчёт в `evals/reports/YYYY-MM-DD_HH-MM.{json,md}`.

### YAxUnit на боевой конфе (после 5.1)

- Расширение `КотировкиТесты` в `workspace/tests-extension/` — единая точка для всех тестов проекта. По мере роста (и появления требования прогонять только тесты конкретной подсистемы) разбивается по префиксам: `КотировкиТесты_Аукционы`, `КотировкиТесты_УБД`, и т.д. — за счёт соглашения об именах модулей `Тест_<Подсистема>_*` это уже сейчас механически выполнимо.
- Стартовая заглушка `Тест_Общие_Пример` оставлена как живая проверка пайплайна. Не удалять — служит smoke-тестом самого контура («дошёл ли LoadConfigFromFiles до выполнения BSL»).
- Параметры прогона зафиксированы в `workspace/.opencode/skills/test-generate/SKILL.md` раздел Г: `config_path=/workspace`, `tests_path=/workspace/tests-extension`, `mode="server"`, `wait=False`. Время прогона ~70-75 сек на полную конфу.
- При ошибках смотри `/tmp/pipeline_logs/<run_id>/` (логи шагов пайплайна) и `/tmp/yaxunit-runs/<run_id>/` (результаты прогона), но **сразу** — раннер чистит эти каталоги между прогонами.
- Эмпирические значения параметров расширения (выяснены в 5.1):
  - `<ConfigurationExtensionCompatibilityMode>Version8_3_23</...>` (не 8_3_24) на платформе 8.3.24.1819
  - `<InternalInfo>` обязателен в `Configuration.xml` расширения для headless `LoadConfigFromFiles`
  - UTF-8 с BOM + CRLF — как в реальной выгрузке DESIGNER

### SonarQube для BSL (после 5.2)

- **Setup с нуля — три скрипта** (см. подробный сценарий в разделе «5.2»):
  ```bash
  python3 scripts/install_sonar_bsl_plugin.py
  docker compose restart sonarqube           # ждём ~60 сек
  python3 scripts/provision_sonar_quality_profile.py --set-default
  python3 scripts/provision_sonar_quality_gate.py --set-default
  python3 scripts/smoke_sonar_bsl.py         # должен дать ✅ PASS
  ```
- **Quality Gate** `1C BSL` — 7 условий, RULESET внутри `provision_sonar_quality_gate.py`. Идемпотентный, для правки порогов: правишь `TARGET_CONDITIONS` → `--update`. Если SonarQube 9+ скопировал унаследованные `new_*` условия от `Sonar way` — `--purge-foreign`.
- **Quality Profile** `BSL Language Server rules` — built-in от плагина 1.18 (167 правил). Кастомизируется через UI; default переключается через тот же `provision_sonar_quality_profile.py --set-default`.
- **Сетевая модель `mcp-sonarqube`:** ходит в `http://sonarqube:9000` (внутри docker-network), наружу для отладки доступен на `http://localhost:9001` (порт 9000 на хосте занят metrics-сервером MCP).
- **CE-task wait в `sonar_scan_code`:** ждёт обработку отчёта 60с, для `sonar_scan_directory` — 180с. Если в JSON-ответе появилось `ce_task: {timed_out: true}` — увеличивай таймаут (это редкость, но на холодном кеше bsl-language-server возможно). На больших проектах CE может занять минуты.
- **Имена правил BSL — `bsl-language-server:*`** (на 1.18). Не `bsl:*`, не `communitybsl:*`. Если плагин когда-либо переименует — smoke упадёт с понятным сообщением, тогда добавляй новый префикс в `BSL_RULE_PREFIXES`.

### Agent decoupling (Wave 6, в разработке)

- **Транспорт для внешних клиентов — SSE, не stdio.** Все MCP-серверы — FastMCP с состоянием в памяти (загруженные fastembed/e5 модели, Neo4j-пул, BM25 IDF-кэш). Stdio через `docker exec` означает холодный старт на каждый вызов и ломает auth-middleware (3.2) + аудит rest-proxy (3.3).
- **Конфиги клиентов — в `agent-configs/<client>/`.** При добавлении нового клиента: его конфиг и инструкция по подключению идут туда, а не в корень. Корневой `opencode-config.json` сохраняется как референс для текущего пути «через `opencode-dev`».
- **Параметризация путей через `.env`.** Все mount-paths в `docker-compose.yml` берутся из переменных с фолбэком на текущие значения. Не сводить разные источники данных (XML, hbk, статьи ИТС, workspace) в один путь — каждый индексатор должен видеть только свой источник, иначе засосёт не-1С-мусор.
- **Профиль `agent-in-container`.** OpenCode-контейнер выносится за дефолтное поведение `docker compose up`. Это сигнал внешним пользователям: «агент опционален, стек самодостаточен».
- **`description` у tools — первоисточник для клиентов без скиллов.** Cursor / Continue / частично Cline не понимают папку скиллов; всё, что они видят — `tools/list`. Качественные описания (что делает, когда вызывать, что возвращает, stateful-нюансы типа `wait=False` для `test_run_path`) — заменяют скиллы на 80%.

- ~~`scripts/smoke_auth.py` — баг в детекции 401 без заголовка~~ **— исправлено в 3.5** (переписан на `client.stream("GET", ...)`, гонка с SSE keep-alive устранена).
- ~~503 на smoke — артефакт старого теста~~ **— уточнено в 3.5**: 503 на smoke с хоста = у пользователя на машине настроен системный HTTP-прокси Windows (типично `127.0.0.1:10809` от v2rayN/Clash/Shadowsocks), `httpx` подхватывает его через `trust_env=True`. Фикс — `$env:NO_PROXY = "localhost,127.0.0.1"`. Подробности — в README → Troubleshooting.
- `mcp-rest-proxy/server.py` содержит и `__main__` (legacy), и запускается через `start.py`. Оба пути защищены middleware, но следить, чтобы при правках не возникли расхождения.
- После удаления сервиса из `docker-compose.yml` команда `docker compose up -d --build` НЕ удаляет осиротевший контейнер — нужен явный `docker rm -f <name>` или `docker compose down`. Засветилось при удалении `mcp-orchestrator` в 1.4.
- **`onec-server:8019` (yaxunit-runner) после долгого простоя или нескольких прогонов подряд может «залипнуть»** — внешне контейнер `Up`, порт слушает, но `health`-запросы повисают по таймауту. Лечится `docker restart onec-client`. Засветилось в 5.1, причина не до конца понятна — возможно, оставшиеся процессы `1cv8` от предыдущих прогонов. Кандидат на улучшение раннера (auto-cleanup), но не приоритет — рестарт занимает 5 секунд.
- ~~**`metadata-indexer` не переиндексирует автоматически при наличии существующего графа.**~~ **Закрыто задачей 4.6** (16.05.2026). Реализован флаг `METADATA_FORCE_REINDEX=true` (одноразово при старте контейнера) + инкрементальный апдейт по факту изменения отдельного файла через MCP-tools `metadata_upsert_file` / `metadata_remove_file`. Защита «✓ Граф уже существует» сохранена для дефолтного запуска — это нужно, чтобы рестарт стека не пересоздавал граф каждый раз.
- ~~**`metadata-graph` сейчас архитектурно неполный.**~~ **Закрыто задачей 4.6** (16.05.2026). Граф теперь двухслойный: 102 `:MetadataObject` на Котировках + 1545 `:Callable` + соответствующие `:Attribute`, `:Type`, `:Form`, `:Subsystem`. Десятки тысяч рёбер (`:HAS_ATTRIBUTE`, `:OF_TYPE`, `:RESOLVES_TO`, `:CALLS`, `:OPERATES_ON`, `:HAS_METHOD`). На `mcp-metadata-graph` зарегистрировано 29 tool'ов. Известные ограничения v1 (полный reindex по `Configuration.xml`, каскад `Forms/*.xml`, GC `:Type`-узлов, глобальная inter-procedural сходимость только при полном reindex) — в `HANDOFF_4_6.md` и закрываются 4.6.7 при необходимости.
- **MCP `mcp-sonarqube` после `docker compose up -d --force-recreate`** не успевает обработать MCP `initialize`-handshake до того, как OpenCode шлёт `tools/call`. Симптом — `MCP error -32602 "Invalid request parameters"`, причём с любыми параметрами. Лечится `docker restart opencode-dev` (форсирует переинициализацию SSE-сессии MCP-клиента). Засветилось в 5.1. Если повторится с другими MCP-серверами — стоит добавить retry-логику в OpenCode-клиент с экспоненциальным backoff (но это апстрим-задача, не наша).
- **`scripts/smoke_yaxunit.py`** работает с временным расширением, генерируемым в `workspace/.smoke-yaxunit/<random>/` и удаляемым после прогона. Если в `workspace/.smoke-yaxunit/` остался старый каталог (например, от прерванного прогона или ручных экспериментов) — `LoadConfigFromFiles` может на нём упасть. Лечится `Remove-Item -Recurse -Force workspace\.smoke-yaxunit`. Не блокер для рабочего цикла через `tests-extension/`, только для smoke.
- ~~**SonarQube возвращает 0 issues на любом BSL-коде.**~~ **Закрыто задачей 5.2** (06.05.2026). Полный сценарий setup'а — в разделе «5.2» выше. Закрыто пятью независимыми фиксами: (1) установка плагина, (2) Quality Gate с осмысленными порогами, (3) Quality Profile как default для bsl, (4) ожидание CE-task'а после scan'а, (5) обход системного HTTP-прокси на localhost. В preflight (`scripts/check_prereqs.py`) добавлена проверка `Sonar BSL plugin loaded` через `/api/plugins/installed` — детектит ситуацию «jar лежит, но не подхвачен» автоматически.
