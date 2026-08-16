#!/usr/bin/env python3
"""
Хост-обёртка над eval-runner (задача 3.4).

Что делает:
  1. Читает MCP_SHARED_SECRET из .env (и .env.local, если есть).
  2. Запускает контейнер `eval-runner` через docker-compose-профиль `evals`,
     передавая endpoint=http://mcp-platform-help:8003/mcp (docker-DNS).
  3. Отчёты падают в ./evals/reports/ через bind-volume, указанный в compose.

Использование:
    python3 scripts/eval.py                        # дефолт — через docker
    python3 scripts/eval.py --dataset evals/datasets/my.jsonl
    python3 scripts/eval.py --limit 3              # первые 3 примера
    python3 scripts/eval.py --local                # запуск на хосте, не в docker
    python3 scripts/eval.py --no-deps              # не поднимать зависимости

Про `--no-deps`. `docker compose run` по умолчанию поднимает зависимости
сервиса. Для обычного прогона это удобно, а для проверки поведения при
остановленном сервисе — губительно: compose заведёт его обратно, и датасет
измерит здоровый стенд, думая, что меряет больной.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# B-3/FAIL-2: печать не должна ронять то, что диагностирует. Консоль
# PowerShell бывает cp1251, а в выводе стоит «←» — на нём скрипт падал бы с
# UnicodeEncodeError вместо того, чтобы сказать, сошлась база или нет.
#
# errors=replace, а не encoding=utf-8: подмена кодировки дала бы кракозябры,
# а замена — всего лишь «?» вместо стрелки.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass




ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = "evals/datasets/platform_help.jsonl"
DEFAULT_OUT = "evals/reports"

# Один прогон — один сервер: eval-runner держит одну MCP-сессию на весь
# датасет. Раньше адрес был захардкожен в двух местах, и после появления
# пятого сервера (v8std, STD-4) его пришлось бы помнить наизусть.
# Значение: (адрес в docker-сети, адрес с хоста).
SERVERS = {
    "help":  ("http://mcp-platform-help:8003/mcp", "http://localhost:8003/mcp"),
    "v8std": ("http://v8std-mcp:8765/mcp",         "http://localhost:8765/mcp"),
    "bsl":   ("http://mcp-bsl-checker:8002/mcp",   "http://localhost:8002/mcp"),
    # EVAL-1. Два сервера, которые до Захода 4 не измерялись ничем. Проект
    # мерил покрытие резолвера до сотых процента и нигде не мерил, отвечают
    # ли инструменты по метаданным правильно. Все дефекты FIX-14/16/17
    # нашлись руками — потому что автоматической проверки не существовало.
    "meta":  ("http://mcp-metadata-graph:8001/mcp", "http://localhost:8001/mcp"),
    "query": ("http://mcp-query-builder:8009/mcp",  "http://localhost:8009/mcp"),
}
# Датасет → сервер по умолчанию, чтобы не указывать --server каждый раз.
DATASET_SERVER = {
    "platform_help.jsonl": "help",
    "v8std.jsonl": "v8std",
    "bsl_checker.jsonl": "bsl",
    "metadata_graph.jsonl": "meta",
    "query_builder.jsonl": "query",
}


def load_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        result[k.strip()] = v
    return result


def load_env_chain() -> dict[str, str]:
    env = {}
    for fname in (".env", ".env.local"):
        env.update(load_env_file(ROOT / fname))
    return env


def run_docker(args: argparse.Namespace) -> int:
    if not shutil.which("docker"):
        print("ERROR: docker not found in PATH. Установите Docker или используйте --local.",
              file=sys.stderr)
        return 2

    compose_file = ROOT / "docker-compose.yml"
    if not compose_file.exists():
        print(f"ERROR: docker-compose.yml не найден по пути {compose_file}",
              file=sys.stderr)
        return 2

    env = os.environ.copy()
    file_env = load_env_chain()
    for k in ("MCP_SHARED_SECRET",):
        if k in file_env and k not in env:
            env[k] = file_env[k]

    # A-8, вторая половина. `docker compose run` поднимает зависимости
    # сервиса — и это не мелочь, а свойство измерения менять измеряемое.
    # Проверено на приёмке 15 августа: `docker compose stop qdrant` +
    # прогон датасета дал зелёные 15/15, потому что compose услужливо
    # завёл qdrant обратно («✔ Container qdrant Healthy 5.8s») ещё до
    # первого запроса. Ловушка на отказ отработала на живом стенде и
    # ничего не проверила.
    #
    # Тот же узор, что снятая зависимость `mcp-platform-help` →
    # `help-indexer`: там измерение уничтожало измеряемое, здесь —
    # чинит его. Оба раза результат выглядит достоверным.
    run_flags = ["run", "--rm"]
    if args.no_deps:
        run_flags.append("--no-deps")

    cmd = [
        "docker", "compose",
        "-f", str(compose_file),
        "--profile", "evals",
        *run_flags,
        "eval-runner",
        "python", "/app/run_eval.py",
        "--dataset", _in_container_path(args.dataset),
        "--out", _in_container_path(args.out),
        "--endpoint", args.endpoint,
    ]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.init_timeout:
        cmd += ["--init-timeout", str(args.init_timeout)]
    if args.call_timeout:
        cmd += ["--call-timeout", str(args.call_timeout)]

    print("[eval] $ " + " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd, env=env, cwd=str(ROOT))


def _in_container_path(host_path: str) -> str:
    """
    В контейнере eval-runner папка ./evals смонтирована как /app/evals.
    """
    p = host_path.strip()
    if p.startswith("/"):
        return p
    parts = Path(p).parts
    if parts and parts[0] == "evals":
        return "/app/" + "/".join(parts)
    return "/app/evals/" + p


def run_local(args: argparse.Namespace) -> int:
    """
    Запуск на хосте без docker — для отладки. Требует установленного
    mcp[cli] на хосте и доступного http://localhost:8003/mcp.
    """
    runner = ROOT / "evals" / "runner" / "run_eval.py"
    if not runner.exists():
        print(f"ERROR: {runner} не найден", file=sys.stderr)
        return 2

    env = os.environ.copy()
    file_env = load_env_chain()
    for k, v in file_env.items():
        env.setdefault(k, v)

    cmd = [
        sys.executable, str(runner),
        "--dataset", str(ROOT / args.dataset),
        "--out", str(ROOT / args.out),
        "--endpoint", args.endpoint,
    ]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.init_timeout:
        cmd += ["--init-timeout", str(args.init_timeout)]
    if args.call_timeout:
        cmd += ["--call-timeout", str(args.call_timeout)]

    print("[eval] $ " + " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd, env=env, cwd=str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Хост-обёртка для eval-runner (3.4).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--dataset", default=DEFAULT_DATASET,
                    help="Путь к .jsonl датасету (относительно корня проекта).")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="Папка для отчётов.")
    ap.add_argument("--server", choices=sorted(SERVERS), default=None,
                    help="К какому серверу идти. По умолчанию определяется "
                         "по имени датасета (см. DATASET_SERVER).")
    ap.add_argument("--endpoint", default=None,
                    help="Явный MCP-эндпоинт. Перебивает --server.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Прогнать только первые N примеров (0 = все).")
    ap.add_argument("--init-timeout", type=float, default=None,
                    help="SSE initialize timeout, сек.")
    ap.add_argument("--call-timeout", type=float, default=None,
                    help="Per-tool call_tool timeout, сек.")
    ap.add_argument("--local", action="store_true",
                    help="Запускать runner на хосте, а не через docker compose.")
    ap.add_argument("--no-deps", action="store_true",
                    help="Не поднимать зависимости eval-runner. Обязателен, "
                         "когда проверяете поведение при остановленном "
                         "сервисе: без него compose заведёт его обратно.")
    args = ap.parse_args()

    if args.endpoint is None:
        server = args.server or DATASET_SERVER.get(Path(args.dataset).name, "help")
        in_docker, on_host = SERVERS[server]
        args.endpoint = on_host if args.local else in_docker
        print(f"[eval] сервер: {server} -> {args.endpoint}", file=sys.stderr)

    if args.local:
        return run_local(args)
    return run_docker(args)


if __name__ == "__main__":
    sys.exit(main())
