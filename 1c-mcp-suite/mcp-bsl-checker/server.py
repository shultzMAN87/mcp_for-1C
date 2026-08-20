"""
MCP-сервер: Проверка синтаксиса BSL
====================================
Использует BSL Language Server для статического анализа кода 1С.
Поддерживает:
  - проверку синтаксиса фрагмента кода
  - анализ файла .bsl
  - анализ каталога
  - `bsl_stats` — состояние самого анализатора (B-7)

STD-5: мост к стандартам разработки
────────────────────────────────────
К каждой диагностике добавляется поле `std_ref` — код в той форме, которую
понимает сервер `v8std` (`bslls:UsingModalWindows`), и в ответ кладётся
секция `std_lookup` со списком уникальных кодов.

Текста стандарта здесь нет и не будет. Прямой путь — сходить за ним по
сети в v8std — сломал бы требование независимости серверов: bsl-checker
единственный, у кого нет ни хранилища, ни внешних вызовов, и это его
главное свойство. Поэтому он отдаёт только якорь, а текст агент берёт
вторым шагом: `v8std_explain_diagnostics(codes)` — один вызов на весь
список, с группировкой по стандартам.

Статическую карту `diagnostic-standard-links.json` в образ (как предлагал
PLAN-STD §STD-5) класть не понадобилось: сервер v8std разрешает коды сам и
делает это по свежему корпусу, а копия в образе устаревала бы молча.
"""

import os
import json
import subprocess
import tempfile
import time
from pathlib import Path
import logging

import sys

from mcp.server.fastmcp import FastMCP

# OBS-1: единый словарь отказа. В образе всё лежит плоско в /app, при
# локальном запуске тестов — уровнем выше, в 1c-mcp-suite/.
try:
    from refusal import install_answerable_field, note_degraded, refusal
    from tool_usage import tool_names, usage_snapshot
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from refusal import install_answerable_field, note_degraded, refusal
    from tool_usage import tool_names, usage_snapshot

# B-7: состояние анализатора. Лежит рядом с server.py и в образе тоже
# попадает в /app, поэтому импорт прямой.
from bsl_health import AnalysisLog, health_report

# PERF-7: BSL LS долгоживущим процессом вместо запуска JVM на каждый вызов.
# Лежит рядом с server.py и в образе тоже попадает в /app.
from bsl_lsp import BslLspClient, LspUnavailable, to_report

# PERF-9: прогрев JVM при старте контейнера. Лежит рядом с server.py и в
# образе тоже попадает в /app.
from bsl_warmup import Warmup, say as _warmup_say

# CFG-4: файл настроек диагностик — один источник правды на оба пути
# анализа. Лежит рядом с server.py и в образе тоже попадает в /app.
import bsl_config

# B-4: единый словарь постраничности — тот же модуль, что у остальных.
try:
    from mcp_pagination import page_fields
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mcp_pagination import page_fields

mcp = FastMCP("1C BSL Syntax Checker")

# OBS-1. Одна строка вместо правки каждого `return json.dumps(...)`: поле
# `answerable` появляется во всех ответах, включая удачные, и у
# инструментов, которых ещё нет.
install_answerable_field(mcp)
logger = logging.getLogger(__name__)

BSL_LS_JAR = os.environ.get("BSL_LS_JAR", "/opt/bsl-language-server/bsl-ls.jar")
BSL_LS_CONFIG = os.environ.get("BSL_LS_CONFIG", "")
JAVA_OPTS = os.environ.get("JAVA_OPTS", "-Xmx512m")
JAVA_CMD = os.environ.get("BSL_JAVA_CMD", "java")
ANALYSIS_TIMEOUT_SEC = int(os.environ.get("BSL_ANALYSIS_TIMEOUT_SEC", "120"))

# CFG-4. Файл настроек читается ОДИН раз при старте, а не на каждый вызов:
# состав правил не должен меняться посреди сессии оттого, что кто-то
# сохранил файл. Перечитывается перезапуском контейнера — как и всё
# остальное в наборе.
_CONFIG = bsl_config.describe(BSL_LS_CONFIG)
_CONFIG_ARG = bsl_config.config_arg(_CONFIG)

# CFG-4, строгий режим. Продолжение A-5 (`--strict` в скриптах): там же,
# где мы отказались считать «набор не запускался» успехом, странно считать
# успехом «проверил не тем набором правил». По умолчанию выключен —
# деградация полезнее отказа, пока пользователь не решил иначе.
BSL_LS_CONFIG_STRICT = os.environ.get(
    "BSL_LS_CONFIG_STRICT", "false").strip().lower() in ("1", "true", "yes")

# B-7: чем ответит bsl_stats на вопрос «как ты себя чувствуешь». Пополняется
# в одном месте — в `_run_analysis`, ниже.
_analysis_log = AnalysisLog()

# PERF-7. Единственное долгоживущее состояние во всём сервере.
#
# PERF-9 изменил здесь одно слово. Раньше стояло: «процесс не поднимается
# при старте: пока никто не просил проверить код, платить за JVM не за
# что». Довод выглядел бережливым, а счёт выставлялся не тому: платил не
# контейнер простоем, а первый пришедший агент — четырнадцатью секундами
# тишины в отчёте `bsl-001`. Теперь процесс поднимается фоновым потоком при
# старте, а лениво — только если BSL_WARMUP=false.
#
# CFG-4 изменил здесь один аргумент: клиенту передаётся не сырое значение
# переменной, а путь, ПРОВЕРЕННЫЙ на существование и разбор. Раньше битый
# JSON ронял JVM на старте, `bsl_stats` показывал «LSP не поднялся», а
# причину приходилось искать в stderr контейнера.
_lsp_client = BslLspClient(
    java_cmd=JAVA_CMD, java_opts=JAVA_OPTS,
    jar=BSL_LS_JAR, config=_CONFIG_ARG,
)

_warmup = Warmup(_lsp_client)

# STD-5. Подсказка агенту одинаковая во всех трёх инструментах — держим одной
# строкой, чтобы формулировка не разъехалась при первой же правке.
STD_HINT = (
    "Коды из std_lookup.codes передай одним вызовом в v8std_explain_diagnostics "
    "(сервер v8std) — получишь описание диагностики и связанные пункты "
    "стандартов. Полный текст пункта — v8std_get_page по его id."
)


def _std_ref(code: str) -> str:
    """
    Код диагностики в форме, которую понимает v8std: `bslls:<Code>`.

    BSL LS отдаёт голое имя проверки (`UsingModalWindows`), АПК и EDT — свои
    пространства (`acc:1245`, `v8cs:...`). Если префикс уже есть, не трогаем:
    сюда может прийти отчёт, собранный не только BSL LS.
    """
    code = (code or "").strip()
    if not code:
        return ""
    return code if ":" in code else f"bslls:{code}"


def _std_lookup(diagnostics: list) -> dict:
    """Секция std_lookup: уникальные коды в порядке первого появления."""
    codes = []
    for d in diagnostics:
        ref = d.get("std_ref")
        if ref and ref not in codes:
            codes.append(ref)
    return {"tool": "v8std_explain_diagnostics", "codes": codes, "hint": STD_HINT}


# ─── FIX-9: анализ реально запускается и его отказ виден ─────────────────
#
# Здесь было три ошибки, каждой из которых хватало, чтобы инструмент всегда
# отвечал «ошибок не найдено» — в том числе на коде, нарушающем три
# диагностики сразу. Обнаружено при первой же живой проверке; проверено по
# исходникам BSL LS (`cli/AnalyzeCommand.java`, `reporters/JsonReporter.java`).
#
# 1. Ключ назывался `--src`. Такого ключа нет: он `-s, --srcDir`. Неизвестный
#    аргумент не приводил к ошибке — `srcDir` оставался пустым, то есть
#    текущим каталогом процесса (`/app`), где `.bsl` нет. В логе это видно
#    как `Analyzing files... 0/0`: анализировалось ноль файлов.
#
# 2. Отчёт искался под именем `bsl-ls_report.json` рядом с исходниками или
#    рядом с jar. `JsonReporter` пишет `bsl-json.json` в `--outputDir`,
#    который по умолчанию тоже текущий каталог. То есть даже при исправном
#    ключе `srcDir` отчёт не нашёлся бы.
#
# 3. Не найдя отчёт, функция возвращала `{"diagnostics": []}` — и вызывающий
#    код превращал это в `{"status": "ok", "message": "Ошибок не найдено"}`.
#    «Анализатор не отработал» и «код чистый» выглядели одинаково, а stdout
#    и stderr выбрасывались. Это тот же класс дефекта, что FIX-6: инструмент
#    не работает, а выглядит работающим.
#
# Отдельно про `--outputDir`: он теперь всегда указывает во временный
# каталог. Раньше отчёт ложился рядом с исходниками, а выгрузка
# конфигурации монтируется `:ro` — то есть на `bsl_check_directory` запись
# отчёта провалилась бы даже с правильными ключами.

REPORT_NAME = "bsl-json.json"


def _analyze_dir(src_path: str, config_path: str | None = None) -> dict:
    """
    Прежний путь: запуск JVM с `--analyze` на каталог.

    Возвращает либо разобранный отчёт, либо dict с ключом `error` — второе
    вызывающий код обязан отличать от пустого списка диагностик.

    FIX-30. Умолчание было пустой строкой, и это выглядело безобидно:
    «конфиг не передали — значит, не нужен». На деле передать его было
    некому — все три инструмента звали `_run_analysis` без этого
    аргумента. Теперь умолчание — `None`, то есть «взять общий», а пустая
    строка осталась осмысленной: «намеренно без конфигурации» (нужна
    сверке `bsl_lsp.py --compare`).
    """
    if config_path is None:
        config_path = _CONFIG_ARG
    # B-7. Отсутствующий jar до этой правки приезжал как `report_missing`:
    # java стартовала, писала «Unable to access jarfile» в stderr и уходила
    # с ненулевым кодом, а отчёта не было. Технически честно (stderr и код
    # возврата в ответе лежали), по смыслу неверно — «анализатор не создал
    # отчёт» и «анализатора нет» лечатся разными командами, а различать их
    # приходилось чтением чужого stderr.
    #
    # Проверка стоит один stat и делает диагноз точным: `linter_missing`
    # ровно там, где линтера действительно нет.
    if not Path(BSL_LS_JAR).exists():
        return refusal(
            "linter_missing",
            f"BSL Language Server не найден: {BSL_LS_JAR}",
            meaning=(
                "Это НЕ значит, что замечаний нет. Анализатор не "
                "запускался вообще."
            ),
            hint="Вызовите bsl_stats — он покажет, что именно отсутствует, "
                 "java или jar.",
        )

    with tempfile.TemporaryDirectory() as outdir:
        # CFG-4: команду собирает bsl_config — тот же модуль, что собирает
        # её для долгоживущего процесса. Пока это делалось здесь и в
        # bsl_lsp.py порознь, ключ `--configuration` был в обеих функциях,
        # а доезжал только в одной (FIX-30).
        cmd = bsl_config.analyze_argv(
            JAVA_CMD, JAVA_OPTS, BSL_LS_JAR, src_path, outdir, config_path)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=ANALYSIS_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            # OBS-1: до этой правки все четыре отказа анализатора и «файла
            # нет» приезжали одинаково — полем `error`. Различить «ответ
            # дан: такого файла нет» и «ответа нет: линтер лежит» было
            # нечем, хотя жёсткое правило 5 в .cursor/rules/mcp-tools.mdc
            # требует от модели именно этого различения. Мы написали
            # правило, исполнить которое было невозможно.
            return refusal(
                "analysis_timeout",
                f"Анализ не уложился в {ANALYSIS_TIMEOUT_SEC} с и был прерван.",
                meaning=(
                    "Это НЕ значит, что замечаний нет. Проверка не "
                    "завершилась, про код сейчас не известно ничего — не "
                    "делай вывод о его качестве и не отвечай по памяти."
                ),
                hint="Проверьте объём каталога и нагрузку на контейнер "
                     "mcp-bsl-checker.",
            )
        except FileNotFoundError:
            return refusal(
                "linter_missing",
                f"BSL Language Server не найден: {BSL_LS_JAR}",
                meaning=(
                    "Это НЕ значит, что замечаний нет. Анализатор не "
                    "запускался вообще."
                ),
                hint="docker compose logs --tail=50 mcp-bsl-checker",
            )

        report_path = Path(outdir) / REPORT_NAME
        if not report_path.exists():
            return refusal(
                "report_missing",
                "Анализатор не создал отчёт — результат неизвестен.",
                meaning=(
                    "Это НЕ значит, что замечаний нет. Проверка не дала "
                    "результата, про код сейчас не известно ничего."
                ),
                hint=(
                    f"Проверьте, что в {src_path} есть файлы .bsl/.os и что "
                    "java отработала: docker exec mcp-bsl-checker java -jar "
                    f"{BSL_LS_JAR} --analyze --srcDir <путь> --reporter json"
                ),
                expected_report=str(report_path),
                returncode=result.returncode,
                stdout=(result.stdout or "")[-2000:],
                stderr=(result.stderr or "")[-2000:],
            )

        try:
            with open(report_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return refusal(
                "report_unreadable",
                f"Отчёт анализатора не читается: {type(e).__name__}: {e}",
                meaning=(
                    "Это НЕ значит, что замечаний нет. Отчёт есть, но "
                    "разобрать его не удалось."
                ),
                report_path=str(report_path),
            )


def _run_analysis(src_path: str, config_path: str | None = None,
                  file_path: str = "", text: str | None = None) -> dict:
    """
    Один вход для всех трёх инструментов: выбор пути и запись исхода.

    Учёт вынесен в обёртку, а не расставлен по точкам возврата внутри:
    иначе следующая ветка отказа появится без записи, и счётчик тихо
    разойдётся с действительностью. Это ровно тот жанр, из-за которого в
    проекте четырежды расходились списки, которые надо помнить руками.

    PERF-7. Если проверяется ОДИН файл, сначала пробуем долгоживущий BSL LS
    (доли секунды вместо десяти). Не вышло — молча уходим на `--analyze`:
    ответ будет тот же, только медленный.

    CFG-4/FIX-30. «Тот же» — это обещание, и до правки оно не
    выполнялось: долгоживущий процесс поднимался с `--configuration`, а
    `--analyze` запускался без него. Один и тот же файл давал разный
    состав замечаний в зависимости от того, жив ли LSP, и признака в
    ответе не было. Теперь оба пути берут `_CONFIG_ARG` — и берут его
    из одного места, а не из аргумента, который можно забыть.

    «Молча» здесь важно и означает не «скрытно». Пользователю незачем
    видеть отказ там, где ответ получен, — но `bsl_stats` покажет и
    причину, и то, что быстрый путь не работает, а счётчик `by_mode`
    покажет, каким путём шли вызовы на самом деле.

    Каталог быстрым путём не идёт: анализ каталога занимает минуты, старт
    JVM в нём теряется, а открывать по LSP сотни файлов — другая задача.
    """
    t0 = time.monotonic()
    mode = "analyze"

    if file_path:
        try:
            diagnostics = _lsp_client.diagnostics(file_path, text=text)
            result = to_report(file_path, diagnostics)
            _analysis_log.record_ok(time.monotonic() - t0, mode="lsp")
            return result
        except LspUnavailable as exc:
            logger.info("LSP недоступен, уходим на --analyze: %s", exc)
            mode = "analyze_fallback"
        except Exception as exc:  # noqa: BLE001
            # Неожиданное в быстром пути не имеет права отменить ответ:
            # прежний путь на месте и работает.
            logger.warning("LSP упал неожиданно (%s: %s), уходим на --analyze",
                           type(exc).__name__, exc)
            mode = "analyze_fallback"

    result = _analyze_dir(src_path, config_path)
    elapsed = time.monotonic() - t0
    if isinstance(result, dict) and "error" in result:
        _analysis_log.record_fail(result.get("error", ""),
                                  result.get("message", ""), elapsed, mode=mode)
    else:
        _analysis_log.record_ok(elapsed, mode=mode)
    return result


def _config_section(src_path: str = "") -> dict:
    """
    CFG-4: чем помечается КАЖДЫЙ ответ проверки.

    Два действия в одном месте, потому что забывать их порознь — ровно то,
    что уже случилось с `config_path` (FIX-30):
      - пометить ответ деградировавшим, если ваш набор правил не применён;
      - вернуть секцию `config` для тела ответа.

    Отпечаток здесь не украшение. «Замечаний не найдено» при `mode: ONLY`
    означает «не найдено ИЗ ЭТОГО СПИСКА» — без отпечатка отчёт нельзя ни
    воспроизвести, ни сравнить с предыдущим.
    """
    reason = bsl_config.degradation_reason(_CONFIG)
    if reason:
        note_degraded(reason)
    section = bsl_config.brief(_CONFIG, src_path)
    warning = section.get("warning")
    if warning:
        note_degraded(warning)
    return section


def _strict_refusal() -> dict | None:
    """
    Отказ вместо ответа, если включён BSL_LS_CONFIG_STRICT и конфига нет.

    Смысл строгого режима — не «сломаться погромче», а не дать построить
    вывод о качестве кода на не том наборе правил. Поэтому это именно
    отказ (`answerable: false`), а не пустой результат.
    """
    if not BSL_LS_CONFIG_STRICT:
        return None
    reason = bsl_config.degradation_reason(_CONFIG)
    if not reason:
        return None
    return refusal(
        "config_invalid",
        f"Набор диагностик не применён: {reason}",
        meaning=(
            "Это НЕ значит, что замечаний нет. Проверка не запускалась: "
            "включён строгий режим, а анализ набором по умолчанию дал бы "
            "другой состав замечаний и ввёл бы в заблуждение."
        ),
        hint=("Почините файл настроек или снимите BSL_LS_CONFIG_STRICT. "
              "Подробности — bsl_stats, секция config."),
        config=bsl_config.brief(_CONFIG),
    )


@mcp.tool()
def bsl_stats() -> str:
    """
    Состояние анализатора: доступна ли java, на месте ли jar BSL Language
    Server и какой он версии, чем закончились последние запуски анализа.

    Спрашивать этим инструментом дёшево и быстро — в отличие от самой
    проверки кода, которая при мёртвом анализаторе отвечает отказом только
    через таймаут. Если `linter_available: false`, ответы `bsl_check_*`
    будут отказами, а не «замечаний не найдено».

    B-7: инструмент состояния был у трёх серверов набора из пяти. Отсутствие
    четвёртого стоило двух минут ожидания, чтобы услышать «java не найдена».
    """
    report = health_report(
        jar_path=BSL_LS_JAR,
        java_cmd=JAVA_CMD,
        java_opts=JAVA_OPTS,
        analysis_timeout_sec=ANALYSIS_TIMEOUT_SEC,
        config_path=BSL_LS_CONFIG,
        # CFG-4: полный разбор файла настроек, а не только «есть ли он».
        # Раньше `bsl_stats` отвечал на вопрос «файл на месте?», а нужный
        # вопрос — «каким набором правил получен отчёт».
        config_report=_CONFIG,
        config_strict=BSL_LS_CONFIG_STRICT,
        log=_analysis_log,
        lsp_state=_lsp_client.state(),
        warmup_state=_warmup.state(),
    )
    # TOOL-1: кого из инструментов этого сервера звали за время жизни
    # контейнера, а кого ни разу.
    report["usage"] = usage_snapshot(tool_names(mcp))
    return json.dumps(report, ensure_ascii=False, indent=2)


@mcp.tool()
def bsl_check_code(code: str) -> str:
    """
    Проверить фрагмент кода 1С (BSL) на синтаксические ошибки и соответствие стандартам.

    Параметр code — текст кода на языке 1С.
    Возвращает список диагностик: строка, код, описание и `std_ref` — код в
    форме `bslls:<Имя>`. Чтобы узнать, какой стандарт нарушен и почему,
    передай `std_lookup.codes` в `v8std_explain_diagnostics` (сервер v8std).
    """
    strict = _strict_refusal()
    if strict:
        return json.dumps(strict, ensure_ascii=False, indent=2)

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl_file = Path(tmpdir) / "Module.bsl"
        bsl_file.write_text(code, encoding="utf-8-sig")
        # Файл пишем всё равно: он нужен запасному пути `--analyze`, а
        # быстрому передаём ещё и текст — LSP разбирает его из сообщения,
        # не читая диск.
        report = _run_analysis(tmpdir, file_path=str(bsl_file), text=code)

    # CFG-4: src_path не передаём намеренно. Фрагмент кода всегда лежит во
    # временном каталоге, `configurationRoot` в нём не разрешится никогда,
    # и предупреждать об этом на каждом вызове — шум, а не сигнал.
    config = _config_section()

    if "error" in report:
        return json.dumps(report, ensure_ascii=False)

    # Извлекаем диагностики
    diagnostics = []
    if isinstance(report, dict) and "fileinfos" in report:
        for fi in report.get("fileinfos", []):
            for d in fi.get("diagnostics", []):
                diagnostics.append({
                    "line": d.get("range", {}).get("start", {}).get("line", 0) + 1,
                    "code": d.get("code", ""),
                    "std_ref": _std_ref(d.get("code", "")),
                    "message": d.get("message", ""),
                    "severity": d.get("severity", ""),
                    "source": d.get("source", ""),
                })
    elif isinstance(report, list):
        for fi in report:
            for d in fi.get("diagnostics", []):
                diagnostics.append({
                    "line": d.get("range", {}).get("start", {}).get("line", 0) + 1,
                    "code": d.get("code", ""),
                    "std_ref": _std_ref(d.get("code", "")),
                    "message": d.get("message", ""),
                    "severity": d.get("severity", ""),
                })

    if not diagnostics:
        return json.dumps({
            "status": "ok",
            "message": "Ошибок не найдено",
            "config": config,
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "issues_found",
        "count": len(diagnostics),
        "diagnostics": diagnostics,
        "std_lookup": _std_lookup(diagnostics),
        "config": config,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def bsl_check_file(file_path: str) -> str:
    """
    Проверить файл .bsl на диагностики.

    Параметр file_path — путь к файлу .bsl (внутри контейнера / смонтированного тома).
    """
    p = Path(file_path)
    if not p.exists():
        # OBS-1, вторая сторона различения. Это ОТВЕТ, а не отказ:
        # инструмент отработал и сообщает достоверный факт о мире —
        # файла по такому пути нет. Опираться на него можно, поэтому
        # `answerable` остаётся true, в отличие от четырёх отказов
        # анализатора выше. Раньше оба случая приезжали одним и тем же
        # полем `error`, и различить их было нечем.
        return json.dumps({
            "error": f"Файл не найден: {file_path}",
            "answerable": True,
            "degraded": False,
            "meaning": (
                "Файла по этому пути нет — это достоверный ответ, а не "
                "поломка инструмента. Проверь путь; про содержимое файла "
                "вывод делать не из чего."
            ),
        }, ensure_ascii=False)

    strict = _strict_refusal()
    if strict:
        return json.dumps(strict, ensure_ascii=False, indent=2)

    report = _run_analysis(str(p.parent), file_path=str(p))
    config = _config_section(str(p.parent))
    if "error" in report:
        return json.dumps(report, ensure_ascii=False, indent=2)

    diagnostics = []
    target_name = p.name.lower()
    entries = report.get("fileinfos", report if isinstance(report, list) else [])
    for fi in entries:
        fname = fi.get("path", fi.get("fileInfo", {}).get("path", ""))
        if target_name in fname.lower():
            for d in fi.get("diagnostics", []):
                diagnostics.append({
                    "line": d.get("range", {}).get("start", {}).get("line", 0) + 1,
                    "code": d.get("code", ""),
                    "std_ref": _std_ref(d.get("code", "")),
                    "message": d.get("message", ""),
                    "severity": d.get("severity", ""),
                })

    if not diagnostics:
        return json.dumps({
            "status": "ok",
            "message": f"В файле {p.name} ошибок не найдено",
            "config": config,
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "issues_found",
        "file": str(p),
        "count": len(diagnostics),
        "diagnostics": diagnostics,
        "std_lookup": _std_lookup(diagnostics),
        "config": config,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def bsl_check_directory(dir_path: str, limit: int = 50, offset: int = 0) -> str:
    """
    Проверить все .bsl файлы в каталоге.

    Параметр dir_path — путь к каталогу (например, каталог выгрузки конфигурации).
    limit/offset — пагинация по списку файлов с проблемами (по умолчанию первые 50).
    """
    p = Path(dir_path)
    if not p.is_dir():
        # См. комментарий в bsl_check_file: это ответ, а не отказ.
        return json.dumps({
            "error": f"Каталог не найден: {dir_path}",
            "answerable": True,
            "degraded": False,
            "meaning": (
                "Каталога по такому пути нет — достоверный ответ, а не "
                "поломка инструмента."
            ),
        }, ensure_ascii=False)

    strict = _strict_refusal()
    if strict:
        return json.dumps(strict, ensure_ascii=False, indent=2)

    report = _run_analysis(str(p))
    # CFG-4: здесь `configurationRoot` проверяется всерьёз — это
    # единственный инструмент, который анализирует настоящую выгрузку.
    config = _config_section(str(p))
    if "error" in report:
        return json.dumps(report, ensure_ascii=False)

    total = 0
    files_with_issues = 0
    summary = []
    # STD-5: коды собираем по ВСЕМ файлам, а не по странице пагинации —
    # иначе список стандартов зависел бы от того, какой offset запросили.
    all_codes = []
    entries = report.get("fileinfos", report if isinstance(report, list) else [])
    for fi in entries:
        diags = fi.get("diagnostics", [])
        if diags:
            files_with_issues += 1
            total += len(diags)
            fname = fi.get("path", fi.get("fileInfo", {}).get("path", "?"))
            summary.append({
                "file": fname,
                "issues": len(diags),
                "first_issue": diags[0].get("message", ""),
            })
            for d in diags:
                all_codes.append({"std_ref": _std_ref(d.get("code", ""))})

    # Применяем пагинацию ко всему списку, а не вырезаем первые 50 молча
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    page = summary[offset:offset + limit]

    # B-4: блок постраничности собирается общим модулем, а не здесь. Своя
    # формула жила рядом с чужими именами: `shown` вместо `returned`, а
    # поля `total` не было вовсе — его место занимало `total_issues`,
    # которое считает ЗАМЕЧАНИЯ, а не строки списка. Агент, приученный
    # сверять `returned` с `total`, на этом сервере получал пустоту.
    return json.dumps({
        "total_issues": total,
        "files_with_issues": files_with_issues,
        **page_fields(len(summary), offset, limit, len(page)),
        "note_pagination": (
            "total — сколько файлов с замечаниями в списке; общее число "
            "самих замечаний лежит в total_issues"
        ),
        "summary": page,
        "std_lookup": _std_lookup(all_codes),
        "config": config,
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if not Path(BSL_LS_JAR).exists():
        _warmup_say(f"⚠ BSL Language Server не найден: {BSL_LS_JAR}", err=True)
        _warmup_say("  Скачайте с https://github.com/1c-syntax/"
                    "bsl-language-server/releases", err=True)
    else:
        _warmup_say(f"✓ BSL Language Server: {BSL_LS_JAR}")
        # PERF-9: поднимаем JVM фоном, не задерживая открытие порта.
        # Запрос, пришедший в середине прогрева, встанет на замке внутри
        # клиента и получит готовый процесс — вторая JVM не появится.
        _warmup.start_background()

    # TR-1: Streamable HTTP (/mcp, stateless) вместо SSE.
    # SEC-3: без MCP_SHARED_SECRET сервер не стартует — mcp_http.run
    # выйдет с кодом 78 и внятным сообщением, а не поднимет открытый порт.
    from mcp_http import run as run_http

    run_http(
        mcp,
        server_name="bsl-checker",
        port=int(os.environ.get("MCP_PORT", 8002)),
    )
