# -*- coding: utf-8 -*-
"""請求書生成（全業種共通・設定駆動）。

確認シートで確定した構造化データ（複数伝票）を、請求先ごとに月末集計して請求書にする。
業種ごとの違いは各業種JSONの `billing` セクションが吸収する：

  産廃 … 排出事業者ごと / 品目×正味重量×単価（混合は高単価）       mode="weight"
  運送 … 取引先ごと / 走行距離×運賃＋立替経費（傭車分は別仕分け）  mode="trip"

公開API:
    build_invoices(cfg, results, month=None, prices=None) -> list[invoice]
    render_html(cfg, invoice) -> str
    build_xlsx(cfg, invoices, path) -> None   請求先ごとに1シート
"""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import IO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_HEAD = PatternFill("solid", fgColor="1F2A3A")
_SUM = PatternFill("solid", fgColor="EAF2FF")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yen(v) -> int:
    return int(round(v or 0))


def _in_month(date_val, month: str | None) -> bool:
    if not month:
        return True
    return isinstance(date_val, str) and date_val.startswith(month)


def _party(cfg: dict, slip: dict) -> str:
    b = cfg["billing"]
    v = slip.get(b["party_field"])
    return v if v not in (None, "") else "（請求先不明）"


# ── 集計 ───────────────────────────────────────────────────────────────────────
def build_invoices(cfg: dict, results: list[dict], month: str | None = None,
                   prices: dict | None = None) -> list[dict]:
    """確定データ（複数伝票）→ 請求先ごとの請求書（構造化）。月で絞り込み可。"""
    b = cfg["billing"]
    rate = float(b.get("tax_rate", 0.10))
    # 請求先ごとに伝票をまとめる（対象月のみ）
    groups: dict[str, list[dict]] = {}
    for slip in results:
        if not _in_month(slip.get("date"), month):
            continue
        groups.setdefault(_party(cfg, slip), []).append(slip)

    invoices = []
    for party in sorted(groups):
        slips = groups[party]
        if b["mode"] == "weight":
            lines, charter = _lines_weight(cfg, slips, prices)
        elif b["mode"] == "manday":
            lines, charter = _lines_manday(cfg, slips, prices)
        else:
            lines, charter = _lines_trip(cfg, slips, prices)
        subtotal = sum(l["amount"] for l in lines)
        tax = _yen(subtotal * rate)
        invoices.append({
            "party": party,
            "party_label": b.get("party_label", "請求先"),
            "month": month or "全期間",
            "slip_count": len(slips),
            "lines": lines,
            "charter_lines": charter,
            "subtotal": _yen(subtotal),
            "tax_rate": rate,
            "tax": tax,
            "total": _yen(subtotal) + tax,
            "currency": b.get("currency", "円"),
            "note": b.get("note", ""),
        })
    return invoices


def _unit_prices(cfg: dict, prices: dict | None) -> dict:
    base = dict(cfg["billing"].get("unit_prices", {}))
    if prices:
        base.update(prices)
    return base


def _lines_weight(cfg: dict, slips: list[dict], prices: dict | None):
    """産廃: 品目×区分ごとに正味重量を合算し、区分別単価をかける。"""
    b = cfg["billing"]
    up = _unit_prices(cfg, prices)
    default = up.get("_default", 0)
    li = cfg["line_items"]["key"]
    agg: dict[tuple, dict] = {}
    for slip in slips:
        for row in slip.get(li) or []:
            item = row.get(b.get("item_field", "item")) or "（品目不明）"
            grp = row.get(b.get("group_field", "category")) or "_default"
            qty = _num(row.get(b["qty_field"]))
            if qty is None:
                continue
            key = (item, grp)
            a = agg.setdefault(key, {"item": item, "group": grp, "qty": 0.0})
            a["qty"] += qty
    lines = []
    for (item, grp), a in sorted(agg.items()):
        unit_price = up.get(grp, default)
        amount = a["qty"] * unit_price
        label = item if grp == "_default" else f"{item}（{grp}）"
        lines.append({
            "desc": label, "qty": a["qty"], "qty_unit": b.get("unit", ""),
            "unit_price": unit_price, "amount": _yen(amount),
        })
    return lines, []


def _lines_manday(cfg: dict, slips: list[dict], prices: dict | None):
    """建設: 元請ごとに 人工合計×人工単価。明細(workers)の person_days を合算。"""
    b = cfg["billing"]
    rate = float((prices or {}).get(b.get("party_label", ""), b.get("rate", 0)))
    li = cfg["line_items"]["key"]
    qf = b["qty_field"]
    mandays = 0.0
    for slip in slips:
        mandays += sum(_num(w.get(qf)) or 0 for w in slip.get(li) or [])
    lines = []
    if mandays:
        lines.append({
            "desc": f"常用（{_q(mandays)} {b.get('unit','人工')} × {int(rate):,}{b.get('currency','円')}）",
            "qty": mandays, "qty_unit": b.get("unit", "人工"),
            "unit_price": rate, "amount": _yen(mandays * rate),
        })
    return lines, []


def _lines_trip(cfg: dict, slips: list[dict], prices: dict | None):
    """運送: 自社便は走行距離×運賃＋立替を運行ごとに計上。傭車便は別仕分け。"""
    b = cfg["billing"]
    up = _unit_prices(cfg, prices)
    rate = float((prices or {}).get("rate_per_unit", b.get("rate_per_unit", 0)))
    li = cfg["line_items"]["key"]
    exp_field = b.get("expense_field", "expense_amount")
    charter_field = b.get("charter_field", "charter")
    self_label = b.get("charter_self", "自社")

    fare_dist = 0.0
    expense = 0.0
    charter_lines = []
    for slip in slips:
        trips = slip.get(li) or []
        is_charter = any(
            t.get(charter_field) and str(t.get(charter_field)) != self_label for t in trips
        )
        slip_exp = sum(_num(t.get(exp_field)) or 0 for t in trips)
        dist = _num(slip.get(b["qty_field"])) or 0
        if is_charter:
            charter_lines.append({
                "desc": f"傭車 {slip.get('date') or ''} {slip.get('vehicle_no') or ''}".strip(),
                "qty": dist, "qty_unit": b.get("unit", ""), "unit_price": None,
                "amount": _yen(slip_exp),  # 傭車は立替/外注費を実費で別記
            })
        else:
            fare_dist += dist
            expense += slip_exp

    lines = []
    if fare_dist:
        lines.append({
            "desc": f"運賃（走行 {int(fare_dist):,} {b.get('unit','')} × {int(rate)}{b.get('currency','円')}）",
            "qty": fare_dist, "qty_unit": b.get("unit", ""), "unit_price": rate,
            "amount": _yen(fare_dist * rate),
        })
    if expense:
        lines.append({
            "desc": "立替経費（高速・駐車 等／実費）", "qty": None, "qty_unit": "",
            "unit_price": None, "amount": _yen(expense),
        })
    return lines, charter_lines


# ── 帳票（HTML）────────────────────────────────────────────────────────────────
def _q(v):
    if v is None:
        return ""
    n = float(v)
    return f"{int(n):,}" if n == int(n) else f"{n:,}"


def render_html(cfg: dict, inv: dict) -> str:
    cur = inv["currency"]
    rows = ""
    for l in inv["lines"]:
        up = f'{_q(l["unit_price"])}{cur}' if l["unit_price"] is not None else "—"
        qty = f'{_q(l["qty"])} {l["qty_unit"]}' if l["qty"] is not None else "—"
        rows += (f'<tr><td>{escape(l["desc"])}</td><td class="num">{qty}</td>'
                 f'<td class="num">{up}</td><td class="num">{_q(l["amount"])}{cur}</td></tr>')
    charter = ""
    if inv["charter_lines"]:
        cr = "".join(f'<tr><td>{escape(c["desc"])}</td><td class="num">{_q(c["amount"])}{cur}</td></tr>'
                     for c in inv["charter_lines"])
        charter = (f'<h3 class="charter-h">傭車分（別仕分け）</h3>'
                   f'<table class="lines"><thead><tr><th>内容</th><th>金額</th></tr></thead>'
                   f'<tbody>{cr}</tbody></table>')
    note = f'<p class="form-note">※ {escape(inv["note"])}</p>' if inv["note"] else ""
    return f"""
<div class="paper invoice">
  <div class="paper-head">
    <div class="badge">{escape(cfg['display_name'])}</div>
    <h2>請求書</h2>
    <p class="subtitle">{escape(inv['party_label'])}: <b>{escape(inv['party'])}</b> 御中 ／
       対象期間: {escape(inv['month'])} ／ 伝票 {inv['slip_count']} 枚</p>
  </div>
  <table class="lines">
    <thead><tr><th>項目</th><th>数量</th><th>単価</th><th>金額</th></tr></thead>
    <tbody>{rows}</tbody>
    <tfoot>
      <tr class="sumrow"><td colspan="3" class="total-label">小計</td><td class="num total">{_q(inv['subtotal'])}{cur}</td></tr>
      <tr class="sumrow"><td colspan="3" class="total-label">消費税（{int(inv['tax_rate']*100)}%）</td><td class="num total">{_q(inv['tax'])}{cur}</td></tr>
      <tr class="sumrow grand"><td colspan="3" class="total-label">合計</td><td class="num total">{_q(inv['total'])}{cur}</td></tr>
    </tfoot>
  </table>
  {charter}
  {note}
</div>
""".strip()


# ── 請求書（Excel：請求先ごとに1シート）───────────────────────────────────────
def build_xlsx(cfg: dict, invoices: list[dict], path: str | Path | IO) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    for i, inv in enumerate(invoices):
        title = (inv["party"][:25] or f"請求書{i+1}").replace("/", "／").replace("（請求先不明）", "請求先不明")
        ws = wb.create_sheet(title[:28] or f"請求書{i+1}")
        cur = inv["currency"]
        ws["A1"] = f"請求書（{cfg['display_name']}）"
        ws["A1"].font = Font(bold=True, size=14)
        ws["A2"] = f"{inv['party_label']}: {inv['party']} 御中"
        ws["A3"] = f"対象期間: {inv['month']} ／ 伝票 {inv['slip_count']} 枚"
        hr = 5
        for ci, h in enumerate(["項目", "数量", "単価", "金額"], 1):
            c = ws.cell(hr, ci, h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = _HEAD
            c.alignment = Alignment(horizontal="center")
        r = hr + 1
        for l in inv["lines"]:
            ws.cell(r, 1, l["desc"])
            ws.cell(r, 2, l["qty"])
            ws.cell(r, 3, l["unit_price"])
            ws.cell(r, 4, l["amount"])
            r += 1
        for label, val in [("小計", inv["subtotal"]),
                           (f"消費税（{int(inv['tax_rate']*100)}%）", inv["tax"]),
                           ("合計", inv["total"])]:
            ws.cell(r, 3, label).font = Font(bold=True)
            cell = ws.cell(r, 4, val)
            cell.font = Font(bold=True)
            cell.fill = _SUM
            r += 1
        if inv["charter_lines"]:
            r += 1
            ws.cell(r, 1, "傭車分（別仕分け）").font = Font(bold=True)
            r += 1
            for c in inv["charter_lines"]:
                ws.cell(r, 1, c["desc"])
                ws.cell(r, 4, c["amount"])
                r += 1
        for ci, w in enumerate([34, 14, 12, 14], 1):
            ws.column_dimensions[get_column_letter(ci)].width = w
    if not wb.sheetnames:
        wb.create_sheet("請求書なし")
    wb.save(path)
