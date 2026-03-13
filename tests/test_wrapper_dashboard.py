import json
import sys
import types
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

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


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


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
        existed = key in self.store
        self.store.pop(key, None)
        return 1 if existed else 0

    async def incr(self, key):
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = value
        return value

    async def expire(self, key, ttl):
        return 1


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.redis = _FakeRedis()
        wrapper._redis = self.redis
        self.original_hash = wrapper.DASHBOARD_PASSWORD_HASH
        self.original_secure = wrapper.DASHBOARD_SECURE_COOKIES
        self.original_lifespan = wrapper.app.router.lifespan_context
        wrapper.DASHBOARD_PASSWORD_HASH = wrapper.PasswordHasher().hash("supersecret")
        wrapper.DASHBOARD_SECURE_COOKIES = False
        wrapper.app.router.lifespan_context = _noop_lifespan

    def tearDown(self):
        wrapper.DASHBOARD_PASSWORD_HASH = self.original_hash
        wrapper.DASHBOARD_SECURE_COOKIES = self.original_secure
        wrapper.app.router.lifespan_context = self.original_lifespan

    def _client(self):
        return TestClient(wrapper.app)

    def test_dashboard_redirects_when_not_authenticated(self):
        with self._client() as client:
            response = client.get("/dashboard", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/login")

    def test_guide_page_supports_turkish_and_english(self):
        with self._client() as client:
            tr_response = client.get("/guide?lang=tr")
            en_response = client.get("/guide?lang=en")

        self.assertEqual(tr_response.status_code, 200)
        self.assertEqual(en_response.status_code, 200)
        self.assertIn("GeoLocalRank Kullanım Kılavuzu", tr_response.text)
        self.assertIn("GeoLocalRank Usage Guide", en_response.text)

    def test_login_sets_httponly_session_cookie_and_opens_dashboard(self):
        dashboard_context = {
            "jobs": [],
            "job_date": "2026-03-13",
            "status_filter": "all",
            "summary": {"total": 0, "pending": 0, "ok": 0, "failed": 0},
        }

        with patch.object(wrapper, "_build_dashboard_page_context", AsyncMock(return_value=dashboard_context)):
            with self._client() as client:
                response = client.post(
                    "/admin/login",
                    data={"password": "supersecret"},
                    follow_redirects=False,
                )
                dashboard = client.get("/dashboard")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("Scrape job oluştur", dashboard.text)
        self.assertIn("GeoLocalRank Kullanım Kılavuzu", dashboard.text)
        self.assertNotIn("supersecret", dashboard.text)

    def test_dashboard_create_job_requires_valid_csrf_and_uses_internal_service(self):
        dashboard_context = {
            "jobs": [],
            "job_date": "2026-03-13",
            "status_filter": "all",
            "summary": {"total": 0, "pending": 0, "ok": 0, "failed": 0},
        }
        created_payload = {"job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48", "status": "pending"}

        with patch.object(wrapper, "_build_dashboard_page_context", AsyncMock(return_value=dashboard_context)):
            with patch.object(wrapper, "_create_job_internal", AsyncMock(return_value=(created_payload, 201))) as create_mock:
                with self._client() as client:
                    login = client.post(
                        "/admin/login",
                        data={"password": "supersecret"},
                        follow_redirects=False,
                    )
                    self.assertEqual(login.status_code, 303)

                    session_key = next(key for key in self.redis.store if key.startswith("admin:session:"))
                    csrf_token = json.loads(self.redis.store[session_key])["csrf"]

                    response = client.post(
                        "/dashboard/jobs",
                        data={
                            "csrf_token": csrf_token,
                            "query": "restaurants istanbul",
                            "depth": "1",
                            "max_reviews": "10",
                            "lang": "tr",
                        },
                        follow_redirects=False,
                    )

        self.assertEqual(response.status_code, 303)
        self.assertIn("created=97c9ed6c-7e52-4c74-aa34-d18a16b9dd48", response.headers["location"])
        create_mock.assert_awaited_once()

    def test_dashboard_logout_rejects_invalid_csrf(self):
        with self._client() as client:
            login = client.post("/admin/login", data={"password": "supersecret"}, follow_redirects=False)
            self.assertEqual(login.status_code, 303)
            response = client.post("/admin/logout", data={"csrf_token": "wrong"})

        self.assertEqual(response.status_code, 403)

    def test_job_detail_renders_preview(self):
        rec = wrapper.JobRecord(
            job_id="97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
            query="restaurants istanbul",
            depth=1,
            max_reviews=10,
            status="ok",
            created_at="2026-03-13T16:10:00",
            date="2026-03-13",
            result_count=1,
            lang="tr",
        )

        with patch.object(wrapper, "_get_job_from_redis", AsyncMock(return_value=rec)):
            with patch.object(wrapper, "_get_result_content_or_raise", AsyncMock(return_value='[{"title":"Cafe"}]')):
                with self._client() as client:
                    login = client.post("/admin/login", data={"password": "supersecret"}, follow_redirects=False)
                    self.assertEqual(login.status_code, 303)
                    response = client.get(f"/dashboard/jobs/{rec.job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Cafe", response.text)


if __name__ == "__main__":
    unittest.main()
