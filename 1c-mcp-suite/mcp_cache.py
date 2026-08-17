"""
Кэширование для MCP-серверов
==============================
Простой TTL-кэш для часто запрашиваемых метаданных.
Можно использовать в памяти (dict) или через Redis.

Использование:
    from mcp_cache import cached

    @cached(ttl=300)  # 5 минут
    def expensive_metadata_query(obj_name):
        return neo4j_query(...)
"""

import os
import time
import json
import hashlib
import functools
import threading
from typing import Any

CACHE_BACKEND = os.environ.get("CACHE_BACKEND", "memory")  # "memory" или "redis"
CACHE_TTL_DEFAULT = int(os.environ.get("CACHE_TTL", "300"))  # 5 минут по умолчанию
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


# ─── In-memory backend ──────────────────────────────────────────────────

class MemoryCache:
    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any:
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        expires_at, value = entry
        if expires_at < time.time():
            del self._store[key]
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: Any, ttl: int):
        self._store[key] = (time.time() + ttl, value)

    def delete(self, key: str):
        self._store.pop(key, None)

    def clear(self):
        self._store.clear()

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "backend": "memory",
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total * 100, 1) if total > 0 else 0,
        }


# ─── Redis backend (опционально) ─────────────────────────────────────────

class RedisCache:
    def __init__(self):
        try:
            import redis
            self._client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            self._client.ping()
            self._hits = 0
            self._misses = 0
            self._available = True
        except Exception as e:
            print(f"Redis недоступен, fallback на memory: {e}")
            self._available = False
            self._fallback = MemoryCache()

    def get(self, key: str) -> Any:
        if not self._available:
            return self._fallback.get(key)
        try:
            val = self._client.get(key)
            if val is None:
                self._misses += 1
                return None
            self._hits += 1
            return json.loads(val)
        except Exception:
            self._misses += 1
            return None

    def set(self, key: str, value: Any, ttl: int):
        if not self._available:
            return self._fallback.set(key, value, ttl)
        try:
            self._client.setex(key, ttl, json.dumps(value, ensure_ascii=False))
        except Exception:
            pass

    def delete(self, key: str):
        if not self._available:
            return self._fallback.delete(key)
        try:
            self._client.delete(key)
        except Exception:
            pass

    def clear(self):
        if not self._available:
            return self._fallback.clear()
        try:
            self._client.flushdb()
        except Exception:
            pass

    def stats(self) -> dict:
        if not self._available:
            return self._fallback.stats()
        total = self._hits + self._misses
        try:
            info = self._client.info("memory")
            return {
                "backend": "redis",
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total * 100, 1) if total > 0 else 0,
                "memory_used": info.get("used_memory_human", "unknown"),
            }
        except Exception:
            return {"backend": "redis", "error": "unavailable"}


# ─── Глобальный инстанс ──────────────────────────────────────────────────
#
# AUDIT-3. Здесь жил третий экземпляр дефекта «флаг вместо замка» — того
# самого, который `PERF-6` чинил у моделей, а `PERF-6.1` нашёл двадцатью
# строками ниже комментария, где он разобран.
#
# Было:
#     if _cache_instance is None:
#         _cache_instance = MemoryCache()
#
# Вопрос, который задаёт эта проверка, — «уже готово?». Пока идёт создание,
# она честно отвечает «нет», и второй поток делает из этого «нет» вывод
# «значит, надо создавать». Нужен другой вопрос: «этим уже кто-то занят?»
#
# Чем это плохо именно здесь. Оба сервера, использующие кеш, крутятся под
# uvicorn и обслуживают запросы параллельно. Два потока, пришедшие в
# `get_cache()` одновременно, создают ДВА кеша; побеждает тот, кто присвоил
# последним, но первый уже отдан своему вызывающему и живёт в его
# декораторе. Дальше записи расходятся между двумя словарями: половина
# попаданий превращается в промахи.
#
# И это не отказ, а тихая потеря скорости, которую нечем заметить:
# `stats()` покажет статистику ОДНОГО из двух кешей, и цифры в ней будут
# правдоподобные. Ровно тот жанр, который заход 5 разбирал как «состояние
# определяется однажды и выдаётся за факт».
#
# Цена ошибки меньше, чем у моделей справки (гигабайты) и несравнима с
# `API-1` (открытый разрушающий инструмент). Но правка стоит четыре строки,
# а разбор дефекта — полдня, и уже дважды стоил.

_cache_lock = threading.Lock()
_cache_instance = None


def get_cache():
    """Единственный кеш процесса, кто бы ни спросил первым."""
    global _cache_instance
    if _cache_instance is not None:
        return _cache_instance
    with _cache_lock:
        # Второй раз проверяем ПОД замком: пока ждали, создать мог другой
        # поток. Без этой строки замок не помогает — он лишь выстраивает
        # потоки в очередь на создание.
        if _cache_instance is None:
            if CACHE_BACKEND == "redis":
                _cache_instance = RedisCache()
            else:
                _cache_instance = MemoryCache()
    return _cache_instance


# ─── Декоратор ──────────────────────────────────────────────────────────

def _looks_degraded(result) -> bool:
    """
    Стоит ли эта выдача в кеше.

    Проверяется двумя способами: пометкой текущего вызова из `refusal`
    (если модуль доехал в образ) и самим ответом — инструмент мог
    проставить `answerable: false` или `degraded: true` сам, до кеша.
    Второй способ работает всегда, даже когда `refusal` недоступен.
    """
    try:
        from refusal import is_degraded
        if is_degraded():
            return True
    except ImportError:
        pass

    if isinstance(result, dict):
        payload = result
    elif isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            payload = json.loads(result)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
    else:
        return False

    return payload.get("degraded") is True or payload.get("answerable") is False


def cached(ttl: int = CACHE_TTL_DEFAULT, key_prefix: str = ""):
    """
    Декоратор кэширования результатов функций.

    @cached(ttl=600)
    def my_func(arg1, arg2):
        return expensive_operation()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Формируем ключ
            key_parts = [key_prefix or func.__name__]
            key_parts.extend(str(a) for a in args)
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            key_str = "|".join(key_parts)
            key = hashlib.md5(key_str.encode()).hexdigest()

            cache = get_cache()
            value = cache.get(key)
            if value is not None:
                return value

            result = func(*args, **kwargs)

            # OBS-1: ответ, полученный хуже штатного, в кеш не кладём.
            #
            # Пометки деградации (`refusal.note_degraded`) ставятся внутри
            # вызова, а поля дописываются снаружи — уже после кеша. Значит,
            # закешированная «частичная» выдача при следующем обращении
            # приехала бы как полноценная: счётчики по нулям, `degraded`
            # false. Это ровно тот жанр, который весь этот заход и чинит —
            # отказ, притворившийся результатом, только с отсрочкой на TTL.
            #
            # Дешевле не кешировать: деградация редка, а десять минут жизни
            # неверного ответа стоят дороже одного лишнего запроса.
            if _looks_degraded(result):
                return result

            cache.set(key, result, ttl)
            return result

        # Добавляем методы для управления кэшем
        wrapper.cache_clear = lambda: get_cache().clear()
        wrapper.cache_delete = lambda *args, **kwargs: get_cache().delete(
            hashlib.md5("|".join([
                key_prefix or func.__name__,
                *(str(a) for a in args),
                *(f"{k}={v}" for k, v in sorted(kwargs.items()))
            ]).encode()).hexdigest()
        )

        return wrapper

    return decorator


def cache_stats() -> dict:
    """Получить статистику кэша."""
    return get_cache().stats()


def cache_clear():
    """Очистить весь кэш."""
    get_cache().clear()
