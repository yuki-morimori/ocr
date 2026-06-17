# -*- coding: utf-8 -*-
"""帳票生成（全業種共通）。

構造化データ（core.extract の戻り）＋業種設定 → 帳票HTML。
業種ごとの違いは設定の form / header_fields / line_items が吸収するので、
このコードは業種に依存しない。HTMLで返すのでそのままブラウザ表示・印刷できる。
"""
from __future__ import annotations

from html import escape


def _fmt(value, vtype: str) -> str:
    if value is None or value == "":
        return '<span class="empty">—</span>'
    if vtype == "number":
        try:
            num = float(value)
            num = int(num) if num == int(num) else num
            return f"{num:,}"
        except (TypeError, ValueError):
            return escape(str(value))
    return escape(str(value))


def _sum_column(rows: list[dict], key: str):
    total = 0.0
    found = False
    for r in rows:
        v = r.get(key)
        try:
            total += float(v)
            found = True
        except (TypeError, ValueError):
            continue
    if not found:
        return None
    return int(total) if total == int(total) else total


def _flag_class(key: str, review: set, low: set) -> str:
    """項目キーが要人間確認/低自信度なら、ハイライト用のCSSクラスを返す。"""
    if key in review:
        return " review"
    if key in low:
        return " low-conf"
    return ""


def render_form(cfg: dict, data: dict) -> str:
    """業種設定＋構造化データから帳票HTMLを生成する。"""
    form = cfg.get("form", {})
    title = form.get("title", cfg["display_name"])
    subtitle = form.get("subtitle", "")
    review = set(data.get("needs_human_review") or [])
    low = {k for k, v in (data.get("confidence") or {}).items() if str(v).lower() == "low"}

    # ヘッダ項目（2列のラベル/値テーブル）
    header_rows = ""
    for f in cfg["header_fields"]:
        val = _fmt(data.get(f["key"]), f.get("type", "text"))
        cls = _flag_class(f["key"], review, low)
        header_rows += (
            f'<tr><th>{escape(f["label"])}</th><td class="kvval{cls}">{val}</td></tr>'
        )

    # 明細テーブル
    li = cfg["line_items"]
    cols = li["columns"]
    thead = "".join(f"<th>{escape(c['label'])}</th>" for c in cols)
    body_rows = ""
    rows = data.get(li["key"]) or []
    for r in rows:
        cells = "".join(
            f'<td class="{"num " if c.get("type")=="number" else ""}'
            f'{_flag_class(c["key"], review, low).strip()}">'
            f'{_fmt(r.get(c["key"]), c.get("type","text"))}</td>'
            for c in cols
        )
        body_rows += f"<tr>{cells}</tr>"
    if not rows:
        body_rows = f'<tr><td colspan="{len(cols)}" class="empty">明細なし</td></tr>'

    # 合計行（sum:true の数値列）
    sum_cells = ""
    has_sum = any(c.get("sum") for c in cols)
    if has_sum and rows:
        for i, c in enumerate(cols):
            if i == 0:
                sum_cells += '<td class="total-label">合計</td>'
            elif c.get("sum"):
                total = _sum_column(rows, c["key"])
                sum_cells += f'<td class="num total">{_fmt(total, "number")}</td>'
            else:
                sum_cells += "<td></td>"
    tfoot = f"<tr class='sumrow'>{sum_cells}</tr>" if sum_cells else ""

    note = form.get("summary_note", "")
    subtitle_html = f'<p class="subtitle">{escape(subtitle)}</p>' if subtitle else ""
    note_html = f'<p class="form-note">※ {escape(note)}</p>' if note else ""

    return f"""
<div class="paper">
  <div class="paper-head">
    <div class="badge">{escape(cfg['display_name'])}</div>
    <h2>{escape(title)}</h2>
    {subtitle_html}
  </div>
  <table class="kv">{header_rows}</table>
  <table class="lines">
    <thead><tr>{thead}</tr></thead>
    <tbody>{body_rows}</tbody>
    <tfoot>{tfoot}</tfoot>
  </table>
  {note_html}
</div>
""".strip()
