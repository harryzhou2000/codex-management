"""Guards for the deletion path: never delete without a verified archive in place."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_session_backup import backup


class DeletePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "rollout-2026-08-01T00-00-00-abc.jsonl"
        self.src.write_bytes(b"hello world")
        self.size = self.src.stat().st_size
        self.row = {
            "session_id": "abc",
            "rollout_path": str(self.src),
            "size_bytes": self.size,
            "last_activity": "2026-08-01 00:00",
            "role_label": "sub d1",
            "tree_root": "root",
            "thread_depth": 1,
        }
        self.archive = self.root / "2026-08" / "abc.jsonl.zst"
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_bytes(b"fake archive")
        self.entry = {
            "session_id": "abc",
            "verified": True,
            "src_size": self.size,
            "archive_path": str(self.archive),
            "decoded_sha256": "hash",
            "deleted_source": False,
        }
        self.ledger = {"abc": dict(self.entry)}

    def tearDown(self):
        self.tmp.cleanup()

    def _records(self) -> list[dict]:
        path = self.root / backup.LEDGER_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _stub_delete(self):
        """Pretend codex delete removed the rollout, as the real CLI is expected to."""

        def fake_run(*args, **kwargs):
            if self.src.exists():
                self.src.unlink()
            return mock.Mock(returncode=0, stdout="Deleted session abc", stderr="")

        return fake_run

    def test_archive_reuse_skips_when_delete_not_requested(self):
        out = backup.backup_one(self.row, archive_root=self.root, ledger=self.ledger, apply=True)
        self.assertEqual(out["action"], "skipped:already-backed-up")
        self.assertTrue(self.src.exists())
        self.assertEqual(self._records(), [])

    def test_delete_reuses_existing_archive(self):
        with (
            mock.patch.object(backup, "decoded_sha256", return_value="hash"),
            mock.patch.object(backup.subprocess, "run", side_effect=self._stub_delete()) as run,
        ):
            out = backup.backup_one(
                self.row, archive_root=self.root, ledger=self.ledger, apply=True, delete=True
            )
        self.assertEqual(out["action"], "deleted:archive-reused")
        self.assertEqual(run.call_args.args[0], ["codex", "delete", "--force", "abc"])
        self.assertFalse(self.src.exists())
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["deleted_source"])
        self.assertTrue(records[0]["reused_archive"])

    def test_reports_incomplete_delete(self):
        with (
            mock.patch.object(backup, "decoded_sha256", return_value="hash"),
            mock.patch.object(
                backup.subprocess, "run",
                return_value=mock.Mock(returncode=0, stdout="ok", stderr=""),
            ),
        ):
            out = backup.backup_one(
                self.row, archive_root=self.root, ledger=self.ledger, apply=True, delete=True
            )
        # The stubbed command never removed the file, so the tool must not claim it did.
        self.assertEqual(out["action"], "delete-incomplete:file-remains:archive-reused")
        records = self._records()
        self.assertFalse(records[0]["deleted_source"])
        self.assertTrue(records[0]["source_still_present"])

    def test_missing_archive_refuses_delete(self):
        self.ledger["abc"] = dict(self.entry, archive_path=str(self.root / "gone.zst"))
        with mock.patch.object(backup.subprocess, "run") as run:
            out = backup.backup_one(
                self.row, archive_root=self.root, ledger=self.ledger, apply=True, delete=True
            )
        self.assertEqual(out["action"], "failed:archive-missing")
        run.assert_not_called()
        self.assertTrue(self.src.exists())

    def test_hash_mismatch_refuses_delete(self):
        with (
            mock.patch.object(backup, "decoded_sha256", return_value="different"),
            mock.patch.object(backup.subprocess, "run") as run,
        ):
            out = backup.backup_one(
                self.row, archive_root=self.root, ledger=self.ledger, apply=True, delete=True
            )
        self.assertEqual(out["action"], "failed:archive-hash-mismatch")
        run.assert_not_called()
        self.assertTrue(self.src.exists())

    def test_fresh_archive_then_delete(self):
        self.ledger = {}
        self.archive.unlink()

        def fake_compress(src, dst, level=6):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"fake archive")

        with (
            mock.patch.object(backup.util, "sha256_file", return_value="hash"),
            mock.patch.object(backup, "decoded_sha256", return_value="hash"),
            mock.patch.object(backup, "compress", side_effect=fake_compress),
            mock.patch.object(backup.subprocess, "run", side_effect=self._stub_delete()),
        ):
            out = backup.backup_one(
                self.row, archive_root=self.root, ledger=self.ledger, apply=True, delete=True
            )
        self.assertEqual(out["action"], "deleted:just-archived")
        self.assertFalse(self.src.exists())
        self.assertTrue(self._records()[0]["deleted_source"])


if __name__ == "__main__":
    unittest.main()
