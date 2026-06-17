# -*- coding: utf-8 -*-
"""集計レポート（全業種共通・設定駆動）。

確定データ（複数伝票）を『軸（会社・車番・乗務員 等）』ごとに集計する内部コスト把握用。
請求書（顧客へ）とは別軸で、「どの会社が・どの車が・どれだけ経費/距離/重量か」を出す。

各業種JSONの `analytics` が軸(dimensions)と集計値(measures)を決める：
  - measures は header/line から sum/count、さらに派生(derived)で合算・比率も出せる
    例: 車両コスト計 = 立替＋燃料（sum）、燃費 = 走行距離÷給油量（ratio）
  - 会社×車番のクロス、月推移(trend) も自動で付く

公開API:
    aggregate(cfg, results, month=None) -> report
    render_html(cfg, report) -> str
    build_xlsx(cfg, report, path) -> None
"""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import IO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_HEAD = PatternFill("solid", fgColor="1F2A3A")
_TOT = PatternFill("solid", fgColor="EAF2FF")
_UNKNOWN = "（不明）"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _in_month(date_val, month: str | None) -> bool:
    if not month:
        return True
    return isinstance(date_val, str) and date_val.startswith(month)


def _dim_value(slip: dict, key: str) -> str:
    if key == "__month":
        d = slip.get("date")
        return d[:7] if isinstance(d, str) and len(d) >= 7 else _UNKNOWN
    v = slip.get(key)
    return v if v not in (None, "") else _UNKNOWN


def _is_derived(m: dict) -> bool:
    return "derived" in m


def _slip_measure(cfg: dict, slip: dict, m: dict) -> float:
    if m.get("agg") == "count":
        return 1
    if m.get("source") == "line":
        li = cfg["line_items"]["key"]
        return sum(_num(r.get(m["key"])) or 0 for r in slip.get(li) or [])
    return _num(slip.get(m["key"])) or 0


def _apply_derived(measures: list[dict], base: dict) -> dict:
    """合算した base から派生メジャー（合算・比率）を計算して足す。"""
    out = dict(base)
    for m in measures:
        if not _is_derived(m):
            continue
        d = m["derived"]
        if d["op"] == "sum":
            out[m["key"]] = sum(base.get(k, 0) for k in d["of"])
        elif d["op"] == "ratio":
            den = base.get(d["den"], 0)
            out[m["key"]] = round((base.get(d["num"], 0) / den) if den else 0, d.get("round", 1))
    return out


def _sort_key(measures: list[dict]) -> str:
    for m in measures:
        if m.get("agg") != "count" and not _is_derived(m):
            return m["key"]
    return measures[0]["key"]


def aggregate(cfg: dict, results: list[dict], month: str | None = None) -> dict:
    """確定データ → 軸ごとの集計＋会社×車番クロス＋月推移。月で絞り込み可。"""
    a = cfg["analytics"]
    measures = a["measures"]
    base_measures = [m for m in measures if not _is_derived(m)]
    dims = a["dimensions"]
    rows_in = [s for s in results if _in_month(s.get("date"), month)]
    sort_k = _sort_key(measures)

    def _group(keyfunc):
        groups: dict = {}
        for s in rows_in:
            k = keyfunc(s)
            g = groups.setdefault(k, {m["key"]: 0.0 for m in base_measures})
            for m in base_measures:
                g[m["key"]] += _slip_measure(cfg, s, m)
        return groups

    def _rows(groups, single=True):
        rows = []
        for k, base in groups.items():
            full = _apply_derived(measures, base)
            rows.append({("value" if single else "key"): k, "measures": full})
        return rows

    out_dims = []
    for d in dims:
        groups = _group(lambda s, k=d["key"]: _dim_value(s, k))
        rows = [{"value": k, "measures": _apply_derived(measures, v)} for k, v in groups.items()]
        rows.sort(key=lambda r: -r["measures"].get(sort_k, 0))
        base_tot = {m["key"]: sum(g[m["key"]] for g in groups.values()) for m in base_measures}
        out_dims.append({"key": d["key"], "label": d["label"], "rows": rows,
                         "totals": _apply_derived(measures, base_tot)})

    cross = None
    if len(dims) >= 2:
        a0, b0 = dims[0], dims[1]
        groups = _group(lambda s: (_dim_value(s, a0["key"]), _dim_value(s, b0["key"])))
        rows = [{"a": k[0], "b": k[1], "measures": _apply_derived(measures, v)} for k, v in groups.items()]
        rows.sort(key=lambda r: (r["a"], -r["measures"].get(sort_k, 0)))
        cross = {"a_label": a0["label"], "b_label": b0["label"], "rows": rows}

    # 月推移（date[:7] で集計、昇順）
    tgroups = _group(lambda s: _dim_value(s, "__month"))
    trend = [{"month": k, "measures": _apply_derived(measures, v)} for k, v in tgroups.items()]
    trend.sort(key=lambda r: r["month"])

    return {
        "month": month or "全期間",
        "slip_count": len(rows_in),
        "measures": [{"key": m["key"], "label": m["label"], "unit": m.get("unit", ""),
                      "derived": _is_derived(m)} for m in measures],
        "dimensions": out_dims,
        "cross": cross,
        "trend": trend,
    }


# ── 表示（HTML）────────────────────────────────────────────────────────────────
def _fmt(v, unit: str) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return escape(str(v))
    s = f"{int(n):,}" if n == int(n) else f"{n:,.1f}"
    return f"{s}{unit}"


def _table(measures, head_labels, body_rows) -> str:
    th = "".join(f"<th>{escape(h)}</th>" for h in head_labels)
    th += "".join(f'<th>{escape(m["label"])}</th>' for m in measures)
    return (f'<table class="lines"><thead><tr>{th}</tr></thead>'
            f'<tbody>{body_rows}</tbody></table>')


def _mrow(measures, md):
    return "".join(f'<td class="num">{_fmt(md[m["key"]], m["unit"])}</td>' for m in measures)


def render_html(cfg: dict, rep: dict) -> str:
    measures = rep["measures"]
    blocks = [f'<p class="subtitle">対象期間: {escape(rep["month"])} ／ 伝票 {rep["slip_count"]} 枚</p>']
    for d in rep["dimensions"]:
        body = "".join(f'<tr><td>{escape(str(r["value"]))}</td>{_mrow(measures, r["measures"])}</tr>'
                       for r in d["rows"])
        body += f'<tr class="sumrow"><td class="total-label">合計</td>' + "".join(
            f'<td class="num total">{_fmt(d["totals"][m["key"]], m["unit"])}</td>' for m in measures) + "</tr>"
        blocks.append(f'<h3 class="rep-h">{escape(d["label"])}別</h3>' + _table(measures, [d["label"]], body))
    if rep["cross"]:
        cx = rep["cross"]
        body = "".join(f'<tr><td>{escape(str(r["a"]))}</td><td>{escape(str(r["b"]))}</td>{_mrow(measures, r["measures"])}</tr>'
                       for r in cx["rows"])
        blocks.append(f'<h3 class="rep-h">{escape(cx["a_label"])} × {escape(cx["b_label"])}（クロス集計）</h3>'
                      + _table(measures, [cx["a_label"], cx["b_label"]], body))
    if rep.get("trend") and len(rep["trend"]) > 1:
        body = "".join(f'<tr><td>{escape(t["month"])}</td>{_mrow(measures, t["measures"])}</tr>' for t in rep["trend"])
        blocks.append('<h3 class="rep-h">月推移</h3>' + _table(measures, ["月"], body))
    inner = "\n".join(blocks)
    return f"""
<div class="paper">
  <div class="paper-head"><div class="badge">{escape(cfg['display_name'])}</div>
    <h2>集計レポート</h2></div>
  {inner}
</div>
""".strip()


# ── 出力（Excel）───────────────────────────────────────────────────────────────
def _write_table(ws, head_labels, measures, rows, totals=None):
    for ci, h in enumerate(head_labels + [m["label"] for m in measures], 1):
        c = ws.cell(1, ci, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = _HEAD
        c.alignment = Alignment(horizontal="center")
    r = 2
    for row in rows:
        for ci, v in enumerate(row, 1):
            ws.cell(r, ci, v)
        r += 1
    if totals is not None:
        ws.cell(r, len(head_labels), "合計").font = Font(bold=True)
        for j, m in enumerate(measures):
            cell = ws.cell(r, len(head_labels) + 1 + j, totals[m["key"]])
            cell.font = Font(bold=True)
            cell.fill = _TOT
    for ci in range(1, len(head_labels) + len(measures) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 16


def build_xlsx(cfg: dict, rep: dict, path: str | Path | IO) -> None:
    measures = rep["measures"]
    keys = [m["key"] for m in measures]
    wb = Workbook()
    wb.remove(wb.active)
    for d in rep["dimensions"]:
        ws = wb.create_sheet((f"{d['label']}別")[:28])
        rows = [[r["value"]] + [r["measures"][k] for k in keys] for r in d["rows"]]
        _write_table(ws, [d["label"]], measures, rows, d["totals"])
    if rep["cross"]:
        cx = rep["cross"]
        ws = wb.create_sheet((f"{cx['a_label']}×{cx['b_label']}")[:28])
        rows = [[r["a"], r["b"]] + [r["measures"][k] for k in keys] for r in cx["rows"]]
        _write_table(ws, [cx["a_label"], cx["b_label"]], measures, rows)
    if rep.get("trend"):
        ws = wb.create_sheet("月推移")
        rows = [[t["month"]] + [t["measures"][k] for k in keys] for t in rep["trend"]]
        _write_table(ws, ["月"], measures, rows)
    if not wb.sheetnames:
        wb.create_sheet("集計なし")
    wb.save(path)
