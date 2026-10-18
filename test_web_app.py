import tempfile
import unittest
from pathlib import Path

import web_app


class CleanupEmptySubtitleTests(unittest.TestCase):
    def test_cleanup_all_empty_subtitles_removes_files_meta_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_root = root / "subtitle_cache"
            history_root = root / "history"
            uid_dir = cache_root / "123"
            meta_dir = uid_dir / "meta"
            hist_dir = history_root / "123"
            meta_dir.mkdir(parents=True)
            hist_dir.mkdir(parents=True)

            empty = uid_dir / "Empty P2 BVEMPTY.txt"
            full = uid_dir / "Full P1 BVFULL.txt"
            empty.write_text("", encoding="utf-8")
            full.write_text("00:01 hello\n", encoding="utf-8")
            (meta_dir / "Empty P2 BVEMPTY.meta.json").write_text("{}", encoding="utf-8")
            (hist_dir / "scanned_parts.tsv").write_text(
                "BVEMPTY\t2\t202\t0\t1\nBVFULL\t1\t101\t0\t1\n",
                encoding="utf-8",
            )
            (hist_dir / "scanned.tsv").write_text(
                "BVEMPTY\t0\t1\nBVFULL\t0\t1\n",
                encoding="utf-8",
            )

            result = web_app.cleanup_all_empty_subtitles(cache_root, history_root)

            self.assertTrue(result["ok"])
            self.assertEqual(1, result["empty_files"])
            self.assertEqual(1, result["parts_removed"])
            self.assertEqual(1, result["videos_removed"])
            self.assertFalse(empty.exists())
            self.assertFalse((meta_dir / "Empty P2 BVEMPTY.meta.json").exists())
            self.assertTrue(full.exists())
            self.assertIn("BVFULL", (hist_dir / "scanned_parts.tsv").read_text(encoding="utf-8"))
            self.assertNotIn("BVEMPTY", (hist_dir / "scanned_parts.tsv").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
