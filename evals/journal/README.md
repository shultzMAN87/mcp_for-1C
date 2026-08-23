# Журнал вызовов (`EVAL-7`)

Сюда серверы пишут трассу вызовов на время **ручного прогона**
(`evals/manual-prompts.md`). В гит попадает только этот файл — сами
журналы игнорируются: они относятся к одному прогону и живут рядом с его
бланком в `docs/archive/ПРОГОН-AGENT-<дата>.md`.

Включается двумя переменными в `.env` и перезапуском серверов:

```
MCP_TOOL_JOURNAL=/journal/calls.jsonl
V8STD_USAGE_LOG=/journal/v8std.jsonl
```

```powershell
docker compose up -d --force-recreate mcp-metadata-graph mcp-bsl-checker mcp-platform-help mcp-query-builder v8std-mcp
```

Проверить, что журнал включился, можно по логу старта: каждый сервер
печатает либо путь журнала, либо «журнал вызовов выключен».

Разбор — `python scripts/journal_report.py`, разметка сценариев —
`python scripts/journal_report.py --mark 7`.

Почему два файла, а не один: `v8std` — чужой сервер, он пишет своим
ключом `--usage-log` и в свой файл. Формат строки общий, читатель
объединяет обе половины сам.

Выключать после прогона не обязательно, но полезно: журнал растёт на
каждый вызов.
