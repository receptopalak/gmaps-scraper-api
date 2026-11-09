import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

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


def _row(point, rank, place_id, sponsored=False, **extra):
    row = {"input_id": f"p{point}", "rank": rank if not sponsored else None, "sponsored": sponsored,
           "title": f"Yer {place_id}", "place_id": place_id, "cid": "1", "data_id": "0x1:0x1",
           "review_rating": "4.800000", "review_count": "52", "category": "Fırın",
           "latitude": "41.0", "longitude": "29.0", "link": "https://maps/x", "user_reviews": None,
           "position": str(rank + 1) if rank else "1"}
    row.update(extra)
    return row


class RankScanBodyTests(unittest.TestCase):
    def test_valid_body_is_normalized(self):
        body = wrapper._parse_rank_scan_body({
            "keyword": "  işitme cihazı ", "points": [{"lat": 40.99, "lng": 29.03}, {"lat": "41", "lng": "29"}],
            "zoom": 15, "depth": 2, "lang": "TR"})
        self.assertEqual(body["keyword"], "işitme cihazı")
        self.assertEqual(body["points"], [{"lat": 40.99, "lng": 29.03}, {"lat": 41.0, "lng": 29.0}])
        self.assertEqual((body["zoom"], body["depth"], body["lang"]), (15, 2, "tr"))

    def test_defaults(self):
        body = wrapper._parse_rank_scan_body({"keyword": "a", "points": [{"lat": 1, "lng": 2}]})
        self.assertEqual((body["zoom"], body["depth"], body["lang"]), (15, 1, "tr"))

    def test_invalid_bodies_are_rejected_with_400(self):
        bad = [
            {"points": [{"lat": 1, "lng": 2}]},
            {"keyword": "a", "points": []},
            {"keyword": "a", "points": [{"lat": 91, "lng": 2}]},
            {"keyword": "a", "points": [{"lat": "x", "lng": 2}]},
            {"keyword": "a", "points": [{"lat": 1, "lng": 2}] * 122},
            {"keyword": "a", "points": [{"lat": 1, "lng": 2}], "zoom": 25},
            {"keyword": "a", "points": [{"lat": 1, "lng": 2}], "depth": 0},
            {"keyword": "a" * 201, "points": [{"lat": 1, "lng": 2}]},
        ]
        for body in bad:
            with self.assertRaises(HTTPException) as ctx:
                wrapper._parse_rank_scan_body(body)
            self.assertEqual(ctx.exception.status_code, 400, body)


class RankScanResultTests(unittest.TestCase):
    def test_rows_are_grouped_by_point_and_ordered(self):
        points = [{"lat": 1.0, "lng": 2.0}, {"lat": 3.0, "lng": 4.0}, {"lat": 5.0, "lng": 6.0}]
        rows = [_row(0, 2, "B"), _row(0, None, "AD", sponsored=True), _row(0, 1, "A"), _row(1, 1, "C"), _row(9, 1, "X")]
        out = wrapper._rank_scan_points(points, rows)

        self.assertEqual([p["index"] for p in out], [0, 1, 2])
        self.assertEqual([r["place_id"] for r in out[0]["results"]], ["A", "B", "AD"])
        first = out[0]["results"][0]
        self.assertEqual(first, {"rank": 1, "position": 2, "sponsored": False, "title": "Yer A", "place_id": "A", "cid": "1",
                                 "data_id": "0x1:0x1", "rating": 4.8, "review_count": 52, "category": "Fırın",
                                 "lat": 41.0, "lng": 29.0, "link": "https://maps/x"})
        self.assertTrue(out[0]["observed"])
        self.assertEqual(out[1]["results"][0]["place_id"], "C")
        # Sonuç gelmeyen nokta "boş sonuç" değil "ölçülemedi" olarak işaretlenir.
        self.assertEqual(out[2], {"index": 2, "lat": 5.0, "lng": 6.0, "observed": False, "results": []})

    def test_missing_numbers_become_none(self):
        out = wrapper._rank_scan_points([{"lat": 1.0, "lng": 2.0}],
                                        [_row(0, 1, "A", review_rating=None, review_count="", category=None)])
        r = out[0]["results"][0]
        self.assertEqual((r["rating"], r["review_count"], r["category"]), (None, None, None))


class RankScanCreateTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_sends_rank_only_grid_to_engine(self):
        sent = {}

        class _Resp:
            status_code = 201
            text = ""

            def json(self):
                return {"id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48"}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                sent["url"], sent["json"] = url, json
                return _Resp()

        with patch.object(wrapper.httpx, "AsyncClient", _Client), \
                patch.object(wrapper, "_save_job_to_redis", AsyncMock()), \
                patch.object(wrapper, "_save_index", AsyncMock()), \
                patch.object(wrapper, "_daily_cleanup", AsyncMock()), \
                patch.object(wrapper, "_poll_and_finalize", AsyncMock()):
            payload, status = await wrapper._create_rank_scan_internal(
                {"keyword": "işitme cihazı", "points": [{"lat": 40.99, "lng": 29.03}]})

        self.assertEqual(status, 201)
        self.assertEqual(payload["scan_id"], "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48")
        self.assertEqual(payload["status"], "pending")
        body = sent["json"]
        self.assertTrue(body["rank_only"])
        self.assertEqual(body["keywords"], ["işitme cihazı"])
        self.assertEqual(body["points"], [{"lat": 40.99, "lon": 29.03}])
        self.assertEqual((body["zoom"], body["depth"], body["max_reviews"]), (15, 1, 0))


class RankScanRoutesTests(unittest.TestCase):
    def test_new_routes_exist_and_require_api_key(self):
        paths = {getattr(r, "path", ""): r for r in wrapper.app.routes}
        for path in ("/api/rank-scans", "/api/rank-scans/{scan_id}", "/api/places", "/api/places/{job_id}"):
            self.assertIn(path, paths)
            self.assertIn(wrapper.require_api_key, [d.call for d in paths[path].dependant.dependencies], path)


class PlaceBodyTests(unittest.TestCase):
    def test_place_body_maps_to_direct_place_job(self):
        body = wrapper._place_job_body({"place_id": " ChIJk9X1Td-5yhQRiqAw0Mp4z3g ", "name": "ONC", "max_reviews": 50})
        self.assertEqual(body["place_ids"], ["ChIJk9X1Td-5yhQRiqAw0Mp4z3g"])
        self.assertEqual((body["lang"], body["depth"], body["max_reviews"]), ("tr", 1, 50))
        self.assertTrue(body["extra_reviews"])
        self.assertFalse(wrapper._place_job_body({"place_id": "ChIJk9X1Td-5yhQR", "max_reviews": 0})["extra_reviews"])
        self.assertNotIn("keywords", body)

    def test_place_body_validation(self):
        for body in ({}, {"place_id": "x y"}, {"place_id": "ChIJk9X1Td-5yhQR", "max_reviews": 501}):
            with self.assertRaises(HTTPException) as ctx:
                wrapper._place_job_body(body)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_pick_place_prefers_matching_place_id(self):
        rows = [{"place_id": "other", "title": "A"}, {"place_id": "want", "title": "B"}]
        self.assertEqual(wrapper._pick_place(rows, "want")["title"], "B")
        self.assertIsNone(wrapper._pick_place(rows, "missing"))
        self.assertIsNone(wrapper._pick_place([], "want"))


if __name__ == "__main__":
    unittest.main()
