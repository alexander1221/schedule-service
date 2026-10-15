#!/usr/bin/env python3
"""Точка входа Django: python manage.py <команда>. Список команд проекта — docs/WEB.md."""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
