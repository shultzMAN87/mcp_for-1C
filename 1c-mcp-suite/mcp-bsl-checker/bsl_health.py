"""
B-7. «Как ты себя чувствуешь» для bsl-checker.
===============================================

Зачем
─────
Находка `AUDIT-2`: инструмент состояния есть у трёх серверов из пяти
(`metadata_stats`, `platform_help_stats`, `v8std_stats`), а у `bsl-checker`
нет вовсе. `OBS-1` научил его отказы различать — `linter_missing`,
`analysis_timeout`, `report_missing`, `report_unreadable` приезжают
по-разному и честно говорят «ответа нет». Но узнаёт об этом модель только
после того, как дождётся конца анализа: до двух минут ожидания, чтобы
услышать «java не найдена».

Спросить заранее было нечем. Теперь есть.

Почему модуль отдельный, а не код в server.py
─────────────────────────────────────────────
Та же причина, по которой отделён `graph_state.py`: `server.py` при импорте
поднимает FastMCP, поэтому юнит-тестами он не покрывается ни в песочнице,
ни на хосте, где пакет `mcp` не установлен. Здесь зависимостей нет, кроме
stdlib, и всё это проверяется напрямую (`tests_bsl_health.py`).

Границы: модуль ничего не решает про код 1С и ничего не запускает, кроме
`java -version`. Он смотрит на свой контейнер и рассказывает, что видит.

Кеша здесь нет — и это решение, а не упущение
─────────────────────────────────────────────
Урок приёмки 15 августа: `metadata_stats` был обёрнут в `@cached(ttl=600)`
и во время аварии отдавал из кеша картину здоровья работающего графа.
К диагностическому инструменту приходят с вопросом «жив ли он ПРЯМО
СЕЙЧАС», и кешированный ответ на такой вопрос не устарел, а перевёрнут.

Проба стоит дёшево: `java -version` не грузит jar анализатора и
укладывается в доли секунды, версия jar читается из манифеста zip-ом, без
запуска JVM вообще. Экономить тут не на чем.

Что считается ответом, а что отказом
────────────────────────────────────
`bsl_stats` отвечает ВСЕГДА, в том числе когда анализатор мёртв: «java не
найдена» — это достоверный ответ на заданный вопрос, а не поломка
инструмента. Поэтому `answerable: true` даже при разобранном линтере, а
неготовность видна по `linter_available: false` и `degraded: true`.

Ровно так же устроен `metadata_stats` после `FIX-3`: он обязан отвечать и
на пустом графе, потому что его дело — отличить «объекта нет» от «граф не
построен». Инструмент состояния, который отказывается отвечать, когда
плохо, бесполезен именно в тот момент, ради которого написан.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import zipfile
from pathlib import Path

# CFG-4.1: разбор файла настроек и проверка configurationRoot. Модуль
# лежит рядом, в образе оба файла попадают в /app.
import bsl_config

__all__ = [
    "PROBE_TIMEOUT_SEC",
    "parse_java_version",
    "probe_java",
    "jar_manifest",
    "jar_version",
    "jar_diagnostics",
    "diagnostics_inventory",
    "probe_jar",
    "AnalysisLog",
    "health_report",
]


# B-2: короткий таймаут по образцу `HELP_QDRANT_TIMEOUT_SEC`. Локальный
# `java -version` либо отвечает за доли секунды, либо не отвечает вовсе;
# длинное ожидание тут не спасает, а только превращает диагностику в ещё
# одно место, где всё висит.
PROBE_TIMEOUT_SEC = float(os.environ.get("BSL_PROBE_TIMEOUT_SEC", "5"))


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# ─── java ────────────────────────────────────────────────────────────────


def parse_java_version(text: str) -> dict:
    """
    Разбирает вывод `java -version`.

    Вывод идёт в stderr и выглядит так:

        openjdk version "17.0.10" 2024-01-16
        OpenJDK Runtime Environment Temurin-17.0.10+7 (build 17.0.10+7)
        OpenJDK 64-Bit Server VM Temurin-17.0.10+7 (build ...)

    Разбор нарочно снисходительный: не узнали формат — отдаём пустую версию
    и первую строку как есть. Диагностика, которая падает на незнакомой
    сборке JVM, хуже диагностики, которая говорит «вижу вот это».
    """
    text = (text or "").strip()
    if not text:
        return {"version": "", "runtime": "", "raw": ""}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    m = re.search(r'version\s+"([^"]+)"', text)
    if not m:
        m = re.search(r"version\s+(\d+(?:\.\d+)*(?:[+_-]\S+)?)", text)
    return {
        "version": m.group(1) if m else "",
        "runtime": lines[1] if len(lines) > 1 else lines[0],
        "raw": lines[0],
    }


def _default_runner(cmd: list, timeout: float):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_java(java_cmd: str = "java", timeout: float | None = None,
               runner=None) -> dict:
    """
    Есть ли java и какая. Возвращает словарь, исключений не бросает.

    `runner` подменяется в тестах: запускать настоящую JVM ради проверки
    разбора строки незачем, а на хосте пользователя её может не быть вовсе.
    """
    timeout = PROBE_TIMEOUT_SEC if timeout is None else timeout
    runner = runner or _default_runner
    out = {"available": False, "command": java_cmd, "version": "",
           "runtime": "", "probe_ms": 0, "error": ""}
    t0 = time.monotonic()
    try:
        result = runner([java_cmd, "-version"], timeout)
    except FileNotFoundError:
        out["error"] = f"исполняемый файл не найден: {java_cmd}"
    except subprocess.TimeoutExpired:
        out["error"] = f"java не ответила за {timeout:g} с"
    except Exception as exc:  # noqa: BLE001 — проба не имеет права падать
        out["error"] = f"{type(exc).__name__}: {exc}"
    else:
        # `java -version` пишет в stderr — это её штатное поведение, а не
        # признак ошибки. Смотрим оба потока, чтобы не зависеть от сборки.
        text = (getattr(result, "stderr", "") or "") + \
               (getattr(result, "stdout", "") or "")
        parsed = parse_java_version(text)
        rc = getattr(result, "returncode", 0)
        if rc == 0:
            out.update(available=True, version=parsed["version"],
                       runtime=parsed["runtime"])
        else:
            out["error"] = f"java вернула код {rc}: {text.strip()[:300]}"
    out["probe_ms"] = int((time.monotonic() - t0) * 1000)
    return out


# ─── jar анализатора ─────────────────────────────────────────────────────


def jar_manifest(path) -> dict:
    """
    META-INF/MANIFEST.MF из jar — обычным zip-ом, без запуска JVM.

    Продолжения строк (перенос с ведущим пробелом) склеиваются: по спецификации
    манифеста длинная строка режется на 72 байта, и `Implementation-Version`
    вполне может приехать разорванной.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except Exception:
        return {}

    fields: dict[str, str] = {}
    key = ""
    for line in raw.splitlines():
        if not line.strip():
            key = ""
            continue
        if line.startswith((" ", "\t")) and key:
            fields[key] += line[1:].strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
    return fields


def jar_version(path) -> str:
    """
    Версия BSL Language Server.

    Порядок источников — от надёжного к правдоподобному: манифест, потом имя
    файла. В образе jar называется `bsl-ls.jar` без номера (версия задана
    `ARG BSL_LS_VERSION` в Dockerfile.bsl и в имени не остаётся), поэтому
    второй путь сработает только у того, кто подложил свой файл.
    """
    fields = jar_manifest(path)
    for key in ("Implementation-Version", "Bundle-Version",
                "Specification-Version"):
        value = (fields.get(key) or "").strip()
        if value:
            return value
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", Path(path).name)
    return m.group(1) if m else ""


def probe_jar(path) -> dict:
    """Файл анализатора: на месте ли, какого размера, какой версии."""
    p = Path(path)
    out = {"path": str(p), "present": False, "version": "",
           "size_mb": 0.0, "modified_iso": "", "error": ""}
    try:
        st = p.stat()
    except FileNotFoundError:
        out["error"] = "файла нет"
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["present"] = True
    out["size_mb"] = round(st.st_size / (1024 * 1024), 1)
    out["modified_iso"] = _iso(st.st_mtime)

    # FIX-11 в миниатюре: заглушка на 0 байт — это не «файл есть».
    # Сорок таких заглушек когда-то прошли check_prereqs с галочкой.
    if st.st_size < 1024:
        out["present"] = False
        out["error"] = (f"файл есть, но в нём {st.st_size} байт — "
                        "это не jar, а заглушка или обрывок закачки")
        return out

    out["version"] = jar_version(p)
    if not out["version"]:
        out["error"] = ("версия не определена: в манифесте нет "
                        "Implementation-Version, в имени файла — номера")
    return out


# ─── CFG-5: какие диагностики вообще существуют в этом jar ───────────────
#
# Задача. Файл настроек объявляет 85 диагностик, `mode: ONLY` включает
# ТОЛЬКО их. Опечатка в имени (`UsingModalWindws` вместо `UsingModalWindows`)
# для BSL LS — просто неизвестный ключ: он его игнорирует. Мы же считаем
# диагностику объявленной, `diagnostics_enabled` показывает на единицу
# больше правды, а проверка, ради которой строку писали, молча выключена.
#
# PLAN-9 предлагал ловить предупреждения BSL LS в stderr при прогреве и
# честно признавал, что тот может ничего не писать. Проверять это нечем
# без стенда — и не нужно: состав диагностик лежит в самом jar.
#
# Как. BSL Language Server выводит код диагностики из имени класса: класс
# `EmptyCodeBlockDiagnostic` даёт код `EmptyCodeBlock`. Правило не наше — оно
# и есть механизм разрешения кодов внутри анализатора, поэтому список,
# собранный по именам классов, не может разойтись с тем, что анализатор
# признаёт.
#
# Почему это лучше JSON Schema. Схему пришлось бы тянуть в образ и держать
# в соответствии с `BSL_LS_VERSION` руками — то есть завести шестой
# рукописный список проекта. Здесь источник и есть тот файл, который
# исполняется: обновили `BSL_LS_VERSION` — список обновился сам.
#
# Цена: чтение центрального каталога zip. Имена без распаковки, на jar в
# 43 МБ это единицы миллисекунд, и делается один раз при старте.
_DIAG_CLASS_RE = re.compile(
    r"(?:^|/)diagnostics/([A-Za-z][A-Za-z0-9]*)Diagnostic\.class$")

# Ниже этого числа считаем, что раскладка jar другая и мы её не поняли.
# Тогда честный ответ — «не знаю», а не «все 85 ваших диагностик выдуманы»:
# сторож, который при непонимании обвиняет пользователя, хуже отсутствующего.
_DIAG_SANITY_MIN = 50


def jar_diagnostics(path) -> set[str]:
    """
    Коды диагностик, которые знает этот jar. Пустое множество — «не смогли».

    Вложенные классы (`XxxDiagnostic$1.class`) отсеиваются регулярным
    выражением: код диагностики у них тот же, и они дали бы дубли.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except Exception:
        return set()
    found = {m.group(1) for m in
             (_DIAG_CLASS_RE.search(n) for n in names) if m}
    return found if len(found) >= _DIAG_SANITY_MIN else set()


def diagnostics_inventory(jar_path, config_info: dict) -> dict:
    """
    Сверка «что объявлено» с «что существует».

    Возвращает секцию для `bsl_stats.config`. Два числа отвечают на два
    разных вопроса:

      `unknown` (CFG-5) — объявлено, но такой диагностики в jar нет. Почти
          всегда опечатка, и почти всегда она означает молча выключенную
          проверку;
      `not_declared` (CFG-6) — существует, но не объявлено. При `mode: ONLY`
          это выключенные диагностики, в том числе НОВЫЕ, приехавшие с
          обновлением `BSL_LS_VERSION`. Их не перечисляем поимённо —
          их сотни; важно само число и то, что оно меняется при обновлении.
    """
    known = jar_diagnostics(jar_path)
    out: dict = {"known_in_jar": len(known)}
    if not known:
        out["note"] = (
            "состав диагностик из jar определить не удалось — раскладка "
            "архива не та, которую мы умеем читать. Опечатка в имени "
            "диагностики останется незамеченной; это ограничение, а не "
            "поломка: анализ идёт как обычно"
        )
        return out

    declared = set(config_info.get("diagnostics_names") or [])
    if not declared:
        return out

    unknown = sorted(declared - known)
    out["unknown"] = unknown
    out["not_declared_count"] = len(known - declared)
    if unknown:
        out["warning"] = (
            f"объявлено {len(unknown)} диагностик, которых нет в BSL LS "
            f"{jar_version(jar_path) or 'этой версии'}: "
            f"{', '.join(unknown[:5])}"
            + (" …" if len(unknown) > 5 else "")
            + ". BSL LS игнорирует неизвестные ключи молча; при mode: ONLY "
              "это означает выключенную проверку, а не лишнюю строку."
        )
    return out


# ─── журнал анализов ─────────────────────────────────────────────────────


class AnalysisLog:
    """
    Что случилось с последними запусками анализатора.

    Живёт в памяти процесса и обнуляется его перезапуском — так и написано
    в ответе. Хранилища у `bsl-checker` нет и заводить его ради счётчиков
    неправильно: сервер без состояния — его главное свойство, из-за него он
    единственный в наборе не зависит ни от чего внешнего.

    Блокировка нужна: FastMCP исполняет синхронные инструменты в рабочем
    потоке на запрос, и два анализа идут параллельно.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self.started_at = clock()
        self.runs_total = 0
        self.runs_ok = 0
        self.runs_failed = 0
        self.last_ok_at: float | None = None
        self.last_ok_sec: float | None = None
        self.last_fail_at: float | None = None
        self.last_fail_error = ""
        self.last_fail_message = ""
        # Счётчик по кодам отказа: видно, что именно ломается — таймаут на
        # большом каталоге и отсутствующая java лечатся по-разному.
        self.failures_by_error: dict[str, int] = {}
        # PERF-7: каким путём шёл анализ — `lsp` (долгоживущий процесс),
        # `analyze` (запуск JVM) или `analyze_fallback` (LSP не сработал,
        # ушли на прежний путь). Это главное число после правки: если
        # быстрый путь не используется, выигрыш существует только на
        # бумаге, и увидеть это надо не по секундомеру, а по счётчику.
        self.by_mode: dict[str, int] = {}
        self.last_mode = ""

    def record_ok(self, seconds: float, mode: str = "") -> None:
        with self._lock:
            self.runs_total += 1
            self.runs_ok += 1
            self.last_ok_at = self._clock()
            self.last_ok_sec = round(seconds, 2)
            self._note_mode(mode)

    def _note_mode(self, mode: str) -> None:
        """Вызывать под замком: считаем пути анализа (PERF-7)."""
        if not mode:
            return
        self.by_mode[mode] = self.by_mode.get(mode, 0) + 1
        self.last_mode = mode

    def record_fail(self, error: str, message: str = "",
                    seconds: float = 0.0, mode: str = "") -> None:
        with self._lock:
            self.runs_total += 1
            self.runs_failed += 1
            self._note_mode(mode)
            self.last_fail_at = self._clock()
            self.last_fail_error = error or "unknown"
            self.last_fail_message = (message or "")[:300]
            self.failures_by_error[self.last_fail_error] = \
                self.failures_by_error.get(self.last_fail_error, 0) + 1

    def snapshot(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            out = {
                "runs_total": self.runs_total,
                "runs_ok": self.runs_ok,
                "runs_failed": self.runs_failed,
                "failures_by_error": dict(self.failures_by_error),
                "by_mode": dict(self.by_mode),
                "last_mode": self.last_mode,
                "last_ok_iso": _iso(self.last_ok_at),
                "last_ok_age_sec": (round(now - self.last_ok_at, 1)
                                    if self.last_ok_at else None),
                "last_ok_duration_sec": self.last_ok_sec,
                "last_fail_iso": _iso(self.last_fail_at),
                "last_fail_age_sec": (round(now - self.last_fail_at, 1)
                                      if self.last_fail_at else None),
                "last_fail_error": self.last_fail_error,
                "last_fail_message": self.last_fail_message,
                "uptime_sec": round(now - self.started_at, 1),
                "note": ("Счётчики живут в памяти процесса и обнуляются его "
                         "перезапуском. Нули означают «с момента старта "
                         "анализ не запускали», а не «анализ не работает»."),
            }
        return out


# ─── сборка ответа ───────────────────────────────────────────────────────


MEANING_BROKEN = (
    "Анализатор сейчас не запустится. Это НЕ значит, что с кодом всё в "
    "порядке: bsl_check_* вернут отказ (answerable: false), а не «замечаний "
    "не найдено». Про качество кода до починки не известно ничего — не "
    "делай вывод и не проверяй код по памяти."
)

MEANING_OK = (
    "Анализатор готов отвечать. Это состояние инструмента, а не суждение о "
    "коде: чтобы узнать про код, вызови bsl_check_code."
)


def health_report(jar_path: str, java_cmd: str = "java",
                  java_opts: str = "", analysis_timeout_sec: int = 120,
                  config_path: str = "", log: AnalysisLog | None = None,
                  java_probe=None, jar_probe=None, now: float | None = None,
                  lsp_state: dict | None = None,
                  warmup_state: dict | None = None,
                  config_report: dict | None = None,
                  config_strict: bool = False) -> dict:
    """
    Полный ответ `bsl_stats`.

    Пробы подменяемы, чтобы собрать в тестах любую комбинацию состояний, не
    ломая настоящий контейнер. Комбинаций всего четыре, и каждая имеет свой
    смысл: нет java, нет jar, нет обоих, всё на месте.
    """
    java_probe = java_probe or (lambda: probe_java(java_cmd))
    jar_probe = jar_probe or (lambda: probe_jar(jar_path))
    log = log or AnalysisLog()
    now = time.time() if now is None else now

    java = java_probe()
    jar = jar_probe()
    ready = bool(java.get("available")) and bool(jar.get("present"))

    reasons = []
    if not java.get("available"):
        reasons.append(f"java недоступна: {java.get('error') or 'причина неизвестна'}")
    if not jar.get("present"):
        reasons.append(f"jar анализатора недоступен: {jar.get('error') or 'причина неизвестна'}")

    # CFG-4. Раньше секция собиралась здесь и отвечала на вопрос «файл на
    # месте?». Вопрос неверный: файл на месте и файл ПРИМЕНЁН — разные
    # вещи, между ними лежит разбор JSON и решение не передавать битый
    # конфиг анализатору. Разбор делает `bsl_config.describe`, и он же —
    # источник для секции `config` в ответах проверок. Два разных ответа
    # про один файл были бы хуже, чем никакого.
    #
    # Ветка со старой проверкой оставлена ради тестов и локальных вызовов,
    # где разбор не передают.
    if config_report is not None:
        config = dict(config_report)
        config["strict"] = config_strict
        # CFG-4.1: `applied: true` не означает «работает». Параметр
        # configurationRoot разрешается только при анализе, и если каталога
        # в смонтированной выгрузке нет, BSL LS об этом не сообщает.
        # Проверяем здесь, чтобы ответ был готов до первого отчёта, а не
        # после того, как отчёту уже поверили.
        root_check = bsl_config.check_mounted_workspace(config)
        if root_check:
            config["configuration_root_check"] = root_check
            if root_check.get("warning"):
                reasons.append(root_check["warning"])
        if config.get("requested") and not config.get("applied"):
            reasons.append(
                config.get("error", "конфигурация диагностик недоступна")
                + (" — при BSL_LS_CONFIG_STRICT=true проверки отвечают "
                   "отказом, а не набором по умолчанию" if config_strict
                   else " — проверки идут набором ПО УМОЛЧАНИЮ")
            )
        # CFG-5 / CFG-6. Применён — не значит «состоит из того, что вы
        # думаете». Сверяем объявленные имена с теми, что действительно
        # есть в этом jar; опечатка перестаёт быть невидимой.
        #
        # Только при `applied`: у непринятого конфига объявлять нечего, а
        # секция про его состав читалась бы как «правила всё-таки в силе».
        if config.get("applied") and jar.get("present"):
            inventory = diagnostics_inventory(jar_path, config)
            if inventory:
                config["diagnostics_check"] = inventory
                if inventory.get("warning"):
                    reasons.append(inventory["warning"])
        # Полные списки имён наружу не отдаём: восемьдесят пять строк в
        # каждом ответе `bsl_stats` — это контекст агента, потраченный на
        # то, что лежит в файле рядом. Числа и имена-подозреваемые есть
        # выше, остальное — в самом bsl-language-server.json.
        for noisy in ("diagnostics_names", "diagnostics_disabled_names"):
            config.pop(noisy, None)
    else:
        config = {"path": config_path, "present": None}
        if config_path:
            config["present"] = Path(config_path).exists()
            if not config["present"]:
                # Не отказ: без своего конфига BSL LS работает на наборе
                # диагностик по умолчанию. Но молчать об этом нельзя —
                # состав замечаний будет не тот, которого ждёт пользователь.
                reasons.append(
                    f"конфигурация диагностик не найдена: {config_path}")
        else:
            config["note"] = ("BSL_LS_CONFIG не задан — анализ идёт на наборе "
                              "диагностик по умолчанию")

    report = {
        "linter_available": ready,
        "answerable": True,
        "degraded": bool(reasons),
        "meaning": MEANING_OK if ready else MEANING_BROKEN,
        "java": java,
        "jar": jar,
        "config": config,
        "analysis": dict(
            log.snapshot(now),
            timeout_sec=analysis_timeout_sec,
            java_opts=java_opts,
        ),
        "checked_at_iso": _iso(now),
        "probe_note": ("Проверка выполнена сейчас, кеша нет: инструмент "
                       "состояния обязан отвечать про «прямо сейчас»."),
    }
    if lsp_state is not None:
        # PERF-7: долгоживущий процесс — состояние, а скрытое состояние
        # делает систему неотлаживаемой. Поэтому оно здесь, рядом со
        # всем остальным, что можно спросить одним вызовом.
        report["lsp"] = lsp_state
        if lsp_state.get("mode") != "off" and not lsp_state.get("running"):
            if lsp_state.get("last_error"):
                # Не отказ: проверки идут прежним путём и дают тот же
                # результат. Но медленнее — и молчать об этом нельзя.
                reasons.append(
                    "быстрый путь (LSP) не работает: "
                    f"{lsp_state['last_error']}; проверки идут прежним "
                    "путём --analyze, это медленнее в десятки раз"
                )

    if warmup_state is not None:
        # PERF-9. Прогрев виден отдельно от процесса, потому что это разные
        # вопросы. `lsp.running=false` отвечает «быстрого пути сейчас нет»;
        # `warmup.state` отвечает, ПОЧЕМУ: ещё греется (подождите), не
        # включён (так настроено) или упал (чинить). Без этого разделения
        # медленный первый вызов и сломанная java выглядят одинаково — а
        # приходят в bsl_stats именно с этим вопросом.
        report["warmup"] = warmup_state
        if warmup_state.get("state") == "warming":
            reasons.append(
                "прогрев JVM ещё идёт: первая проверка подождёт его "
                "окончания. Это разовая цена старта контейнера, не поломка"
            )
        elif warmup_state.get("state") == "failed" and warmup_state.get("error"):
            reasons.append(
                f"прогрев JVM не удался ({warmup_state['error']}); проверки "
                f"идут путём --analyze"
            )

    if reasons:
        report["degradation_reasons"] = reasons
        report["hint"] = (
            "docker compose logs --tail=50 mcp-bsl-checker; проверьте "
            "BSL_LS_JAR и пересоберите образ: "
            "docker compose up -d --build --force-recreate mcp-bsl-checker"
        )
    return report
