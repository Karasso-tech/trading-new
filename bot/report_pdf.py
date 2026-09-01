"""Renders a saved .md report (Hebrew, RTL) into a PDF the user can open on their
phone -- reuses the same RTL/bidi fix already confirmed working in widget_template.html
(unicode-bidi:plaintext on table cells), since numbers/percentages inside RTL Hebrew
text have the same "-4.72% renders as 4.72%-" bug in a PDF as they did in the widget."""

from __future__ import annotations

import markdown as md
from playwright.sync_api import sync_playwright

_ACCENT = "#1a5f7a"       # teal-blue accent, matches the widget's --blue tone
_ACCENT_DARK = "#0d3b4a"

_CSS = f"""
@page {{ margin: 18mm 16mm; }}
body {{
  direction: rtl; text-align: right; font-family: 'Segoe UI', Arial, sans-serif;
  font-size: 13px; line-height: 1.7; color: #1f2429;
}}
h1 {{
  font-size: 21px; color: {_ACCENT_DARK}; border-bottom: 3px solid {_ACCENT};
  padding-bottom: 8px; margin-bottom: 4px; letter-spacing: -0.2px;
}}
h2 {{
  font-size: 15.5px; color: {_ACCENT_DARK}; margin-top: 26px; margin-bottom: 10px;
  padding: 6px 10px; background: #eef5f7; border-right: 4px solid {_ACCENT};
  border-radius: 3px;
}}
h3 {{ font-size: 13.5px; margin-top: 18px; color: {_ACCENT_DARK}; }}
table {{
  border-collapse: collapse; width: 100%; margin: 10px 0 16px; direction: rtl;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}}
th, td {{
  border: 1px solid #dde3e6; padding: 6px 9px; text-align: center; font-size: 11px;
  unicode-bidi: plaintext;
}}
th {{
  background: {_ACCENT}; color: #fff; font-weight: 700; font-size: 10.5px;
}}
tr:nth-child(even) td {{ background: #f7fafb; }}
tr:hover td {{ background: #eef5f7; }}
strong {{ color: {_ACCENT_DARK}; }}
hr {{ border: none; border-top: 1.5px dashed #c8d2d6; margin: 20px 0; }}
/* p, li deliberately NOT unicode-bidi:plaintext (2026-07-13, found real: section
   ה's paragraphs each start with a bolded English ticker, e.g. "**QQQ (Core):**"
   -- plaintext picks the paragraph's base direction from its first STRONG
   character, so that leading Latin "Q" flipped the whole Hebrew paragraph to
   LTR, breaking right-alignment and garbling the Hebrew/English mix. Plain
   inherited direction:rtl (no override) is the correct/standard behavior for
   RTL prose with embedded English words -- plaintext is only needed (and kept
   below) on th/td/code, which can be PURELY a signed number with zero strong
   characters of either direction. */
code {{
  unicode-bidi: plaintext; background: #eef5f7; padding: 1px 5px; border-radius: 3px;
  font-family: Consolas, monospace; font-size: 11px; color: {_ACCENT_DARK};
}}
ul, ol {{ padding-inline-start: 20px; }}
"""


def render_report_pdf(markdown_text: str) -> bytes:
    body_html = md.markdown(markdown_text, extensions=["tables"])
    full_html = f"<html dir='rtl'><head><meta charset='utf-8'><style>{_CSS}</style></head><body>{body_html}</body></html>"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(full_html, wait_until="load")
        pdf_bytes = page.pdf(format="A4", print_background=True)
        browser.close()
    return pdf_bytes
