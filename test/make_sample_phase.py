#!/usr/bin/env python3
"""検証用テストPDF生成スクリプト（Phase違い・追加キーワードのテスト用）。

8ページ構成:
  1: 表紙
  2: STEP 01 | Phase A
  3: STEP 01 | Phase A-1
  4: STEP 01 | Phase B
  5: STEP 02 | Phase A
  6: STEP 02 | Q&A （本文に "see STEP 01" を含む）
  7: SYSTEM | Support
  8: SYSTEM | FAQ

期待:
  追加語なし  -> 区切り 1, 2, 5           (3章)
  追加語=SYSTEM -> 区切り 1, 2, 5, 7      (4章)
"""
from fpdf import FPDF

OUT_PATH = "sample_phase.pdf"

TITLES = [
    None,
    "STEP 01 | Phase A",
    "STEP 01 | Phase A-1",
    "STEP 01 | Phase B",
    "STEP 02 | Phase A",
    "STEP 02 | Q&A",
    "SYSTEM | Support",
    "SYSTEM | FAQ",
]

pdf = FPDF()
pdf.set_auto_page_break(auto=False)

for i, title in enumerate(TITLES, start=1):
    pdf.add_page()
    pdf.set_font("Helvetica", size=20)
    if title:
        pdf.set_xy(10, 20)
        pdf.multi_cell(0, 12, title)
    else:
        pdf.set_xy(10, 20)
        pdf.multi_cell(0, 12, "Cover / Table of Contents")
    pdf.set_font("Helvetica", size=12)
    pdf.set_xy(10, 60)
    body = f"Dummy body text for page {i} of the phase-splitting test."
    if i == 6:
        body += "\nFor a recap, see STEP 01 for the basics."
    pdf.multi_cell(0, 8, body)

pdf.output(OUT_PATH)
print(f"wrote {OUT_PATH}, pages = {pdf.page_no()}")

# --- sample_phase2.pdf: 本文中に別章番号への言及がある場合の見出し判定用 -----------------
# ページ2の本文に "see also STEP 02"、ページ3の本文（見出しより後ろ・80文字目以降）に
# "STEP 02" への言及を追加。見出し判定は先頭60文字だけを見るため、これらは
# 章の区切りとして拾われず、期待される区切りは sample_phase.pdf と同じになるはず。
OUT_PATH2 = "sample_phase2.pdf"

pdf2 = FPDF()
pdf2.set_auto_page_break(auto=False)

for i, title in enumerate(TITLES, start=1):
    pdf2.add_page()
    pdf2.set_font("Helvetica", size=20)
    if title:
        pdf2.set_xy(10, 20)
        pdf2.multi_cell(0, 12, title)
    else:
        pdf2.set_xy(10, 20)
        pdf2.multi_cell(0, 12, "Cover / Table of Contents")
    pdf2.set_font("Helvetica", size=12)
    pdf2.set_xy(10, 60)
    body = f"Dummy body text for page {i} of the phase-splitting test."
    if i == 2:
        body += "\nBy the way, see also STEP 02 for a related topic."
    if i == 3:
        # 見出しより後ろ・80文字目以降に "STEP 02" への言及を置くための埋め草
        padding = "x" * 80
        body += f"\n{padding} note: this table references STEP 02 in a footnote."
    if i == 6:
        body += "\nFor a recap, see STEP 01 for the basics."
    pdf2.multi_cell(0, 8, body)

pdf2.output(OUT_PATH2)
print(f"wrote {OUT_PATH2}, pages = {pdf2.page_no()}")
