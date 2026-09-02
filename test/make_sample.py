#!/usr/bin/env python3
"""検証用テストPDF生成スクリプト。
「Chapter 1」〜「Chapter 4」を各5ページ、計20ページ作る。
日本語フォント未対応環境向けに見出しは英語表記。
"""
from fpdf import FPDF

OUT_PATH = "sample.pdf"

pdf = FPDF()
pdf.set_auto_page_break(auto=False)

for chapter in range(1, 5):
    for page_in_chapter in range(1, 6):
        pdf.add_page()
        pdf.set_font("Helvetica", size=20)
        if page_in_chapter == 1:
            pdf.set_xy(10, 20)
            pdf.multi_cell(0, 12, f"Chapter {chapter}: Sample Topic {chapter}")
        pdf.set_font("Helvetica", size=12)
        pdf.set_xy(10, 60)
        pdf.multi_cell(
            0,
            8,
            f"This is slide number {page_in_chapter} in this section.\n"
            f"Dummy body text for testing the splitter tool, part {chapter}.\n"
            f"Line A / Line B / Line C for this topic.",
        )

pdf.output(OUT_PATH)
print(f"wrote {OUT_PATH}, pages = {pdf.page_no()}")
