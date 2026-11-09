"""Offline contract tests; fake Docker responses do not validate PostgreSQL itself."""

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("schedule_backup", Path(__file__).with_name("schedule-backup.py"))
assert SPEC is not None and SPEC.loader is not None
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / ".env").write_text("DB_PASSWORD=example-only\n", encoding="utf-8")
        (self.root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.out = Path(self.temp.name) / "backups"
        self.calls = []

    def fake_run(self, args, *, cwd, stdout=None, stdin=None):
        self.calls.append(args)
        if args[0] == "git":
            return SimpleNamespace(stdout=b"a" * 40 if args[1] == "rev-parse" else b"")
        if "pg_dump" in args[-1]:
            stdout.write(b"PGDMP\x00\xff\r\n\x00example")
            return SimpleNamespace(stdout=None)
        if args[-1] == "--list":
            self.assertIsNotNone(stdin)
            return SimpleNamespace(stdout=b"; fake test archive table of contents\n")
        if args[-1] == "config":
            return SimpleNamespace(stdout=b"services: {}\n")
        raise AssertionError(f"Unexpected command: {args}")

    def test_archive_preserves_binary_dump_configuration_and_hashes(self):
        with patch.object(backup, "run", side_effect=self.fake_run):
            path = backup.create_backup(self.root, "raspisanie", self.out)
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("database.dump"), b"PGDMP\x00\xff\r\n\x00example")
            self.assertEqual(archive.read("config/.env"), (self.root / ".env").read_bytes())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["project"], "raspisanie")
            self.assertFalse(manifest["encrypted"])
            for line in archive.read("SHA256SUMS.txt").decode().splitlines():
                digest, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)
        compose_calls = [c for c in self.calls if c[0] == "docker"]
        self.assertTrue(all(c[c.index("-p") + 1] == "raspisanie" for c in compose_calls))

    def test_failed_dump_does_not_leave_completed_archive_or_delete_previous_copy(self):
        self.out.mkdir()
        previous = self.out / "previous.zip"
        previous.write_bytes(b"previous backup")
        with (
            patch.object(backup, "run", side_effect=subprocess.CalledProcessError(1, ["docker"])),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            backup.create_backup(self.root, "raspisanie", self.out)
        self.assertEqual(list(self.out.iterdir()), [previous])
        self.assertEqual(previous.read_bytes(), b"previous backup")

    def test_unreadable_archive_is_not_reported_as_success(self):
        def fail_toc(args, **kwargs):
            if args[-1] == "--list":
                raise subprocess.CalledProcessError(1, args)
            return self.fake_run(args, **kwargs)

        with patch.object(backup, "run", side_effect=fail_toc), self.assertRaises(subprocess.CalledProcessError):
            backup.create_backup(self.root, "raspisanie", self.out)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_invalid_project_never_calls_docker(self):
        with patch.object(backup, "run") as mocked, self.assertRaises(ValueError):
            backup.create_backup(self.root, "wrong project", self.out)
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
