#!/usr/bin/env python3
"""Build Markdown/Word/PDF deployment kit with Pandoc and XeLaTeX."""

import argparse
import hashlib
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

FILES = [
    "deployment-guide.md",
    "deployment-guide.docx",
    "deployment-guide.pdf",
    "schedule-backup.py",
    "test_backup.py",
    "Dockerfile.check",
    "compose.test.yml",
    "build-kit.py",
    "pdf-header.tex",
    "README.md",
    "README.txt",
]
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def style_docx(path, main_font, mono_font):
    """Set readable fonts, A4 page size and margins without python-docx."""
    with zipfile.ZipFile(path) as archive:
        content = {name: archive.read(name) for name in archive.namelist()}
    styles = ET.fromstring(content["word/styles.xml"])
    for style in styles.findall(f"{W}style"):
        style_id = style.get(f"{W}styleId", "")
        if style_id not in {"Normal", "BodyText", "SourceCode", "VerbatimChar"}:
            continue
        properties = style.find(f"{W}rPr")
        if properties is None:
            properties = ET.SubElement(style, f"{W}rPr")
        fonts = properties.find(f"{W}rFonts")
        if fonts is None:
            fonts = ET.SubElement(properties, f"{W}rFonts")
        code = style_id in {"SourceCode", "VerbatimChar"}
        for key in ("ascii", "hAnsi", "cs", "eastAsia"):
            fonts.set(f"{W}{key}", mono_font if code else main_font)
        size = properties.find(f"{W}sz")
        if size is None:
            size = ET.SubElement(properties, f"{W}sz")
        size.set(f"{W}val", "17" if code else "22")
    content["word/styles.xml"] = ET.tostring(styles, encoding="utf-8", xml_declaration=True)
    document = ET.fromstring(content["word/document.xml"])
    for section in document.iter(f"{W}sectPr"):
        size = section.find(f"{W}pgSz")
        if size is not None:
            size.set(f"{W}w", "11906")
            size.set(f"{W}h", "16838")
        margins = section.find(f"{W}pgMar")
        if margins is not None:
            for edge in ("top", "bottom", "left", "right"):
                margins.set(f"{W}{edge}", "1134")
    content["word/document.xml"] = ET.tostring(document, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in content.items():
            archive.writestr(name, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-font", default="Arial")
    parser.add_argument("--mono-font", default="Consolas")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    root = folder.parent.parent
    output = args.output or root / "output" / "schedule-service-deployment-kit.zip"
    for name in ("pandoc", "xelatex"):
        if not shutil.which(name):
            parser.error(f"Missing build tool: {name}")
    source = folder / "deployment-guide.md"
    docx = folder / "deployment-guide.docx"
    pdf = folder / "deployment-guide.pdf"
    base = ["pandoc", str(source), "--from=markdown", "--toc", "--toc-depth=2", "--metadata=lang:ru-RU"]
    subprocess.run([*base, "-o", str(docx)], check=True)
    style_docx(docx, args.main_font, args.mono_font)
    with tempfile.TemporaryDirectory(prefix="schedule-guide-") as temp:
        pdf_source = Path(temp) / "guide.md"
        # The PDF gets a separate cover; the source heading remains in Markdown/Word.
        pdf_source.write_text(source.read_text(encoding="utf-8").split("\n", 1)[1], encoding="utf-8")
        subprocess.run(
            [
                "pandoc",
                str(pdf_source),
                "--from=markdown",
                "--toc",
                "--toc-depth=2",
                "--metadata=lang:ru-RU",
                "--metadata=title:Сервис расписания",
                "--metadata=subtitle:Развёртывание и сопровождение",
                "--metadata=author:Руководство для студентов и сопровождающих",
                "--metadata=date:Документ 1.1 • 10 ноября 2026 • Приложение 0.14.0",
                "--pdf-engine=xelatex",
                "--include-in-header",
                str(folder / "pdf-header.tex"),
                "-V",
                f"mainfont={args.main_font}",
                "-V",
                f"monofont={args.mono_font}",
                "-V",
                "papersize=a4",
                "-V",
                "geometry:margin=19mm",
                "-V",
                "fontsize=11pt",
                "-V",
                "colorlinks=true",
                "-V",
                "linkcolor=blue",
                "-V",
                "urlcolor=blue",
                "-o",
                str(pdf),
            ],
            check=True,
        )
    hashes = []
    for name in FILES:
        digest = hashlib.sha256((folder / name).read_bytes()).hexdigest()
        hashes.append(f"{digest}  {name}")
    sums = folder / "SHA256SUMS.txt"
    sums.write_text("\n".join(hashes) + "\n", encoding="utf-8", newline="\n")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in [*FILES, sums.name]:
                archive.write(folder / name, f"schedule-service-deployment/{name}")
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("Kit ZIP integrity check failed")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"KIT OK: {output}")


if __name__ == "__main__":
    main()
