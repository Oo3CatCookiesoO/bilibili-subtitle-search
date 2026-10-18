import unittest
import tempfile
from pathlib import Path

import bili_sub_search as core


class UploadFilterTests(unittest.TestCase):
    def test_charge_upload_filter_adds_charging_special_type(self):
        client = core.BiliClient()
        captured = {}

        def fake_request_json(url, params=None, **kwargs):
            captured["url"] = url
            captured["params"] = dict(params or {})
            return {"data": {"page": {"count": 0}, "list": {"vlist": []}}}

        client.request_json = fake_request_json

        list(
            client.get_uploader_videos(
                "123",
                upload_filter="charge",
            )
        )

        self.assertEqual("charging", captured["params"].get("special_type"))
        self.assertEqual(0, captured["params"].get("tid"))
        self.assertEqual(0, captured["params"].get("index"))
        self.assertEqual("true", captured["params"].get("order_avoided"))

    def test_default_upload_filter_does_not_add_special_type(self):
        client = core.BiliClient()
        captured = {}

        def fake_request_json(url, params=None, **kwargs):
            captured["params"] = dict(params or {})
            return {"data": {"page": {"count": 0}, "list": {"vlist": []}}}

        client.request_json = fake_request_json

        list(client.get_uploader_videos("123"))

        self.assertNotIn("special_type", captured["params"])


class ScanHistoryTests(unittest.TestCase):
    def test_legacy_video_history_without_part_history_skips_whole_bvid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uid_dir = root / "123"
            uid_dir.mkdir()
            (uid_dir / "scanned.tsv").write_text("BVOLD\t0\t1\n", encoding="utf-8")

            history = core.ScanHistory(root, "123")
            pages = [
                core.Page(page=1, part="P1", cid=101),
                core.Page(page=2, part="P2", cid=102),
            ]

            self.assertEqual([], history.filter_unscanned_pages("BVOLD", pages))

    def test_part_history_stays_granular_even_when_bvid_history_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uid_dir = root / "123"
            uid_dir.mkdir()
            (uid_dir / "scanned.tsv").write_text("BVPART\t0\t1\n", encoding="utf-8")
            (uid_dir / "scanned_parts.tsv").write_text("BVPART\t1\t101\t0\t1\n", encoding="utf-8")

            history = core.ScanHistory(root, "123")
            pages = [
                core.Page(page=1, part="P1", cid=101),
                core.Page(page=2, part="P2", cid=102),
            ]

            self.assertEqual([pages[1]], history.filter_unscanned_pages("BVPART", pages))


class VideoLogTests(unittest.TestCase):
    def test_video_log_suffix_includes_page_item_index(self):
        video = core.Video(
            title="demo",
            bvid="BVTEST",
            pubdate=1780000000,
            list_page=3,
            list_index=8,
        )

        text = core.video_log_suffix(video)

        self.assertIn("第 3 页", text)
        self.assertIn("本页第 8 个", text)


if __name__ == "__main__":
    unittest.main()
