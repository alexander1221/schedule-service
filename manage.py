#!/usr/bin/env python3
"""Точка входа Django: python manage.py <команда>. Список команд проекта — docs/WEB.md."""

import os
import sys


def _utf8_output() -> None:
    """Вывод в UTF-8. На Windows, когда вывод уходит в файл или другую программу, Python пишет в кодировке
    системы (cp1251/cp1252) и падает на кириллице и значках (UnicodeEncodeError). В консоли Windows
    вывод и так UTF-8 — там ничего не меняется."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")


def main() -> None:
    _utf8_output()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
