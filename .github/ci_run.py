"""Запустить команду; при ошибке — хвост её вывода в аннотацию GitHub Actions.

Журналы задач лежат в отдельном хранилище, которое не всегда доступно; аннотации видны на странице
запуска и через API. Запуск: python .github/ci_run.py <команда> [аргументы…]
"""

import subprocess
import sys


def esc(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(cmd: list[str]) -> int:
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = r.stdout.decode("utf-8", errors="replace")
    sys.stdout.buffer.write(r.stdout)
    sys.stdout.flush()
    if r.returncode:
        tail = "\n".join(out.strip().splitlines()[-40:])
        print(f"::error title={esc(' '.join(cmd)[:150])} (код {r.returncode})::{esc(tail[-4000:])}")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
