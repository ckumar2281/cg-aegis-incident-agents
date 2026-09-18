#!/usr/bin/env python3
"""
Render docs/RUN-SHEET.md as a single self-contained page you can open or print.

Markdown does not render in a browser and a terminal is a poor place to read a script
you are following live. This produces one HTML file with no external assets, sized for
reading beside a terminal and for printing on A4.

    python scripts/render_runsheet.py
    open docs/DEMO-DAY.html
"""

from __future__ import annotations

import io
import pathlib
import sys

try:
    import markdown
except ImportError:
    print("pip install markdown", file=sys.stderr)
    raise SystemExit(1)

SRC = pathlib.Path("docs/RUN-SHEET.md")
OUT = pathlib.Path("docs/DEMO-DAY.html")

CSS = """
:root { --ink:#14181f; --paper:#fbfbf8; --muted:#5a6470; --line:#e3e1d9;
        --green:#2e6f4e; --rust:#a03e2b; --card:#ffffff; }
* { box-sizing:border-box; }
body { margin:0; padding:40px 20px 80px; background:var(--paper); color:var(--ink);
       font:17px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
.wrap { max-width:860px; margin:0 auto; }
h1 { font-size:34px; line-height:1.15; letter-spacing:-0.5px; margin:0 0 24px; }
h2 { font-size:25px; line-height:1.2; margin:44px 0 14px; padding-top:20px;
     border-top:2px solid var(--line); }
h2:first-of-type { border-top:none; padding-top:0; }
h3 { font-size:19px; margin:26px 0 10px; }
p, li { font-size:17px; }
ul, ol { padding-left:24px; }
li { margin:5px 0; }
blockquote { margin:20px 0; padding:18px 22px; background:var(--card);
             border-left:4px solid var(--green); border-radius:0 8px 8px 0; }
blockquote h2 { font-size:21px; margin:0 0 10px; border:none; padding:0; color:var(--green); }
blockquote p:first-child { margin-top:0; }
blockquote p:last-child { margin-bottom:0; }
code { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:15px;
       background:#f0efe8; padding:2px 6px; border-radius:4px; }
pre { background:var(--ink); color:#eef1ee; padding:16px 18px; border-radius:8px;
      overflow-x:auto; line-height:1.5; }
pre code { background:none; color:inherit; padding:0; font-size:14.5px; }
table { border-collapse:collapse; width:100%; margin:18px 0; font-size:16px; }
th, td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line);
         vertical-align:top; }
th { background:#f2f1ea; font-weight:600; font-size:14px; text-transform:uppercase;
     letter-spacing:0.04em; color:var(--muted); }
hr { border:none; border-top:2px solid var(--line); margin:36px 0; }
strong { font-weight:600; }
a { color:var(--green); }
@media print {
  body { padding:0; font-size:12pt; background:#fff; }
  .wrap { max-width:none; }
  pre { background:#f4f4f2; color:#14181f; border:1px solid #ddd; }
  h2 { page-break-after:avoid; }
  pre, table, blockquote { page-break-inside:avoid; }
}
"""


def main() -> int:
    if not SRC.exists():
        print(f"missing {SRC}", file=sys.stderr)
        return 1
    body = markdown.markdown(
        io.open(SRC, encoding="utf-8").read(),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    page = (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Aegis — Demo Day</title>\n<style>" + CSS + "</style>\n</head>\n"
        "<body>\n<div class=\"wrap\">\n" + body + "\n</div>\n</body>\n</html>\n"
    )
    io.open(OUT, "w", encoding="utf-8").write(page)
    print(f"wrote {OUT}  ({len(page):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
