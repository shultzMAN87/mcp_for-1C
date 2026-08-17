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
    from refusal import install_answerable_field, refusal
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from refusal import install_answerable_field, refusal

# B-7: состояние анализатора. Лежит рядом с server.py и в образе тоже
# попадает в /app, поэтому импорт прямой.
from bsl_health import AnalysisLog, health_report

# PERF-7: BSL LS долгоживущим процессом вместо запуска JVM на каждый вызов.
# Лежит рядом с server.py и в образе тоже попадает в /app.
from bsl_lsp import BslLspClient, LspUnavailable, to_report

# PERF-9: прогрев JVM при старте контейнера. Лежит рядом с server.py и в
# образе тоже попадает в /app.
from bsl_warmup import Warmup, say as _warmup_say

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
_lsp_client = BslLspClient(
    java_cmd=JAVA_CMD, java_opts=JAVA_OPTS,
    jar=BSL_LS_JAR, config=BSL_LS_CONFIG,
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


def _analyze_dir(src_path: str, config_path: str = "") -> dict:
    """
    Прежний путь: запуск JVM с `--analyze` на каталог.

    Возвращает либо разобранный отчёт, либо dict с ключом `error` — второе
    вызывающий код обязан отличать от пустого списка диагностик.
    """
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
        cmd = [
            JAVA_CMD, *JAVA_OPTS.split(),
            "-jar", BSL_LS_JAR,
            "--analyze",
            "--srcDir", src_path,
            "--outputDir", outdir,
            "--reporter", "json",
        ]
        if config_path:
            cmd.extend(["--configuration", config_path])

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


def _run_analysis(src_path: str, config_path: str = "",
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
    return json.dumps(
        health_report(
            jar_path=BSL_LS_JAR,
            java_cmd=JAVA_CMD,
            java_opts=JAVA_OPTS,
            analysis_timeout_sec=ANALYSIS_TIMEOUT_SEC,
            config_path=BSL_LS_CONFIG,
            log=_analysis_log,
            lsp_state=_lsp_client.state(),
            warmup_state=_warmup.state(),
        ),
        ensure_ascii=False, indent=2,
    )


@mcp.tool()
def bsl_check_code(code: str) -> str:
    """
    Проверить фрагмент кода 1С (BSL) на синтаксические ошибки и соответствие стандартам.

    Параметр code — текст кода на языке 1С.
    Возвращает список диагностик: строка, код, описание и `std_ref` — код в
    форме `bslls:<Имя>`. Чтобы узнать, какой стандарт нарушен и почему,
    передай `std_lookup.codes` в `v8std_explain_diagnostics` (сервер v8std).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl_file = Path(tmpdir) / "Module.bsl"
        bsl_file.write_text(code, encoding="utf-8-sig")
        # Файл пишем всё равно: он нужен запасному пути `--analyze`, а
        # быстрому передаём ещё и текст — LSP разбирает его из сообщения,
        # не читая диск.
        report = _run_analysis(tmpdir, file_path=str(bsl_file), text=code)

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
        return json.dumps({"status": "ok", "message": "Ошибок не найдено"}, ensure_ascii=False)

    return json.dumps({
        "status": "issues_found",
        "count": len(diagnostics),
        "diagnostics": diagnostics,
        "std_lookup": _std_lookup(diagnostics),
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

    report = _run_analysis(str(p.parent), file_path=str(p))
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
        return json.dumps({"status": "ok", "message": f"В файле {p.name} ошибок не найдено"}, ensure_ascii=False)

    return json.dumps({
        "status": "issues_found",
        "file": str(p),
        "count": len(diagnostics),
        "diagnostics": diagnostics,
        "std_lookup": _std_lookup(diagnostics),
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

    report = _run_analysis(str(p))
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
