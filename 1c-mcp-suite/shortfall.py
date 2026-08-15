"""
A-2. Сверка «вход против выхода» — общий модуль набора.
========================================================

Зачем он есть
─────────────
Заход по `PLAN-5` вскрыл один и тот же узор четыре раза подряд:

  • читатель `.hbk` отдавал 25 страниц из 128 — и «25» выглядело результатом;
  • `walk_workspace` молча пропускает файл, который не разобрался;
  • `walk_workspace_bsl` знает число файлов (оно нужно прогресс-бару), но в
    итоговую строку кладёт только число модулей;
  • два скрипта генерации лок-файлов разошлись и никто этого не увидел.

Ни один случай не выглядел поломкой. Каждый нашёлся только сверкой чисел.
Это не четыре дефекта, а один — отсутствующая дисциплина: **шаг печатает
выход и не печатает вход**.

Образец правильного поведения в проекте уже был — `_warn_shortfall` в
`graph_writer.py`: «отправлено N, записано M, разница вслух». Он вызывался
ровно в трёх местах и больше нигде. Этот модуль — тот же образец, поднятый
в общий код и расширенный причинами отсева.

Что даёт
────────
`Tally` держит три числа на шаг пайплайна: сколько было на входе, сколько
дошло до выхода, сколько и почему отсеяно. И проверяет арифметику:

    вход == выход + сумма отсева

Невязка в этом равенстве — это ровно «потери, о которых никто не знает».
Она печатается как WARNING даже тогда, когда все причины отсева законны.

Правила именования причин: причина — короткая строка на русском, годная
для лога («битый XML», «вне схемы путей»). `alarm=True` помечает причину,
которая законной не бывает никогда, — такая всплывает в WARNING, даже если
арифметика сошлась.

Зависимостей нет намеренно: модуль кладётся и в образ `Dockerfile.python`
(индексер метаданных), и в `Dockerfile.embeddings` (индексатор справки), и
должен запускаться в тестах без единого пакета сверх стандартной библиотеки.
"""

from __future__ import annotations

import logging
from collections import Counter, OrderedDict
from typing import Iterable, Optional

__all__ = ["Tally", "TallyBook", "warn_shortfall", "format_shortfall"]

_log = logging.getLogger("shortfall")

# Сколько примеров хранить на каждую причину отсева. Три — чтобы в логе
# было за что зацепиться руками, и при этом строка оставалась читаемой.
MAX_EXAMPLES = 3


# ─── Простая форма: отправлено против записанного ────────────────────────


def format_shortfall(what: str, sent: int, written: int, unit: str = "строк") -> str:
    """Текст сообщения о недостаче. Отдельно от логирования — для тестов."""
    return (
        f"{what}: записано {written} из {sent} — "
        f"{sent - written} {unit} не дошли и пропущены молча"
    )


def warn_shortfall(
    what: str,
    sent: int,
    written: int,
    *,
    log: Optional[logging.Logger] = None,
    hint: str = "",
    unit: str = "строк",
) -> int:
    """
    Сообщает о недостаче «отправлено против записанного». Возвращает размер
    недостачи (0, если её нет).

    Прямой наследник `graph_writer._warn_shortfall`. Тот остался на месте как
    тонкая обёртка — вызовов у него три, и переписывать их ради переезда
    смысла нет, а вот второй копии правила быть не должно.
    """
    if written >= sent:
        return 0
    message = format_shortfall(what, sent, written, unit)
    if hint:
        message = f"{message}. {hint}"
    (log or _log).warning("%s", message)
    return sent - written


# ─── Полная форма: вход, выход и причины отсева ──────────────────────────


class Tally:
    """
    Счётчик одного шага пайплайна.

    Использование:

        t = Tally("разбор XML", unit="файл", log=log)
        for path in paths:
            t.see()
            obj = parse(path)
            if obj is None:
                t.drop("битый XML", example=path.name, alarm=True)
                continue
            t.keep()
        t.report()

    `see()` вызывается на КАЖДОМ входном элементе, `keep()` — на дошедшем до
    выхода, `drop()` — на отсеянном с указанием причины. Если сумма не
    сходится, `report()` скажет об этом отдельной строкой: значит, в шаге
    есть путь выхода, о котором автор счётчика не знал. Именно такие пути и
    съедали данные во всех разобранных случаях.
    """

    def __init__(
        self,
        what: str,
        *,
        unit: str = "шт",
        log: Optional[logging.Logger] = None,
        max_examples: int = MAX_EXAMPLES,
        min_keep_ratio: Optional[float] = None,
    ) -> None:
        self.what = what
        self.unit = unit
        self.log = log or _log
        self.max_examples = max_examples
        # Порог доли дошедших, ниже которого шаг считается подозрительным.
        # Нужен там, где отсев законен и массов (картинки в контейнере
        # справки), но обвал всё равно надо заметить.
        self.min_keep_ratio = min_keep_ratio

        self.seen = 0
        self.kept = 0
        self.dropped: Counter = Counter()
        self.alarm_reasons: set[str] = set()
        self._examples: "OrderedDict[str, list[str]]" = OrderedDict()

    # ─ Учёт ─

    def see(self, n: int = 1) -> None:
        self.seen += n

    def keep(self, n: int = 1) -> None:
        self.kept += n

    def drop(
        self,
        reason: str,
        example: Optional[object] = None,
        n: int = 1,
        alarm: bool = False,
    ) -> None:
        self.dropped[reason] += n
        if alarm:
            self.alarm_reasons.add(reason)
        if example is not None:
            bucket = self._examples.setdefault(reason, [])
            if len(bucket) < self.max_examples:
                bucket.append(str(example))

    # ─ Производные величины ─

    @property
    def lost(self) -> int:
        """Вход минус выход. Сколько всего не дошло, по любой причине."""
        return self.seen - self.kept

    @property
    def explained(self) -> int:
        """Сколько из потерь объяснено причинами."""
        return sum(self.dropped.values())

    @property
    def unexplained(self) -> int:
        """
        Потери без причины. Главное число этого модуля.

        Ноль означает, что автор шага перечислил все выходы. Не ноль —
        что данные уходят по пути, которого в коде счётчика нет.
        """
        return self.lost - self.explained

    @property
    def keep_ratio(self) -> float:
        return (self.kept / self.seen) if self.seen else 0.0

    @property
    def ok(self) -> bool:
        """True, если шагу нечего предъявить."""
        if self.unexplained != 0:
            return False
        if self.alarm_reasons & set(self.dropped):
            return False
        if self.seen and not self.kept:
            return False
        if self.min_keep_ratio is not None and self.seen and self.keep_ratio < self.min_keep_ratio:
            return False
        return True

    # ─ Вывод ─

    def line(self) -> str:
        """Главная строка: вход, выход, отсев."""
        return (
            f"{self.what}: вход {self.seen} {self.unit} → "
            f"выход {self.kept}, отсев {self.lost}"
        )

    def reasons_line(self) -> str:
        """Разбивка отсева по причинам с примерами. Пустая строка, если отсева нет."""
        if not self.dropped:
            return ""
        parts = []
        for reason, count in self.dropped.most_common():
            piece = f"{reason} {count}"
            examples = self._examples.get(reason)
            if examples:
                piece += " (напр. " + ", ".join(examples) + ")"
            parts.append(piece)
        return "  причины отсева: " + "; ".join(parts)

    def problems(self) -> list[str]:
        """Список претензий к шагу. Пустой — значит всё сошлось."""
        out: list[str] = []
        if self.unexplained > 0:
            out.append(
                f"{self.unexplained} {self.unit} потеряно без объяснения "
                f"(вход {self.seen} ≠ выход {self.kept} + отсев {self.explained}) — "
                f"в шаге есть выход, который никто не считает"
            )
        elif self.unexplained < 0:
            out.append(
                f"счётчики не сходятся: отсев {self.explained} больше "
                f"разницы {self.lost} — see()/keep()/drop() расставлены неверно"
            )
        if self.seen and not self.kept:
            out.append(f"на выходе ноль при входе {self.seen} — это отказ, а не результат")
        for reason in sorted(self.alarm_reasons & set(self.dropped)):
            out.append(f"«{reason}»: {self.dropped[reason]} — причина законной не бывает")
        if (
            self.min_keep_ratio is not None
            and self.seen
            and self.kept
            and self.keep_ratio < self.min_keep_ratio
        ):
            out.append(
                f"дошло {self.keep_ratio:.1%} при ожидаемых "
                f"{self.min_keep_ratio:.0%} и выше"
            )
        return out

    def report(self, log: Optional[logging.Logger] = None, hint: str = "") -> bool:
        """
        Печатает итог шага. Возвращает True, если претензий нет.

        Главная строка идёт в INFO всегда — вход должен быть виден и тогда,
        когда всё хорошо, иначе сравнивать будет не с чем. Претензии идут в
        WARNING.
        """
        out = log or self.log
        out.info("  %s", self.line())
        reasons = self.reasons_line()
        if reasons:
            out.info("%s", reasons)
        problems = self.problems()
        for problem in problems:
            out.warning("  ⚠ %s: %s", self.what, problem)
        if problems and hint:
            out.warning("    %s", hint)
        return not problems

    def to_dict(self) -> dict:
        return {
            "what": self.what,
            "seen": self.seen,
            "kept": self.kept,
            "lost": self.lost,
            "unexplained": self.unexplained,
            "dropped": dict(self.dropped),
            "ok": self.ok,
        }


class TallyBook:
    """
    Набор счётчиков за один прогон и сводка по ним.

    Нужен затем же, зачем сами счётчики: отдельный шаг может отчитаться
    честно, а прогон в целом — потерять треть данных на трёх шагах по
    одиннадцать процентов. Сводка в конце ставит все пары чисел рядом.
    """

    def __init__(self, log: Optional[logging.Logger] = None) -> None:
        self.log = log or _log
        self.stages: list[Tally] = []

    def stage(self, what: str, **kwargs) -> Tally:
        kwargs.setdefault("log", self.log)
        t = Tally(what, **kwargs)
        self.stages.append(t)
        return t

    def add(self, tally: Tally) -> Tally:
        self.stages.append(tally)
        return tally

    @property
    def bad(self) -> list[Tally]:
        return [t for t in self.stages if not t.ok]

    def report(self, log: Optional[logging.Logger] = None, title: str = "Сводка потерь") -> int:
        """
        Печатает сводку. Возвращает число шагов с претензиями.

        Ноль шагов в книге — это тоже сообщение: значит, прогон прошёл без
        единой сверки, и «всё хорошо» сказать не о чем.
        """
        out = log or self.log
        if not self.stages:
            out.info("%s: сверок не было", title)
            return 0
        out.info("%s (%d шагов):", title, len(self.stages))
        for t in self.stages:
            mark = "✓" if t.ok else "⚠"
            out.info("  %s %s: вход %d → выход %d (отсев %d)",
                     mark, t.what, t.seen, t.kept, t.lost)
        bad = self.bad
        if bad:
            out.warning("⚠ шагов с потерями: %d из %d — %s",
                        len(bad), len(self.stages),
                        ", ".join(t.what for t in bad))
        return len(bad)

    def to_dict(self) -> dict:
        return {
            "stages": [t.to_dict() for t in self.stages],
            "stages_with_problems": len(self.bad),
        }


def report_all(tallies: Iterable[Tally], log: Optional[logging.Logger] = None) -> int:
    """Отчёт по произвольной последовательности счётчиков. Возвращает число плохих."""
    book = TallyBook(log)
    for t in tallies:
        book.add(t)
    return book.report(log)
