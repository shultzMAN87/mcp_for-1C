"""
ENV-1. Значение строки `.env` — по тем же правилам, что у docker compose.

Зачем
─────
README показывает `.env` с комментарием в конце строки:

    WORKSPACE_DIR=D:/work/ubd_for_ai     # корень репозитория, НЕ src

docker compose такой комментарий отрезает, и стек работал. А семь
скриптов на хосте читали `.env` каждый своим разбором и брали значение
целиком, вместе с `# корень репозитория…`. `check_prereqs.py` на чистом
подъёме стека 4 ноября сказал «каталог не найден» про существующий
каталог — путь для него заканчивался комментарием.

Правила (как в compose):
  • значение в кавычках — то, что между кавычками; после них — игнор;
  • без кавычек — до первого `#`, перед которым пробел или табуляция;
    `#` внутри значения (`pa#ss`) — часть значения;
  • пробелы по краям срезаются.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Защита потоков, как у всех скриптов в scripts/ (сторож — tests_host_scripts):
# модуль сам ничего не печатает, но импортируется скриптами, которые печатают.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_INLINE_COMMENT = re.compile(r"\s#")


def clean_env_value(raw: str) -> str:
    """Значение из правой части `KEY=...`."""
    v = raw.strip()
    if v.startswith("#"):          # `KEY=   # только комментарий` — пусто
        return ""
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        if end > 0:
            return v[1:end]
        return v
    m = _INLINE_COMMENT.search(v)
    if m:
        v = v[:m.start()]
    return v.strip()


def parse_env_text(text: str) -> dict[str, str]:
    """Словарь из текста `.env`: пустые строки и `# …` пропускаются."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out[key] = clean_env_value(value)
    return out


def parse_env_file(path: Path) -> dict[str, str]:
    """Словарь из файла `.env`; нет файла — пустой словарь."""
    path = Path(path)
    if not path.is_file():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8-sig"))
