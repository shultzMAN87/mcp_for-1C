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

        tail = f" — {extra}" if extra else ""
        self.log.log(
            self.level, "  [%s] %s, %.0f %s/с, прошло %s%s",
            self.label, head, rate, self.unit, human_sec(elapsed), tail,
        )
        self._t_last = now
        self._count_last = self.count
        self._lines += 1
