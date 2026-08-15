"""
MCP-сервер: Проверка синтаксиса BSL
====================================
Использует BSL Language Server для статического анализа кода 1С.
Поддерживает:
  - проверку синтаксиса фрагмента кода
  - анализ файла .bsl
  - список доступных диагностик

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

mcp = FastMCP("1C BSL Syntax Checker")

# OBS-1. Одна строка вместо правки каждого `return json.dumps(...)`: поле
# `answerable` появляется во всех ответах, включая удачные, и у
# инструментов, которых ещё нет.
install_answerable_field(mcp)
logger = logging.getLogger(__name__)

BSL_LS_JAR = os.environ.get("BSL_LS_JAR", "/opt/bsl-language-server/bsl-ls.jar")
BSL_LS_CONFIG = os.environ.get("BSL_LS_CONFIG", "")
JAVA_OPTS = os.environ.get("JAVA_OPTS", "-Xmx512m")

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


def _run_analysis(src_path: str, config_path: str = "") -> dict:
    """
    Запускает BSL Language Server в режиме анализа.

    Возвращает либо разобранный отчёт, либо dict с ключом `error` — второе
    вызывающий код обязан отличать от пустого списка диагностик.
    """
    with tempfile.TemporaryDirectory() as outdir:
        cmd = [
            "java", *JAVA_OPTS.split(),
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
                cmd, capture_output=True, text=True, timeout=120
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
                "Анализ не уложился в 120 секунд и был прерван.",
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
        report = _run_analysis(tmpdir)

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

    report = _run_analysis(str(p.parent))
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
    has_more = offset + limit < len(summary)

    return json.dumps({
        "total_issues": total,
        "files_with_issues": files_with_issues,
        "shown": len(page),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
        "summary": page,
        "std_lookup": _std_lookup(all_codes),
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if not Path(BSL_LS_JAR).exists():
        print(f"⚠ BSL Language Server не найден: {BSL_LS_JAR}")
        print("  Скачайте с https://github.com/1c-syntax/bsl-language-server/releases")
    else:
        print(f"✓ BSL Language Server: {BSL_LS_JAR}")

    # TR-1: Streamable HTTP (/mcp, stateless) вместо SSE.
    # SEC-3: без MCP_SHARED_SECRET сервер не стартует — mcp_http.run
    # выйдет с кодом 78 и внятным сообщением, а не поднимет открытый порт.
    from mcp_http import run as run_http

    run_http(
        mcp,
        server_name="bsl-checker",
        port=int(os.environ.get("MCP_PORT", 8002)),
    )
