#!/usr/bin/env python3
"""Backup a selected schedule-service Compose stack. Python standard library only.

The ZIP contains secrets and personal data; it is NOT encrypted.
No restoration, rotation, deletion of old backups or application restart is performed.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path


def run(args, *, cwd, stdout=None, stdin=None):
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        stdin=stdin,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def create_backup(root, project, destination, include_caddy=False):
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if not (root / "docker-compose.yml").is_file() or not (root / ".env").is_file():
        raise ValueError("Project directory must contain docker-compose.yml and .env")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError("Specify the actual Compose project name in lowercase")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    final = destination / f"schedule_{stamp}_{uuid.uuid4().hex[:8]}.zip"
    partial = final.with_suffix(".zip.part")
    compose = [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "-p",
        project,
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="schedule-backup-", dir=destination) as temp:
            stage = Path(temp)
            dump = stage / "database.dump"
            # pg_dump's binary stdout never passes through a text shell or PowerShell pipeline.
            with dump.open("wb") as stream:
                run(
                    [
                        *compose,
                        "exec",
                        "-T",
                        "db",
                        "sh",
                        "-c",
                        'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner',
                    ],
                    cwd=root,
                    stdout=stream,
                )
            with dump.open("rb") as stream:
                if stream.read(5) != b"PGDMP":
                    raise ValueError("Dump is empty or has an unexpected format")
            # This validates the archive table of contents, not a full restoration.
            with dump.open("rb") as stream:
                toc = run([*compose, "exec", "-T", "db", "pg_restore", "--list"], cwd=root, stdin=stream)
            (stage / "database-toc.txt").write_bytes(toc.stdout)
            config_files = [
                ".env",
                "docker-compose.yml",
                "Dockerfile",
                "deploy/Caddyfile",
                "deploy/Caddyfile.nginx",
                "deploy/compose.nginx.yml",
                "deploy/NGINX.md",
                "deploy/entrypoint.sh",
                "requirements-lock.txt",
                "requirements-dev-lock.txt",
                "frontend/package-lock.json",
            ]
            for name in config_files:
                source = root / name
                if source.is_file():
                    target = stage / "config" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
            resolved = run([*compose, "config"], cwd=root)
            (stage / "compose.resolved.yml").write_bytes(resolved.stdout)
            metadata = {
                "created_utc": stamp,
                "project": project,
                "project_dir": str(root),
                "caddy_data_included": include_caddy,
                "encrypted": False,
                "database_check": "PGDMP header and pg_restore --list; restore drill still required",
            }
            try:
                metadata["git_commit"] = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.decode().strip()
                status = run(["git", "status", "--porcelain"], cwd=root).stdout.decode("utf-8", errors="replace")
                metadata["git_dirty"] = bool(status.strip())
                (stage / "git-status.txt").write_text(status, encoding="utf-8")
            except (subprocess.CalledProcessError, FileNotFoundError):
                metadata["git_commit"] = "unavailable; record release manually"
            if include_caddy:
                cid = run([*compose, "ps", "-a", "-q", "caddy"], cwd=root).stdout.decode().strip()
                if not cid or "\n" in cid:
                    raise ValueError("Expected exactly one Caddy container for TLS state backup")
                run(["docker", "cp", f"{cid}:/data", str(stage / "caddy-data")], cwd=root)
            (stage / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            hashes = []
            for path in sorted(stage.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Symbolic links are not accepted in this backup")
                if path.is_file():
                    with path.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    hashes.append(f"{digest}  {path.relative_to(stage).as_posix()}")
            (stage / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
            with partial.open("xb") as stream:
                with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(stage.rglob("*")):
                        if path.is_file():
                            archive.write(path, path.relative_to(stage).as_posix())
            with zipfile.ZipFile(partial) as archive:
                if archive.testzip() is not None:
                    raise ValueError("ZIP integrity check failed")
            os.chmod(partial, 0o600)
            partial.rename(final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default="/opt/raspisanie")
    parser.add_argument("--project", required=True, help="Actual Compose project name, e.g. raspisanie")
    parser.add_argument("--output-dir", default="/var/backups/raspisanie")
    parser.add_argument("--include-caddy-data", action="store_true", help="Also copy /data (TLS keys) from Caddy")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        path = create_backup(args.project_dir, args.project, args.output_dir, args.include_caddy_data)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        # Avoid printing command stderr: provider/Compose diagnostics may contain secrets.
        print(f"BACKUP FAILED ({type(exc).__name__}); no completed archive was created.", file=sys.stderr)
        return 1
    print(f"BACKUP OK: {path}")
    print("Contains secrets; NOT encrypted. Copy off-host and perform a restore drill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
