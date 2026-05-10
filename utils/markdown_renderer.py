"""Markdown 渲染工具，支持表格等扩展，带 CSS 样式。"""

from __future__ import annotations

import markdown


def render_markdown_to_html(md_text: str) -> str:
    """
    将 Markdown 文本渲染为带样式的 HTML。

    Args:
        md_text: Markdown 格式的文本

    Returns:
        完整的 HTML 文档字符串，包含 CSS 样式
    """
    html_body = markdown.markdown(md_text, extensions=['tables'])

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 14px; line-height: 1.6; color: #333; }}
h1 {{ font-size: 18px; color: #1d1d1f; margin: 16px 0 12px 0; }}
h2 {{ font-size: 16px; color: #1d1d1f; margin: 14px 0 10px 0; }}
h3 {{ font-size: 15px; color: #1d1d1f; margin: 12px 0 8px 0; }}
p {{ margin: 8px 0; }}
ul, ol {{ margin: 8px 0; padding-left: 24px; }}
li {{ margin: 4px 0; display: list-item; }}
table {{ border-collapse: collapse; margin: 12px 0; width: 100%; }}
th, td {{ border: 1px solid #d1d1d6; padding: 8px 12px; text-align: left; }}
th {{ background-color: #f5f5f7; font-weight: 600; }}
strong {{ font-weight: 600; color: #1d1d1f; }}
hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 16px 0; }}
</style>
</head>
<body>{html_body}</body>
</html>"""
