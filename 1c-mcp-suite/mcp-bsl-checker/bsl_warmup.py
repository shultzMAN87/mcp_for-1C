"""
PERF-9. Прогрев JVM при старте контейнера.
===========================================

Что было
────────
`PERF-7` сделал JVM долгоживущей — вместо запуска на каждый вызов процесс
поднимается один раз. Но поднимается он **лениво**, на первой проверке
агента. В последнем отчёте это видно строкой: `bsl-001` — 14 секунд,
`bsl-002` — 224 мс.

Четырнадцать секунд платит первый пришедший, и платит их молча.

Почему это не «просто медленно один раз»
────────────────────────────────────────
Тот же довод, что у `PERF-6` для справки и у `FIX-14` до неё: четырнадцать
секунд тишины — достаточно, чтобы агент счёл инструмент неотвечающим и
пошёл отвечать по памяти. Причём приходит он в `bsl_check_code` сразу после
правки кода, то есть в момент, когда ответ по памяти особенно вреден —
именно проверку он и заменяет.

Разница между «медленно» и «не отвечает» определяется не секундомером, а
терпением вызывающего. У агента оно короче человеческого.

Из чего складывается прогрев
────────────────────────────
Двух шагов, а не одного:

  1. `client.start()` — запуск java, загрузка jar 43 МБ, LSP `initialize`;
  2. одна проверка синтетического файла.

Второй шаг не «на всякий случай». Это тот же урок, что `load_dense()` в
справке: там после загрузки весов делается холостой forward, потому что
внутри torch есть ленивая инициализация, и первый настоящий проход стоит
заметно дороже следующих. У BSL LS та же природа: `initialize` не создаёт
парсер и не прогревает JIT — это делает первый разбираемый документ.
Греть только старт значило бы перенести половину цены, а не убрать её.

Синтетический файл намеренно крошечный и намеренно с ошибкой в нём НЕТ:
задача — пройти путь целиком, а не получить замечания.

Чего этот модуль НЕ делает
──────────────────────────
**Не делает прогрев условием работы.** Если java нет, jar битый или
процесс не поднялся — прогрев пишет строку в лог и заканчивается. Сервер
стартует, инструменты отвечают, проверки идут прежним путём `--analyze`.
Ровно так устроен прогрев справки, и по той же причине: ускорение не имеет
права стать новой точкой отказа.

**Не ускоряет `bsl_check_directory`.** Тот и так идёт минуты по пути
`--analyze`, старт JVM в нём теряется (`PERF-7`, раздел про границы).

**Не заводит второй замок.** Ждать чужой загрузки умеет сам
`BslLspClient.start()` — он под `self._lock` и проверяет `running()` уже
внутри замка. Запрос, пришедший на пятой секунде прогрева, встанет на этом
замке и получит готовый процесс, а не запустит вторую JVM. Это то самое
различие «флаг против замка», из-за которого `PERF-6`, `PERF-6.1` и
`AUDIT-3` чинили три разных места.

Выключается: `BSL_WARMUP=false`.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

__all__ = ["Warmup", "WARMUP_ENABLED", "WARMUP_SNIPPET", "say"]


WARMUP_ENABLED = os.environ.get("BSL_WARMUP", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

# Крошечный корректный модуль. Задача — пройти путь целиком: разбор,
# создание парсера, первый прогон диагностик. Замечаний тут быть не должно,
# иначе в логе старта появятся строки, которые никого не касаются.
WARMUP_SNIPPET = (
    "Процедура Прогрев()\n"
    "    Сообщить(\"ок\");\n"
    "КонецПроцедуры\n"
)


def say(message: str, err: bool = False) -> None:
    """
    Печать, переживающая узкую консоль.

    `FAIL-2`: диагностический вывод со знаками ✓ и ⚠ ронял вызов на
    Windows — у дочернего процесса cp1251, и `$OutputEncoding` на это не
    влияет. Прогрев печатает при каждом старте контейнера, то есть чаще
    всего остального.
    """
    stream = sys.stderr if err else sys.stdout
    try:
        stream.write(message + "\n")
        stream.flush()
    except UnicodeEncodeError:
        stream.write(message.encode("ascii", "replace").decode("ascii") + "\n")
        stream.flush()


class Warmup:
    """
    Прогрев клиента BSL LS. Состояние видно снаружи — через `bsl_stats`.

    Названо и устроено по образцу `model_warmup.Warmup` у справки. Общего
    модуля не заведено намеренно: там реестр из трёх разнородных загрузок с
    ленивым доступом, здесь — один клиент, который уже умеет себя
    поднимать. Общий модуль пришлось бы делать абстрактнее обоих случаев,
    и он описывал бы не то, что происходит.
    """

    def __init__(self, client, clock=time.monotonic, snippet: str = WARMUP_SNIPPET):
        self._client = client
        self._clock = clock
        self._snippet = snippet
        self._thread = None
        self.started_at = None
        self.finished_at = None
        self.seconds = None
        self.error = ""
        self.state_name = "idle"

    # ─ сам прогрев ─

    def run(self, out=say) -> str:
        """
        Поднять процесс и один раз пройти путь проверки. Не бросает наружу.

        Возвращает итог строкой: `ready` | `failed` | `disabled`.
        """
        if not WARMUP_ENABLED:
            self.state_name = "disabled"
            out("[warmup] bsl: выключен (BSL_WARMUP=false) — JVM поднимется "
                "на первой проверке, первый вызов будет медленным")
            return self.state_name

        self.started_at = self._clock()
        self.state_name = "warming"
        out("[warmup] bsl: поднимаю BSL Language Server...")
        try:
            self._client.start()
            out(f"[warmup] bsl: процесс поднят за "
                f"{round(self._clock() - self.started_at, 2)} с, "
                f"прогоняю пробный файл")
            self._probe()
        except Exception as exc:  # noqa: BLE001 — прогрев не роняет сервер
            self.error = f"{type(exc).__name__}: {exc}"
            self.seconds = round(self._clock() - self.started_at, 2)
            self.finished_at = self._clock()
            self.state_name = "failed"
            out(f"[warmup] bsl: не поднялся ({self.error}) — проверки пойдут "
                f"путём --analyze, медленнее, но с тем же результатом",
                err=True)
            return self.state_name

        self.finished_at = self._clock()
        self.seconds = round(self.finished_at - self.started_at, 2)
        self.state_name = "ready"
        out(f"[warmup] bsl: готово за {self.seconds} с — первая проверка "
            f"агента больше не платит за старт JVM")
        return self.state_name

    def _probe(self) -> None:
        """
        Одна настоящая проверка. Файл на диске, а не выдуманный путь.

        Клиенту хватило бы и текста в памяти, но BSL LS вправе сходить за
        файлом по URI, и выдуманный путь дал бы отказ, неотличимый от «не
        поднялся». Цена настоящего файла — несколько байт во временном
        каталоге.
        """
        with tempfile.TemporaryDirectory(prefix="bsl-warmup-") as tmp:
            path = Path(tmp) / "Прогрев.bsl"
            path.write_text(self._snippet, encoding="utf-8")
            self._client.diagnostics(str(path), text=self._snippet)

    def start_background(self, out=say) -> None:
        """
        Прогрев в фоне: старт контейнера не должен его ждать.

        Порт открывается сразу, `/healthz` отвечает сразу. Запрос,
        пришедший в середине прогрева, встанет на замке внутри клиента и
        дождётся готового процесса — второй JVM не появится.
        """
        self._thread = threading.Thread(
            target=self.run, args=(out,), name="bsl-warmup", daemon=True)
        self._thread.start()

    # ─ наблюдаемость ─

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def state(self) -> dict:
        """
        Что показывает `bsl_stats`.

        `OBS-2` в чистом виде: к статистике приходят, когда что-то не так, и
        «прогрев ещё идёт» обязано отличаться от «прогрев упал». Без этого
        медленный первый вызов и сломанная java выглядят одинаково.
        """
        return {
            "state": ("warming" if self.running else self.state_name),
            "seconds": self.seconds,
            "error": self.error,
            "enabled": WARMUP_ENABLED,
            "note": (
                "JVM поднимается при старте контейнера, а не на первой "
                "проверке (PERF-9). state=warming — первая проверка "
                "подождёт прогрева; state=failed — прогрев не удался, "
                "проверки идут путём --analyze, это медленнее, но "
                "результат тот же."
            ),
        }
