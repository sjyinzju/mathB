# -*- coding: utf-8 -*-
"""Audit helper: extract official PDF texts (problem statement + format spec)."""
import io
import sys
from pathlib import Path

import PyPDF2

ROOT = Path(__file__).resolve().parent.parent
ATT = ROOT / "2026年度“策联杯”数学建模精英联赛-B题-附件"
OUT = ROOT / "outputs" / "audit"
OUT.mkdir(parents=True, exist_ok=True)

sys.stdout.reconfigure(encoding="utf-8")

targets = {
    "problem_statement.txt": ROOT / "2026年度“策联杯”数学建模精英联赛-B题.pdf",
    "format_spec.txt": ATT / "2026年度“策联杯”数学建模精英联赛-论文格式规范.pdf",
}

for name, path in targets.items():
    reader = PyPDF2.PdfReader(str(path))
    buf = io.StringIO()
    for i, page in enumerate(reader.pages):
        buf.write(f"\n===== PAGE {i + 1} =====\n")
        buf.write(page.extract_text() or "")
    text = buf.getvalue()
    (OUT / name).write_text(text, encoding="utf-8")
    print(f"{name}: {len(reader.pages)} pages, {len(text)} chars")
