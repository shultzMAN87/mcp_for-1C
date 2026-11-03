"""
Прогресс-лог долгих этапов индексации — задача PERF-5 (Заход 4).
==================================================================

Зачем отдельный модуль
----------------------
`build_call_graph`, запись слоя 1 и разбор BSL на боевой конфигурации идут
десятки минут и не пишут в лог ничего до самого конца. На тридцати секундах
это незаметно; на получасе снаружи невозможно отличить работающий процесс от
зависшего, и диагностика сводится к `docker stats` и гаданию.

Требования, из которых вырос интерфейс:

  • Лог не должен зависеть от того, знаем ли мы общее число единиц заранее.
    Модули при разборе BSL мы знаем (список файлов собран), а число итераций
    фикс-пойнта — нет.

  • Частота должна ограничиваться и по количеству, и по времени. Только по
    количеству — на медленном этапе строки идут раз в минуту, на быстром
    заливают лог. Только по времени — на очень быстром этапе таймер
    проверяется чаще, чем делается работа.

  • Накладные расходы на единицу — один инкремент и одно сравнение. Вызов
    `time.monotonic()` на каждом шаге при 723 тысячах callsite'ов заметен,
    поэтому время проверяется не чаще, чем раз в `check_every` шагов.

Имя файла `progress_log.py`, а не `progress.py`: в PyPI есть пакет с таким
именем, а Dockerfile.python кладёт все модули плоско в `/app`. Совпадение
имён рано или поздно выстрелило бы неочевидной ошибкой импорта.

Использование
-------------
    prog = ProgressLogger(log, "разбор BSL", total=len(files))
    for f in files:
        ...
        prog.step()
    prog.done()

или без общего числа:

    prog = ProgressLogger(log, "резолв", every_items=2000)
    ...
    prog.done(extra="разрешено 428172")
"""
from __future__ import annotations

import logging
import time
from typing import Optional


def human_bytes(n: float) -> str:
    """Байты → человекочитаемое. Для логов размеров выгрузки."""
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024.0 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} {unit}"
        n /= 1024.0
    return f"{n:.1f} ТБ"


def human_sec(sec: float) -> str:
    """Секунды → человекочитаемое (с, мин, ч)."""
    if sec < 90:
        return f"{sec:.1f} с"
    if sec < 5400:
        return f"{sec / 60:.1f} мин"
    return f"{sec / 3600:.1f} ч"


class ProgressLogger:
    """
    Периодический прогресс-лог.

    Параметры:
      log         — logging.Logger, куда писать.
      label       — что именно идёт («разбор BSL», «рёбра HAS_ATTRIBUTE»).
      total       — общее число единиц, если известно. Даёт проценты и ETA.
      every_items — писать не чаще, чем раз в столько единиц (0 — не
                    ограничивать по количеству).
      every_sec   — писать не чаще, чем раз в столько секунд.
      check_every — как часто сверяться с часами (в единицах). Защита от
                    накладных расходов на очень частых шагах.
      level       — уровень логирования строк прогресса.
      unit        — название единицы для строки лога.
    """

    def __init__(
        self,
        log: logging.Logger,
        label: str,
        total: Optional[int] = None,
        every_items: int = 0,
        every_sec: float = 15.0,
        check_every: int = 200,
        level: int = logging.INFO,
        unit: str = "шт",
    ):
        self.log = log
        self.label = label
        self.total = total if (total is None or total > 0) else None
        self.every_items = max(0, int(every_items))
        self.every_sec = float(every_sec)
        self.check_every = max(1, int(check_every))
        self.level = level
        self.unit = unit

        self.count = 0
        self._t_start = time.monotonic()
        self._t_last = self._t_start
        self._count_last = 0
        self._since_check = 0
        self._lines = 0

    # ─── Основной API ────────────────────────────────────────────────

    def step(self, n: int = 1, extra: str = "") -> None:
        """Отметить n выполненных единиц; при необходимости — написать строку."""
        self.count += n
        self._since_check += n

        if self.every_items and (self.count - self._count_last) >= self.every_items:
            self._emit(extra)
            return

        if self._since_check < self.check_every:
            return
        self._since_check = 0

        if (time.monotonic() - self._t_last) >= self.every_sec:
            self._emit(extra)

    def note(self, message: str) -> None:
        """Разовая строка в том же префиксе — для событий вне счётчика."""
        self.log.log(self.level, "  [%s] %s", self.label, message)

    def done(self, extra: str = "") -> float:
        """
        Финальная строка. Возвращает затраченное время в секундах.

        Пишется всегда, даже если промежуточных строк не было: на коротком
        прогоне это единственный источник тайминга этапа.
        """
        elapsed = time.monotonic() - self._t_start
        rate = self.count / elapsed if elapsed > 0 else 0.0
        tail = f" — {extra}" if extra else ""
        self.log.log(
            self.level,
            "  [%s] готово: %d %s за %s (%.0f %s/с)%s",
            self.label, self.count, self.unit, human_sec(elapsed),
            rate, self.unit, tail,
        )
        return elapsed

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t_start

    # ─── Внутреннее ──────────────────────────────────────────────────

    def _emit(self, extra: str = "") -> None:
        now = time.monotonic()
        elapsed = now - self._t_start
        rate = self.count / elapsed if elapsed > 0 else 0.0

        if self.total:
            pct = 100.0 * self.count / self.total
            if rate > 0:
                eta = (self.total - self.count) / rate
                eta_s = f", осталось ~{human_sec(eta)}"
            else:
                eta_s = ""
            head = f"{self.count}/{self.total} ({pct:.0f}%){eta_s}"
        else:
            head = f"{self.count} {self.unit}"

        beat(now)
        tail = f" — {extra}" if extra else ""
        gc_s = GC_METER.share_text(now)
        if gc_s:
            tail = f"{tail}; {gc_s}" if tail else f" — {gc_s}"
        self.log.log(
            self.level, "  [%s] %s, %.0f %s/с, прошло %s%s",
            self.label, head, rate, self.unit, human_sec(elapsed), tail,
        )
        self._t_last = now
        self._count_last = self.count
        self._lines += 1


class GcMeter:
    """
    PERF-13. Сколько времени уходит на сборщик мусора Python.

    Гипотеза: резолв замедляется в пять раз по ходу одной итерации (701 →
    133 проц/с при 98 % CPU и свободной памяти), потому что сборщик раз за
    разом пересматривает миллионы объектов — разобранные модули плюс
    копящиеся узлы и рёбра. Проверяется не рассуждением, а числом: доля
    времени сборщика пишется в каждую строку прогресса, пока счётчик
    включён (`start()`), — «gc 41 %» с прошлой строки.

    Цена — два вызова `perf_counter()` на сборку; сборок тысячи, не
    миллионы.
    """

    def __init__(self):
        self.active = False
        self.total = 0.0
        self.collections = [0, 0, 0]
        self._t0: Optional[float] = None
        self._mark_wall = 0.0
        self._mark_gc = 0.0

    def _cb(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._t0 = time.perf_counter()
        elif self._t0 is not None:
            self.total += time.perf_counter() - self._t0
            self._t0 = None
            gen = info.get("generation", 0)
            if 0 <= gen < 3:
                self.collections[gen] += 1

    def start(self) -> None:
        import gc
        if not self.active:
            gc.callbacks.append(self._cb)
            self.active = True
        self._mark_wall = time.monotonic()
        self._mark_gc = self.total

    def stop(self) -> None:
        import gc
        if self.active:
            try:
                gc.callbacks.remove(self._cb)
            except ValueError:
                pass
            self.active = False

    def share_text(self, now: Optional[float] = None) -> str:
        """«gc 41 %» — доля с прошлого вызова; пусто, если счётчик выключен."""
        if not self.active:
            return ""
        now = time.monotonic() if now is None else now
        wall = now - self._mark_wall
        spent = self.total - self._mark_gc
        self._mark_wall, self._mark_gc = now, self.total
        if wall <= 0:
            return ""
        return f"gc {100.0 * spent / wall:.0f}%"

    def summary(self) -> str:
        return (f"сборщик мусора: {human_sec(self.total)} всего, сборок "
                f"{self.collections[0]}/{self.collections[1]}/{self.collections[2]} "
                f"(поколения 0/1/2)")


# Один на процесс: gc.callbacks — глобальный список интерпретатора.
GC_METER = GcMeter()


# ─── Пульс долгих прогонов (FIX-39) ──────────────────────────────────────
#
# Полная индексация держит аренду графа часами. Раньше аренда бралась сразу
# на восемь часов: упавший индексер запирал граф на всё это время, а чтобы
# этого не было, у всех индексеров было одно имя владельца — и второй
# индексер принимал чужую аренду за свою и писал в граф одновременно с
# первым. Теперь аренда короткая и продлевается, пока прогон жив. Продлевать
# удобнее всего здесь: строка прогресса выводится каждые десятки секунд в
# любой долгой фазе (разбор, резолв, запись), и всегда в главном процессе —
# дочерние процессы PERF-15 прогресс не ведут, а пульс проверяет pid.

_BEATS: list[dict] = []


def add_heartbeat(fn, every_sec: float = 60.0):
    """Звать `fn()` не чаще раза в `every_sec` при выводе строк прогресса.
    Возвращает функцию, которая пульс снимает."""
    import os
    h = {"fn": fn, "every": every_sec, "last": time.monotonic(), "pid": os.getpid()}
    _BEATS.append(h)

    def remove() -> None:
        if h in _BEATS:
            _BEATS.remove(h)
    return remove


def beat(now: Optional[float] = None) -> None:
    import os
    now = time.monotonic() if now is None else now
    pid = os.getpid()
    for h in list(_BEATS):
        if h["pid"] != pid or now - h["last"] < h["every"]:
            continue
        h["last"] = now
        try:
            h["fn"]()
        except Exception as e:  # noqa: BLE001 — пульс не роняет прогон
            logging.getLogger("progress").warning("пульс: %s: %s", type(e).__name__, e)
