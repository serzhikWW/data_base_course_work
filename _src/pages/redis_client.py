import redis
import pickle
from datetime import timedelta
from functools import wraps
import streamlit as st
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RedisConnectionError(Exception):
    pass

class RedisClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RedisClient, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        self._client = None
        try:
            self._client = redis.Redis(
                host='localhost',
                port=6379,
                db=0,
                socket_connect_timeout=3,
                socket_timeout=3,
                decode_responses=False,
                health_check_interval=30
            )
            # Проверка подключения
            self._client.ping()
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.warning(f"Redis connection failed: {str(e)}")
            self._client = None

    @property
    def client(self):
        if self._client is None:
            raise RedisConnectionError("Redis connection is not available")
        return self._client

    def is_available(self):
        try:
            return self._client is not None and self._client.ping()
        except (redis.ConnectionError, redis.TimeoutError):
            return False


# Глобальный экземпляр Redis
try:
    redis_client = RedisClient()
except Exception as e:
    st.warning(f"Redis initialization error: {str(e)}")
    redis_client = None


def cache_to_redis(key_prefix: str, ttl: int = 3600):
    """Декоратор для кэширования результатов функций в Redis."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Если Redis недоступен, просто выполняем функцию
            if not redis_client or not redis_client.is_available():
                return await func(*args, **kwargs)

            cache_key = f"{key_prefix}:{str(args)}:{str(kwargs)}"

            try:
                # Пытаемся получить данные из кэша
                cached_data = redis_client.client.get(cache_key)
                if cached_data is not None:
                    return pickle.loads(cached_data)
            except Exception as e:
                logger.error(f"Redis get error: {str(e)}")

            # Если в кэше нет, выполняем функцию
            result = await func(*args, **kwargs)

            try:
                # Преобразуем asyncpg.Record в dict перед сериализацией
                serializable_result = result
                if isinstance(result, list):
                    serializable_result = [dict(record) for record in result]
                elif hasattr(result, '_asdict'):  # Для asyncpg.Record
                    serializable_result = dict(result)

                # Сохраняем результат в кэш
                redis_client.client.setex(
                    cache_key,
                    timedelta(seconds=ttl),
                    pickle.dumps(serializable_result)
                )
            except Exception as e:
                logger.error(f"Redis setex error: {str(e)}")

            return result  # Возвращаем оригинальный результат, не сериализованный

        return wrapper

    return decorator


def invalidate_cache(key_prefix: str):
    """Удаляет все ключи кэша с указанным префиксом."""
    if not redis_client or not redis_client.is_available():
        return

    try:
        keys = redis_client.client.keys(f"{key_prefix}:*")
        if keys:
            redis_client.client.delete(*keys)
    except Exception as e:
        logger.error(f"Redis cache invalidation error: {str(e)}")