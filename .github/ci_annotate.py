"""Упавшие тесты из отчёта pytest (JUnit XML) → аннотации GitHub Actions.

Аннотации видны прямо на странице запуска (и через API), без скачивания журналов.
Запуск: python .github/ci_annotate.py pytest-report.xml
"""

import sys
import xml.etree.ElementTree as ET


def esc(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: str) -> None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as e:
        print(f"::warning title=Отчёт pytest не прочитан::{esc(str(e))}")
        return
    n = 0
    for case in root.iter("testcase"):
        for bad in list(case.findall("failure")) + list(case.findall("error")):
            n += 1
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            text = (bad.get("message") or "") + "\n" + (bad.text or "")
            tail = "\n".join(text.strip().splitlines()[-25:])  # хвост трассировки — там причина
            print(f"::error title={esc(name[:200])}::{esc(tail[:3000])}")
    print(f"Упавших тестов: {n}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # журнал GitHub — UTF-8; иначе cp1252 на Windows
    main(sys.argv[1])
