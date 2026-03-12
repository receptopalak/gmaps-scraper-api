import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import redis.asyncio  # noqa: F401
except ModuleNotFoundError:
    fake_redis = types.ModuleType("redis")
    fake_redis_asyncio = types.ModuleType("redis.asyncio")
    fake_redis_asyncio.Redis = object
    fake_redis_asyncio.from_url = lambda *args, **kwargs: None
    fake_redis.asyncio = fake_redis_asyncio
    sys.modules["redis"] = fake_redis
    sys.modules["redis.asyncio"] = fake_redis_asyncio

import wrapper


class _FakeTask:
    def add_done_callback(self, _cb):
        return None


def _fake_create_task(coro):
    coro.close()
    return _FakeTask()


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


class CreateJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = _FakeRedis()
        wrapper._redis = self.redis

    async def test_create_job_passes_extra_reviews_to_gosom(self):
        response_json = {"id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48"}
        http_response = Mock()
        http_response.status_code = 201
        http_response.json.return_value = response_json

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = http_response

        with patch.object(wrapper, "_daily_cleanup", AsyncMock()):
            with patch.object(wrapper, "_save_job_to_redis", AsyncMock()):
                with patch.object(wrapper, "_save_index", AsyncMock()):
                    with patch.object(wrapper.asyncio, "create_task", side_effect=_fake_create_task):
                        with patch.object(wrapper.httpx, "AsyncClient", return_value=client):
                            response = await wrapper.create_job(
                                {
                                    "query": "restaurants istanbul",
                                    "depth": 1,
                                    "max_reviews": 15,
                                    "extra_reviews": True,
                                    "email": True,
                                    "lang": "en",
                                    "geo": "41.0082,28.9784",
                                    "zoom": 14,
                                    "radius": 2500,
                                    "fast_mode": True,
                                }
                            )

        payload = client.post.await_args.kwargs["json"]
        self.assertTrue(payload["extra_reviews"])
        self.assertTrue(payload["email"])
        self.assertTrue(payload["fast_mode"])
        self.assertEqual(payload["lang"], "en")
        self.assertEqual(payload["geo"], "41.0082,28.9784")
        self.assertEqual(payload["zoom"], 14)
        self.assertEqual(payload["radius"], 2500)
        self.assertEqual(payload["max_reviews"], 15)
        body = json.loads(response.body)
        self.assertTrue(body["extra_reviews"])
        self.assertTrue(body["email"])
        self.assertTrue(body["fast_mode"])
        self.assertEqual(body["lang"], "en")

    async def test_create_job_builds_maps_url_from_place_id(self):
        response_json = {"id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48"}
        http_response = Mock()
        http_response.status_code = 201
        http_response.json.return_value = response_json

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = http_response

        with patch.object(wrapper, "_daily_cleanup", AsyncMock()):
            with patch.object(wrapper, "_save_job_to_redis", AsyncMock()):
                with patch.object(wrapper, "_save_index", AsyncMock()):
                    with patch.object(wrapper.asyncio, "create_task", side_effect=_fake_create_task):
                        with patch.object(wrapper.httpx, "AsyncClient", return_value=client):
                            response = await wrapper.create_job(
                                {
                                    "place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
                                    "query": "Google Sydney",
                                    "depth": 1,
                                    "max_reviews": 10,
                                }
                            )

        keyword = client.post.await_args.kwargs["json"]["keywords"][0]
        self.assertIn("query_place_id=ChIJN1t_tDeuEmsRUsoyG83frY4", keyword)
        self.assertIn("query=Google+Sydney", keyword)
        body = json.loads(response.body)
        self.assertEqual(body["place_id"], "ChIJN1t_tDeuEmsRUsoyG83frY4")

    async def test_create_job_rejects_invalid_zoom(self):
        with self.assertRaises(wrapper.HTTPException) as ctx:
            await wrapper.create_job(
                {
                    "query": "restaurants istanbul",
                    "zoom": 30,
                }
            )

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_create_job_rejects_invalid_geo(self):
        with self.assertRaises(wrapper.HTTPException) as ctx:
            await wrapper.create_job(
                {
                    "query": "restaurants istanbul",
                    "geo": "istanbul",
                }
            )

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_create_job_requires_query_when_place_id_is_used(self):
        with self.assertRaises(wrapper.HTTPException) as ctx:
            await wrapper.create_job(
                {
                    "place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
                }
            )

        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
