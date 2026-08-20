"""
CFG-4: файл настроек BSL Language Server — один источник правды
===============================================================

Зачем модуль, если переменная `BSL_LS_CONFIG` была и раньше
─────────────────────────────────────────────────────────────
Была переменная, и был ключ `--configuration` в двух местах кода. Не было
трёх вещей, и без них механизм не работал:

1. `FIX-30`. Параметр `config_path` у `_run_analysis` существовал, но его
   не передавал НИ ОДИН из трёх инструментов (`server.py`, строки 373, 439,
   491 до правки). То есть конфигурация доезжала только до долгоживущего
   BSL LS — быстрого пути. `bsl_check_directory` шёл через `--analyze` и
   всегда работал на наборе диагностик по умолчанию, а `bsl_check_file`
   при упавшем LSP молча менял набор правил посреди сессии.

   Один и тот же файл давал два разных ответа, и признака в ответе не
   было. Это тот же жанр, что `FIX-6` и `FIX-16`: инструмент не работает,
   а выглядит работающим.

2. Проверки содержимого. Битый JSON BSL LS не чинит: в режиме LSP он
   валится на старте (то есть быстрый путь исчезает молча, а `bsl_stats`
   показывает «LSP не поднялся» без причины), в `--analyze` — пишет в
   stderr и считает по умолчанию. Пустой файл и файл с опечаткой в имени
   диагностики выглядят одинаково успешно.

3. Отпечатка в ответе. «Замечаний нет» без указания, каким набором правил
   это получено, — не ответ. При `mode: ONLY` набор задаёт СОСТАВ
   проверок, и отчёт без отпечатка невозможно ни воспроизвести, ни
   сравнить с прошлым.

Что делает модуль
──────────────────
Читает файл один раз при старте (`describe`) и отдаёт словарь, который
кладётся и в `bsl_stats`, и в каждый ответ проверки. Решение «передавать
ли `--configuration`» принимается здесь же: невалидный файл НЕ
передаётся анализатору — иначе оба пути ложатся разом, и вместо
деградации получается полный отказ.

Про `configurationRoot`
────────────────────────
Параметр указывает каталог корня конфигурации ОТНОСИТЕЛЬНО анализируемых
исходников, а не относительно файла настроек. У нас выгрузка монтируется
в `/data/1c-src`, а `bsl_check_code` вообще работает во временном
каталоге. Поэтому `check_root` сверяет его с фактическим `srcDir` каждого
вызова и говорит вслух, когда каталога там нет: молчаливо не сработавший
`configurationRoot` выключает часть диагностик, ничего не сообщая.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Ключи, которые читаем из файла настроек. Список нарочно короткий: модуль
# не валидирует схему BSL LS (это работа самого BSL LS и его JSON Schema),
# он отвечает на вопрос «какой набор правил применён» — и только.
_MODE_MEANING = {
    "ONLY": "включены ТОЛЬКО перечисленные диагностики, остальные выключены",
    "EXCEPT": "включено всё по умолчанию, КРОМЕ перечисленных",
    "ON": "включено всё по умолчанию, перечисленные — по своим настройкам",
    "OFF": "все диагностики выключены",
}


def _fingerprint(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:12]


def _count_diagnostics(params: dict) -> tuple[int, int]:
    """
    Сколько диагностик перечислено и сколько из них включено.

    Значение бывает не только булевым: у диагностики с параметрами это
    объект (`{"maxComplexity": 15}`), и он тоже означает «включена».
    Выключена — только явное `false`.
    """
    declared = len(params)
    enabled = sum(1 for v in params.values() if v is not False)
    return declared, enabled


def describe(path: str) -> dict:
    """
    Прочитать файл настроек и сказать про него всё, что нужно знать.

    Не бросает исключений: сервер обязан подняться и при битом конфиге,
    иначе одна опечатка в JSON выключает проверку кода целиком.
    """
    path = (path or "").strip()
    if not path:
        return {
            "path": "",
            "requested": False,
            "present": None,
            "valid": None,
            "applied": False,
            "note": ("BSL_LS_CONFIG не задан — анализ идёт на наборе "
                     "диагностик по умолчанию"),
        }

    info = {
        "path": path,
        "requested": True,
        "present": False,
        "valid": False,
        "applied": False,
    }

    p = Path(path)
    if not p.exists():
        info["error"] = f"файл настроек диагностик не найден: {path}"
        info["hint"] = (
            "Проверьте монтирование в docker-compose.yml "
            "(./bsl-config:/app/bsl-config:ro) и переменную BSL_LS_CONFIG."
        )
        return info

    info["present"] = True
    try:
        raw = p.read_bytes()
    except OSError as exc:
        info["error"] = f"файл настроек не читается: {type(exc).__name__}: {exc}"
        return info

    info["fingerprint"] = _fingerprint(raw)
    info["size_bytes"] = len(raw)

    try:
        # utf-8-sig: файл настроек часто редактируют в конфигураторе или
        # блокноте Windows, и BOM в начале — обычное дело. json.loads на
        # нём падает, а причина выглядит как «Expecting value: line 1».
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 — причина уходит в ответ целиком
        info["error"] = f"файл настроек не разбирается как JSON: {exc}"
        info["hint"] = (
            "Пока JSON битый, ключ --configuration анализатору НЕ "
            "передаётся: иначе BSL LS не поднимется ни быстрым путём, ни "
            "запасным. Проверьте файл любым линтером JSON."
        )
        return info

    if not isinstance(data, dict):
        info["error"] = "файл настроек разобран, но верхний уровень не объект"
        return info

    info["valid"] = True
    info["applied"] = True

    diagnostics = data.get("diagnostics") or {}
    if isinstance(diagnostics, dict):
        mode = diagnostics.get("mode")
        if mode:
            info["mode"] = mode
            info["mode_meaning"] = _MODE_MEANING.get(
                str(mode).upper(), "режим неизвестен этому модулю")
        params = diagnostics.get("parameters")
        if isinstance(params, dict):
            declared, enabled = _count_diagnostics(params)
            info["diagnostics_declared"] = declared
            info["diagnostics_enabled"] = enabled

    if data.get("language"):
        info["language"] = data["language"]
    if data.get("configurationRoot"):
        info["configuration_root"] = data["configurationRoot"]
    if data.get("skipSupport"):
        info["skip_support"] = data["skipSupport"]

    return info


def config_arg(info: dict) -> str:
    """
    Путь для ключа `--configuration` — или пустая строка.

    Единственное место, где принимается это решение. Невалидный файл не
    передаётся анализатору намеренно: см. заголовок модуля.
    """
    return info.get("path", "") if info.get("applied") else ""


def degradation_reason(info: dict) -> str:
    """
    Чем помечать ответ, если конфигурация запрошена, но не применена.

    Пустая строка означает «всё в порядке, помечать нечем»: либо
    конфигурация применена, либо её не просили.
    """
    if not info.get("requested") or info.get("applied"):
        return ""
    return (
        f"{info.get('error', 'файл настроек диагностик недоступен')} — "
        "проверка идёт на наборе диагностик ПО УМОЛЧАНИЮ, а не на вашем. "
        "Состав замечаний будет другим: и лишние, и недостающие."
    )


# Куда в контейнере смонтирована выгрузка конфигурации. Тот же список, что
# в `bsl_lsp.SOURCE_ROOTS`, и по той же причине: путь монтирования — вещь,
# которую держат в голове ровно до первого раза, когда она понадобилась.
# Копией он быть не может (bsl_lsp импортирует этот модуль, не наоборот),
# поэтому расхождение сторожит tests_bsl_config.py.
SOURCE_ROOTS = ("/data/1c-src", "/workspace", "/data/1c-config")


def check_root(info: dict, src_path: str) -> str:
    """
    Разрешится ли `configurationRoot` при анализе этого каталога.

    BSL LS ищет корень конфигурации внутри анализируемых исходников. Если
    каталога там нет, часть диагностик (те, что смотрят на метаданные и на
    режим поддержки) молча не отработает — отчёт будет выглядеть чище, чем
    код на самом деле.
    """
    root = info.get("configuration_root")
    if not root or not src_path:
        return ""
    try:
        if (Path(src_path) / root).exists():
            return ""
    except OSError:
        return ""
    return (
        f"configurationRoot='{root}' не найден внутри {src_path} — "
        "диагностики, которым нужны метаданные конфигурации, не отработают. "
        "Путь ищется ОТНОСИТЕЛЬНО анализируемого каталога, а не относительно "
        "файла настроек."
    )


def check_mounted_workspace(info: dict, roots=SOURCE_ROOTS) -> dict:
    """
    CFG-4.1: то же самое, но про СМОНТИРОВАННУЮ выгрузку, а не про каталог
    конкретного вызова.

    Зачем отдельно от `check_root`. Тот срабатывает только когда кто-то
    позвал `bsl_check_directory` — то есть о неработающем
    `configurationRoot` узнаёшь в момент, когда уже читаешь отчёт и веришь
    ему. Проверять это руками (`docker exec … ls /data/1c-src`) можно, но
    ровно один раз: на второй неделе про такую команду не помнит никто.

    Поэтому `bsl_stats` отвечает на вопрос сразу и без анализа. Разбор
    ответа тот же, что у всей секции: `applied` не равно «работает».
    """
    root = info.get("configuration_root")
    if not root:
        return {}
    checked = []
    for candidate in roots:
        base = Path(candidate)
        if not base.is_dir():
            continue
        checked.append(candidate)
        if (base / root).is_dir():
            return {"root": root, "found_in": candidate, "resolves": True}
    if not checked:
        return {
            "root": root,
            "resolves": None,
            "note": ("выгрузка в контейнер не смонтирована — проверить "
                     "нечем; для bsl_check_directory это и так означает, "
                     "что анализировать нечего"),
        }
    return {
        "root": root,
        "resolves": False,
        "searched": checked,
        "warning": (
            f"configurationRoot='{root}' не найден ни в одном из "
            f"{', '.join(checked)} — диагностики, которым нужны метаданные "
            "конфигурации, не отработают, и отчёт будет выглядеть чище, чем "
            "код. Поправьте путь в файле настроек или уберите параметр: "
            "тогда хотя бы не будет видимости, что они работают."
        ),
    }


def should_refuse(info: dict, strict: bool) -> bool:
    """
    Отвечать ли отказом вместо анализа (строгий режим).

    Правило вынесено сюда, а не оставлено в `server.py`, по той же
    причине, что и `analyze_argv` ниже: `server.py` невозможно
    импортировать без пакета `mcp`, то есть проверка этого правила не
    попала бы в прогон на голом Python — а именно так гоняются все 43
    набора проекта.
    """
    return bool(strict and degradation_reason(info))


# ─── argv: одна команда, два вызывающих ──────────────────────────────────
#
# FIX-30 случился не оттого, что кто-то поленился, а оттого, что команду
# для JVM собирали в двух местах: `server._analyze_dir` и
# `bsl_lsp._default_launcher`. Добавить `--configuration` в одно и забыть
# про другое — вопрос времени, и время вышло.
#
# Пока сборка argv живёт в двух функциях, любой следующий ключ (`--silent`,
# `-w/--workspaceDir`, смена репортера) заведёт расхождение заново. Поэтому
# argv собирается здесь, а обе стороны только зовут.


def analyze_argv(java_cmd: str, java_opts: str, jar: str, src_path: str,
                 out_dir: str, config: str = "",
                 reporter: str = "json") -> list[str]:
    """Команда пакетного анализа: `--analyze`."""
    argv = [java_cmd, *java_opts.split(), "-jar", jar,
            "--analyze", "--srcDir", src_path,
            "--outputDir", out_dir, "--reporter", reporter]
    if config:
        argv.extend(["--configuration", config])
    return argv


def lsp_argv(java_cmd: str, java_opts: str, jar: str,
             config: str = "") -> list[str]:
    """Команда долгоживущего процесса в режиме LSP (PERF-7)."""
    argv = [java_cmd, *java_opts.split(), "-jar", jar]
    if config:
        argv.extend(["--configuration", config])
    return argv


def brief(info: dict, src_path: str = "") -> dict:
    """
    Компактная секция `config` для ответа инструмента проверки.

    Полная картина живёт в `bsl_stats`; здесь ровно то, без чего нельзя
    прочитать отчёт: применён ли ваш набор правил и какой именно.
    """
    out = {"applied": bool(info.get("applied"))}
    if info.get("path"):
        out["path"] = info["path"]
    if info.get("fingerprint"):
        out["fingerprint"] = info["fingerprint"]
    if info.get("mode"):
        out["mode"] = info["mode"]
    if info.get("diagnostics_enabled") is not None and info.get("applied"):
        out["diagnostics_enabled"] = info.get("diagnostics_enabled")
    if not info.get("applied"):
        out["ruleset"] = "по умолчанию (BSL Language Server)"
        if info.get("error"):
            out["error"] = info["error"]
        if info.get("note"):
            out["note"] = info["note"]
    warning = check_root(info, src_path)
    if warning:
        out["warning"] = warning
    return out
