import sys
import types
import unittest

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


class ParseCsvRankTests(unittest.TestCase):
    def test_rows_are_ordered_by_google_rank_and_typed(self):
        # Motor satırları bitme sırasıyla yazar; çıktı Google sırasına dönmeli.
        csv_text = (
            "title,place_id,rank,sponsored\n"
            "C,p3,3,false\n"
            "Reklam,p0,0,true\n"
            "A,p1,1,false\n"
            "B,p2,2,false\n"
        )
        rows = wrapper._parse_csv(csv_text)
        self.assertEqual([r["title"] for r in rows], ["A", "B", "C", "Reklam"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3, None])
        self.assertEqual([r["sponsored"] for r in rows], [False, False, False, True])

    def test_old_engine_output_without_rank_is_unchanged(self):
        csv_text = "title,place_id\nY,p2\nX,p1\n"
        rows = wrapper._parse_csv(csv_text)
        self.assertEqual([r["title"] for r in rows], ["Y", "X"])
        self.assertNotIn("rank", rows[0])


if __name__ == "__main__":
    unittest.main()
