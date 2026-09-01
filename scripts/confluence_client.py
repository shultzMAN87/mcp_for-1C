#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ядро DOC-39. HTTP-клиент Confluence: Retry-After, экспонента, лимит частоты,
пакетное чтение, журнал заголовков лимитов с первого прогона.

Пишет только через методы с явным write=True и только при allow_write=True у
клиента. По умолчанию клиент доступен на чтение (R18: --dry-run по умолчанию).

Работает и с Cloud, и с Data Center: общий знаменатель — REST v1 (/rest/api).
Для Cloud base_url должен включать /wiki.

Зависимость: httpx (уже в стеке как зависимость MCP SDK).
"""

from __future__ import annotations

import base64
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx

# B-3 / FAIL-2: защита потоков ровно в той форме, которую требует
# scripts/tests_host_scripts.py. Не encoding="utf-8": подмена кодировки дала
# бы кракозябры в cp1251-консоли, а замена — всего лишь «?» вместо галочки.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
RATE_LOG = REPO_ROOT / "logs" / "confluence_ratelimit.jsonl"

RATE_HEADERS = (
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-interval-seconds",
    "x-ratelimit-fillrate",
    "x-ratelimit-nearlimit",
)


class ConfluenceError(RuntimeError):
    pass


class WriteForbidden(ConfluenceError):
    """Попытка записи при allow_write=False. R18, предохранитель первый."""


@dataclass
class RetryPolicy:
    max_attempts: int = 6
    base_delay: float = 1.0
    max_delay: float = 60.0
    min_interval: float = 0.2       # не чаще 5 запросов в секунду
    respect_retry_after: bool = True


def load_env(path: Path = REPO_ROOT / ".env") -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class ConfluenceClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        user: str | None = None,
        *,
        allow_write: bool = False,
        policy: RetryPolicy | None = None,
        timeout: float = 30.0,
        rate_log: Path | None = RATE_LOG,
        verbose: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.allow_write = allow_write
        self.policy = policy or RetryPolicy()
        self.rate_log = rate_log
        self.verbose = verbose
        self._last_request = 0.0
        self.deployment: str | None = None

        if user:
            # Cloud: email + API token через Basic.
            raw = f"{user}:{token}".encode("utf-8")
            auth_header = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            # Data Center: персональный токен через Bearer.
            auth_header = f"Bearer {token}"

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": auth_header,
                "Accept": "application/json",
                # R9: кириллица в телах, кодировка проставляется явно.
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "1c-mcp-suite-docs/0.1",
                "X-Atlassian-Token": "no-check",
            },
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ConfluenceClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    def _log_rate_headers(self, response: httpx.Response) -> None:
        """Заголовки лимитов пишем с первого прогона: без них реальные пороги неизвестны."""
        if self.rate_log is None:
            return
        found = {h: response.headers[h] for h in RATE_HEADERS if h in response.headers}
        if not found:
            return
        self.rate_log.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": response.status_code,
            "url": str(response.request.url),
            "headers": found,
        }
        with self.rate_log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _sleep_before(self, delay: float, reason: str) -> None:
        if self.verbose:
            print(f"  пауза {delay:.1f} с ({reason})")
        time.sleep(delay)

    def request(self, method: str, path: str, *, write: bool = False, **kwargs: Any) -> httpx.Response:
        if write and not self.allow_write:
            raise WriteForbidden(
                f"{method} {path}: запись запрещена. Клиент создан с allow_write=False")

        gap = time.monotonic() - self._last_request
        if gap < self.policy.min_interval:
            time.sleep(self.policy.min_interval - gap)

        last_error: Exception | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == self.policy.max_attempts:
                    break
                self._sleep_before(self._backoff(attempt), f"сеть: {exc.__class__.__name__}")
                continue
            finally:
                self._last_request = time.monotonic()

            self._log_rate_headers(response)

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self.policy.max_attempts:
                    raise ConfluenceError(
                        f"{method} {path}: {response.status_code} после "
                        f"{attempt} попыток. {response.text[:300]}")
                delay = self._delay_from(response, attempt)
                self._sleep_before(delay, f"HTTP {response.status_code}")
                continue

            if response.status_code >= 400:
                raise ConfluenceError(
                    f"{method} {path}: {response.status_code}. {response.text[:500]}")
            return response

        raise ConfluenceError(f"{method} {path}: не удалось выполнить. {last_error}")

    def _delay_from(self, response: httpx.Response, attempt: int) -> float:
        if self.policy.respect_retry_after:
            raw = response.headers.get("retry-after")
            if raw:
                try:
                    return min(float(raw), self.policy.max_delay)
                except ValueError:
                    pass
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        delay = min(self.policy.base_delay * (2 ** (attempt - 1)), self.policy.max_delay)
        return delay * (0.75 + random.random() * 0.5)  # джиттер

    def get_json(self, path: str, **kwargs: Any) -> dict:
        return self.request("GET", path, **kwargs).json()

    # ------------------------------------------------------------------
    # Определение типа развёртывания (Р-1)
    # ------------------------------------------------------------------
    def detect_deployment(self) -> dict:
        """Cloud или Data Center. Различаем по наличию v2 API и по хосту."""
        info: dict[str, Any] = {"base_url": self.base_url}
        host = httpx.URL(self.base_url).host or ""
        info["host_suggests"] = "cloud" if host.endswith("atlassian.net") else "unknown"

        try:
            v2 = self.request("GET", "/api/v2/spaces", params={"limit": 1})
            info["api_v2"] = v2.status_code
        except ConfluenceError as exc:
            info["api_v2"] = f"нет ({exc})"

        try:
            v1 = self.request("GET", "/rest/api/space", params={"limit": 1})
            info["api_v1"] = v1.status_code
        except ConfluenceError as exc:
            info["api_v1"] = f"нет ({exc})"

        self.deployment = "cloud" if isinstance(info.get("api_v2"), int) else "datacenter"
        info["deployment"] = self.deployment
        return info

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    def get_page(self, page_id: str, expand: str = "version,space,body.storage,metadata.labels") -> dict:
        return self.get_json(f"/rest/api/content/{page_id}", params={"expand": expand})

    def search_cql(self, cql: str, *, limit: int = 100, expand: str = "version,space",
                   max_pages: int | None = None) -> Iterator[dict]:
        """R19: пакетное чтение через CQL — на порядок меньше вызовов, чем постранично."""
        start = 0
        fetched = 0
        while True:
            data = self.get_json("/rest/api/content/search", params={
                "cql": cql, "limit": limit, "start": start, "expand": expand})
            results = data.get("results", [])
            for item in results:
                yield item
                fetched += 1
                if max_pages is not None and fetched >= max_pages:
                    return
            if len(results) < limit or not data.get("_links", {}).get("next"):
                return
            start += limit

    def get_pages_batch(self, page_ids: Iterable[str],
                        expand: str = "version,body.storage,metadata.labels",
                        chunk: int = 50) -> Iterator[dict]:
        """Забираем пачками id in (...), а не по одной странице."""
        ids = [str(i) for i in page_ids]
        for offset in range(0, len(ids), chunk):
            part = ids[offset:offset + chunk]
            cql = "id in ({})".format(",".join(part))
            yield from self.search_cql(cql, limit=len(part), expand=expand)

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------
    def create_page(self, space: str, title: str, storage_body: str,
                    parent_id: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space},
            "body": {"storage": {"value": storage_body, "representation": "storage"}},
        }
        if parent_id:
            payload["ancestors"] = [{"id": str(parent_id)}]
        response = self.request("POST", "/rest/api/content", write=True, json=payload)
        return response.json()

    def update_page(self, page_id: str, title: str, storage_body: str, *,
                    message: str = "", labels: list[str] | None = None) -> dict:
        """Камень 1: номер версии читаем прямо перед записью, один повтор при конфликте.
        Камень 13: обновляем поверх опубликованной версии.
        Камень 15: метки страницы сохраняются отдельным вызовом."""
        for attempt in (1, 2):
            current = self.get_page(page_id, expand="version,space")
            version = int(current["version"]["number"])
            payload = {
                "id": str(page_id),
                "type": "page",
                "title": title,
                "version": {"number": version + 1, "message": message, "minorEdit": True},
                "body": {"storage": {"value": storage_body, "representation": "storage"}},
            }
            try:
                response = self.request("PUT", f"/rest/api/content/{page_id}",
                                        write=True, json=payload)
            except ConfluenceError as exc:
                if "409" in str(exc) and attempt == 1:
                    continue  # чужая правка между чтением и записью
                raise
            result = response.json()
            if labels:
                self.add_labels(page_id, labels)
            return result
        raise ConfluenceError(f"{page_id}: конфликт версий не разошёлся за два захода")

    def add_labels(self, page_id: str, labels: list[str]) -> dict:
        payload = [{"prefix": "global", "name": name} for name in labels]
        return self.request("POST", f"/rest/api/content/{page_id}/label",
                            write=True, json=payload).json()

    def get_comments(self, page_id: str) -> list[dict]:
        """DOC-37. В комментариях часто лежит «сейчас на самом деле не так»."""
        data = self.get_json(f"/rest/api/content/{page_id}/child/comment",
                             params={"expand": "body.storage,version", "limit": 100})
        return data.get("results", [])


def client_from_env(*, allow_write: bool = False, verbose: bool = False) -> ConfluenceClient:
    load_env()
    base_url = os.environ.get("CONFLUENCE_BASE_URL", "").strip()
    token = os.environ.get("CONFLUENCE_TOKEN", "").strip()
    user = os.environ.get("CONFLUENCE_USER", "").strip() or None
    if not base_url or not token:
        raise SystemExit(
            "Задайте CONFLUENCE_BASE_URL и CONFLUENCE_TOKEN в .env "
            "(для Cloud ещё CONFLUENCE_USER — почту учётки)")
    return ConfluenceClient(base_url, token, user, allow_write=allow_write, verbose=verbose)
