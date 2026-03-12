import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import FileResponse

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


def _job_record(status: str = "ok") -> wrapper.JobRecord:
    return wrapper.JobRecord(
        job_id="97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
        query="restaurants istanbul",
        depth=1,
        max_reviews=10,
        status=status,
        created_at="2026-03-09T14:28:36.061990",
        date="2026-03-09",
        result_count=20,
        error="boom" if status == "failed" else None,
    )


class ResultEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_job_includes_inline_result_url(self):
        rec = _job_record()

        with patch.object(wrapper, "_get_job_from_redis", AsyncMock(return_value=rec)):
            response = await wrapper.get_job(rec.job_id)

        payload = json.loads(response.body)
        self.assertEqual(payload["download_url"], f"/api/jobs/{rec.job_id}/result")
        self.assertEqual(payload["result_url"], f"/api/jobs/{rec.job_id}/result/json")

    async def test_get_result_json_returns_inline_file_response(self):
        rec = _job_record()

        with TemporaryDirectory() as tmpdir:
            result_file = Path(tmpdir) / f"{rec.job_id}.json"
            result_file.write_text('[{"title":"Cafe"}]', encoding="utf-8")

            with patch.object(wrapper, "_get_job_from_redis", AsyncMock(return_value=rec)):
                with patch.object(wrapper, "_result_path_for_read", return_value=result_file):
                    response = await wrapper.get_result_json(rec.job_id)

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.path, str(result_file))
        self.assertEqual(response.headers["content-disposition"], "inline")
        self.assertEqual(response.media_type, "application/json; charset=utf-8")

    async def test_get_result_json_returns_202_for_pending_job(self):
        rec = _job_record(status="pending")

        with patch.object(wrapper, "_get_job_from_redis", AsyncMock(return_value=rec)):
            response = await wrapper.get_result_json(rec.job_id)

        self.assertEqual(response.status_code, 202)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "pending")

    async def test_get_result_json_raises_422_for_failed_job(self):
        rec = _job_record(status="failed")

        with patch.object(wrapper, "_get_job_from_redis", AsyncMock(return_value=rec)):
            with self.assertRaises(HTTPException) as ctx:
                await wrapper.get_result_json(rec.job_id)

        self.assertEqual(ctx.exception.status_code, 422)

    async def test_get_result_json_raises_410_when_file_is_missing(self):
        rec = _job_record()
        missing_file = Path("/tmp/does-not-exist.json")

        with patch.object(wrapper, "_get_job_from_redis", AsyncMock(return_value=rec)):
            with patch.object(wrapper, "_result_path_for_read", return_value=missing_file):
                with self.assertRaises(HTTPException) as ctx:
                    await wrapper.get_result_json(rec.job_id)

        self.assertEqual(ctx.exception.status_code, 410)


if __name__ == "__main__":
    unittest.main()
