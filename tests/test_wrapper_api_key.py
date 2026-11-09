import sys
import types
import unittest
from unittest.mock import patch

from fastapi import HTTPException

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


class _Req:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


class ApiKeyTests(unittest.TestCase):
    def test_no_keys_configured_keeps_api_open(self):
        with patch.object(wrapper, "API_KEYS", ()):
            wrapper.require_api_key(_Req({}))

    def test_valid_key_in_header_or_bearer_passes(self):
        with patch.object(wrapper, "API_KEYS", ("old-key", "new-key")):
            wrapper.require_api_key(_Req({"X-API-Key": "new-key"}))
            wrapper.require_api_key(_Req({"Authorization": "Bearer old-key"}))

    def test_missing_or_wrong_key_is_rejected(self):
        with patch.object(wrapper, "API_KEYS", ("secret",)):
            for headers in ({}, {"X-API-Key": "wrong"}, {"Authorization": "Bearer "}, {"X-API-Key": ""}):
                with self.assertRaises(HTTPException) as ctx:
                    wrapper.require_api_key(_Req(headers))
                self.assertEqual(ctx.exception.status_code, 401)

    def test_parse_keys_ignores_blanks(self):
        self.assertEqual(wrapper._parse_api_keys(" a , ,b,"), ("a", "b"))
        self.assertEqual(wrapper._parse_api_keys(""), ())

    def test_every_api_route_requires_the_key(self):
        api_routes = [r for r in wrapper.app.routes if getattr(r, "path", "").startswith("/api/")]
        self.assertGreaterEqual(len(api_routes), 5)
        for route in api_routes:
            calls = [d.call for d in route.dependant.dependencies]
            self.assertIn(wrapper.require_api_key, calls, route.path)


if __name__ == "__main__":
    unittest.main()
