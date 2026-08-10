"""
Метрики и мониторинг для MCP-серверов
=======================================
Легковесный сбор метрик вызовов инструментов с сохранением в SQLite.
Предоставляет HTTP-эндпоинт для дашборда.

Использование:
    from mcp_metrics import track, get_dashboard_app

    @mcp.tool()
    @track
    def my_tool(...):
        ...

Дашборд:
    app = get_dashboard_app()
    uvicorn.run(app, port=9000)
"""

import os
import time
import json
import sqlite3
import functools
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

METRICS_DB = os.environ.get("METRICS_DB", "/data/metrics/mcp_metrics.db")


# ─── Инициализация БД ────────────────────────────────────────────────────

def _ensure_db():
    """Создаёт БД и таблицы если не существуют."""
    Path(METRICS_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(METRICS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            timestamp REAL NOT NULL,
            duration_ms INTEGER NOT NULL,
            success INTEGER NOT NULL,
            error_message TEXT,
            args_size INTEGER,
            result_size INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp
        ON tool_calls(timestamp DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tool_calls_tool
        ON tool_calls(tool_name)
    """)
    conn.commit()
    conn.close()


@contextmanager
def _get_conn():
    conn = sqlite3.connect(METRICS_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ─── Декоратор отслеживания ──────────────────────────────────────────────

_server_name = os.environ.get("MCP_SERVER_NAME", "unknown")


def track(func):
    """Декоратор для отслеживания вызовов MCP-инструментов."""
    _ensure_db()

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        success = True
        error_message = None
        result = None

        try:
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            success = False
            error_message = str(e)
            raise
        finally:
            duration_ms = int((time.time() - start) * 1000)
            try:
                args_size = sum(len(str(a)) for a in args) + sum(len(str(v)) for v in kwargs.values())
                result_size = len(str(result)) if result else 0

                with _get_conn() as conn:
                    conn.execute("""
                        INSERT INTO tool_calls
                        (server_name, tool_name, timestamp, duration_ms, success,
                         error_message, args_size, result_size)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        _server_name,
                        func.__name__,
                        time.time(),
                        duration_ms,
                        1 if success else 0,
                        error_message,
                        args_size,
                        result_size,
                    ))
                    conn.commit()
            except Exception:
                pass  # Не роняем основной запрос из-за метрик

    # Поддержка async функций
    if hasattr(func, '__code__') and func.__code__.co_flags & 0x80:  # CO_COROUTINE
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.time()
            success = True
            error_message = None
            result = None

            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                success = False
                error_message = str(e)
                raise
            finally:
                duration_ms = int((time.time() - start) * 1000)
                try:
                    args_size = sum(len(str(a)) for a in args) + sum(len(str(v)) for v in kwargs.values())
                    result_size = len(str(result)) if result else 0

                    with _get_conn() as conn:
                        conn.execute("""
                            INSERT INTO tool_calls
                            (server_name, tool_name, timestamp, duration_ms, success,
                             error_message, args_size, result_size)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            _server_name,
                            func.__name__,
                            time.time(),
                            duration_ms,
                            1 if success else 0,
                            error_message,
                            args_size,
                            result_size,
                        ))
                        conn.commit()
                except Exception:
                    pass

        return async_wrapper

    return wrapper


# ─── API для запроса метрик ──────────────────────────────────────────────

def get_stats(hours: int = 24) -> dict:
    """Получить статистику за последние N часов."""
    _ensure_db()
    cutoff = time.time() - hours * 3600

    with _get_conn() as conn:
        # Общая статистика
        total = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful,
                   AVG(duration_ms) as avg_duration,
                   MAX(duration_ms) as max_duration
            FROM tool_calls
            WHERE timestamp >= ?
        """, (cutoff,)).fetchone()

        # По инструментам
        by_tool = conn.execute("""
            SELECT tool_name,
                   server_name,
                   COUNT(*) as calls,
                   AVG(duration_ms) as avg_ms,
                   MAX(duration_ms) as max_ms,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors
            FROM tool_calls
            WHERE timestamp >= ?
            GROUP BY tool_name, server_name
            ORDER BY calls DESC
        """, (cutoff,)).fetchall()

        # По серверам
        by_server = conn.execute("""
            SELECT server_name,
                   COUNT(*) as calls,
                   AVG(duration_ms) as avg_ms,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors
            FROM tool_calls
            WHERE timestamp >= ?
            GROUP BY server_name
            ORDER BY calls DESC
        """, (cutoff,)).fetchall()

        # Последние ошибки
        errors = conn.execute("""
            SELECT tool_name, server_name, timestamp, error_message
            FROM tool_calls
            WHERE success = 0 AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 20
        """, (cutoff,)).fetchall()

        # Медленные запросы
        slow = conn.execute("""
            SELECT tool_name, server_name, duration_ms, timestamp
            FROM tool_calls
            WHERE timestamp >= ?
            ORDER BY duration_ms DESC
            LIMIT 10
        """, (cutoff,)).fetchall()

    return {
        "period_hours": hours,
        "summary": {
            "total_calls": total["total"] or 0,
            "successful": total["successful"] or 0,
            "error_rate": round(
                (1 - (total["successful"] or 0) / max(total["total"] or 1, 1)) * 100, 2
            ),
            "avg_duration_ms": round(total["avg_duration"] or 0, 1),
            "max_duration_ms": total["max_duration"] or 0,
        },
        "by_tool": [dict(r) for r in by_tool],
        "by_server": [dict(r) for r in by_server],
        "recent_errors": [
            {**dict(r), "timestamp_iso": datetime.fromtimestamp(r["timestamp"]).isoformat()}
            for r in errors
        ],
        "slow_queries": [
            {**dict(r), "timestamp_iso": datetime.fromtimestamp(r["timestamp"]).isoformat()}
            for r in slow
        ],
    }


# ─── Dashboard HTTP-сервер ───────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>1C MCP Suite — Metrics</title>
<style>
  :root {
    --bg: #0f1419;
    --panel: #1a1f29;
    --border: #2d3748;
    --text: #e2e8f0;
    --muted: #718096;
    --accent: #4fd1c5;
    --error: #fc8181;
    --success: #68d391;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 24px;
    line-height: 1.5;
  }
  h1 {
    font-size: 28px;
    margin-bottom: 8px;
    color: var(--accent);
  }
  .subtitle { color: var(--muted); margin-bottom: 32px; font-size: 14px; }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
  }
  .stat-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
  }
  .stat-value {
    font-size: 32px;
    font-weight: 700;
    color: var(--accent);
  }
  .stat-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--muted);
    margin-top: 8px;
  }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 24px;
    margin-bottom: 24px;
  }
  .panel h2 {
    font-size: 18px;
    margin-bottom: 16px;
    color: var(--text);
  }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
  }
  th {
    color: var(--muted);
    font-weight: 500;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.5px;
  }
  tr:last-child td { border-bottom: none; }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
  }
  .badge-server { background: #2c5282; color: #bee3f8; }
  .badge-error { background: #742a2a; color: #fc8181; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .refresh {
    float: right;
    background: var(--accent);
    color: var(--bg);
    border: none;
    padding: 8px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 500;
  }
  .error-row { background: rgba(252, 129, 129, 0.05); }
</style>
</head>
<body>
<h1>1C MCP Suite — Dashboard</h1>
<div class="subtitle">
  Мониторинг MCP-серверов
  <button class="refresh" onclick="loadData()">Обновить</button>
</div>

<div id="content"></div>

<script>
async function loadData() {
  const resp = await fetch('/api/stats?hours=24');
  const data = await resp.json();
  render(data);
}

function render(data) {
  const s = data.summary;
  let html = `
    <div class="summary-grid">
      <div class="stat-card">
        <div class="stat-value">${s.total_calls}</div>
        <div class="stat-label">Всего вызовов (24ч)</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${s.successful}</div>
        <div class="stat-label">Успешных</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${s.error_rate}%</div>
        <div class="stat-label">Error Rate</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${s.avg_duration_ms}ms</div>
        <div class="stat-label">Среднее время</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${s.max_duration_ms}ms</div>
        <div class="stat-label">Максимум</div>
      </div>
    </div>

    <div class="panel">
      <h2>Топ инструментов</h2>
      <table>
        <thead>
          <tr>
            <th>Инструмент</th>
            <th>Сервер</th>
            <th class="num">Вызовов</th>
            <th class="num">Ср. время</th>
            <th class="num">Макс.</th>
            <th class="num">Ошибок</th>
          </tr>
        </thead>
        <tbody>
          ${data.by_tool.map(t => `
            <tr>
              <td><code>${t.tool_name}</code></td>
              <td><span class="badge badge-server">${t.server_name}</span></td>
              <td class="num">${t.calls}</td>
              <td class="num">${Math.round(t.avg_ms)}ms</td>
              <td class="num">${t.max_ms}ms</td>
              <td class="num">${t.errors > 0 ? '<span class="badge badge-error">' + t.errors + '</span>' : '0'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>По серверам</h2>
      <table>
        <thead>
          <tr>
            <th>Сервер</th>
            <th class="num">Вызовов</th>
            <th class="num">Ср. время</th>
            <th class="num">Ошибок</th>
          </tr>
        </thead>
        <tbody>
          ${data.by_server.map(s => `
            <tr>
              <td><span class="badge badge-server">${s.server_name}</span></td>
              <td class="num">${s.calls}</td>
              <td class="num">${Math.round(s.avg_ms)}ms</td>
              <td class="num">${s.errors}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;

  if (data.recent_errors.length > 0) {
    html += `
      <div class="panel">
        <h2>Последние ошибки</h2>
        <table>
          <thead>
            <tr>
              <th>Время</th>
              <th>Инструмент</th>
              <th>Ошибка</th>
            </tr>
          </thead>
          <tbody>
            ${data.recent_errors.map(e => `
              <tr class="error-row">
                <td>${new Date(e.timestamp_iso).toLocaleString('ru')}</td>
                <td><code>${e.tool_name}</code></td>
                <td>${e.error_message || ''}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  }

  if (data.slow_queries.length > 0) {
    html += `
      <div class="panel">
        <h2>Самые медленные запросы</h2>
        <table>
          <thead>
            <tr>
              <th>Инструмент</th>
              <th>Сервер</th>
              <th class="num">Время</th>
              <th>Когда</th>
            </tr>
          </thead>
          <tbody>
            ${data.slow_queries.map(q => `
              <tr>
                <td><code>${q.tool_name}</code></td>
                <td><span class="badge badge-server">${q.server_name}</span></td>
                <td class="num">${q.duration_ms}ms</td>
                <td>${new Date(q.timestamp_iso).toLocaleString('ru')}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  }

  document.getElementById('content').innerHTML = html;
}

loadData();
setInterval(loadData, 30000);  // автообновление каждые 30 сек
</script>
</body>
</html>"""


def get_dashboard_app():
    """Возвращает ASGI-приложение для дашборда."""
    try:
        from starlette.applications import Starlette
        from starlette.responses import HTMLResponse, JSONResponse
        from starlette.routing import Route
    except ImportError:
        raise RuntimeError("Для дашборда требуется starlette: pip install starlette")

    async def dashboard_home(request):
        return HTMLResponse(DASHBOARD_HTML)

    async def api_stats(request):
        hours = int(request.query_params.get("hours", 24))
        return JSONResponse(get_stats(hours))

    return Starlette(routes=[
        Route("/", dashboard_home),
        Route("/api/stats", api_stats),
    ])


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("METRICS_PORT", 9000))
    print(f"Запуск дашборда метрик на порту {port}")
    print(f"Откройте http://localhost:{port}")
    uvicorn.run(get_dashboard_app(), host="0.0.0.0", port=port)
