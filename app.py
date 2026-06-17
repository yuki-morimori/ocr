# -*- coding: utf-8 -*-
"""業種切替式 帳票読み取りデモ（Web）。

自己完結で動く：FastAPI + Jinja2。APIキー（ANTHROPIC_API_KEY）が無くてもモックで全機能稼働。

起動:
    pip install -r requirements.txt
    uvicorn app:app --reload   # http://127.0.0.1:8000

構造（引き継ぎメモの三層）:
    コア          engine/core.py     画像→Claude→JSON（業種非依存・無改変）
    業種別設定     industries/*.json  文脈宣言＋用語辞書＋出力の型＋補完抑制ルール
    業種別の見せ方  engine/forms.py    帳票HTML（設定が違いを吸収）
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from engine import core, forms, invoice, learning, payroll, registry, report, review_sheet

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

BASE = Path(__file__).resolve().parent
app = FastAPI(title="汎用 帳票読み取りエンジン デモ")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "industries": registry.list_industries(),
            "live": core.available(),
        },
    )


@app.post("/read", response_class=HTMLResponse)
async def read(request: Request, industry: str = Form(...), image: UploadFile = File(...)):
    try:
        cfg = registry.get(industry)
    except KeyError as e:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"industries": registry.list_industries(),
             "live": core.available(), "error": str(e)},
            status_code=400,
        )

    suffix = Path(image.filename or "upload.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await image.read())
        tmp_path = Path(tmp.name)

    try:
        data = core.extract(tmp_path, cfg)
        form_html = forms.render_form(cfg, data)
    finally:
        os.unlink(tmp_path)

    # 1枚からプレビュー（その伝票の請求先・月で集計）。設定がある業種のみ。
    month = (data.get("date") or "")[:7]
    invoice_html = ""
    if "billing" in cfg:
        invs = invoice.build_invoices(cfg, [data], month=month or None)
        if invs:
            invoice_html = invoice.render_html(cfg, invs[0])
    payroll_html = ""
    if "payroll" in cfg:
        pays = payroll.build(cfg, [data], month=month or None)
        if pays:
            payroll_html = payroll.render_html(cfg, pays[0])

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "cfg": cfg,
            "data": data,
            "form_html": form_html,
            "invoice_html": invoice_html,
            "payroll_html": payroll_html,
            "invoice_month": month,
            "json_text": _pretty(data),
            "live": core.available(),
            "filename": image.filename,
        },
    )


@app.post("/review")
def review(industry: str = Form(...), payload: str = Form(...)):
    """読み取り結果から確認シート(.xlsx)を生成してダウンロードさせる。"""
    cfg = registry.get(industry)
    data = json.loads(payload)
    results = data if isinstance(data, list) else [data]
    bio = io.BytesIO()
    review_sheet.build(cfg, results, bio)
    return Response(
        content=bio.getvalue(),
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="review_{cfg["id"]}.xlsx"'},
    )


@app.post("/invoice")
def make_invoice(industry: str = Form(...), payload: str = Form(...), month: str = Form("")):
    """確定データから請求書(.xlsx)を生成（請求先ごとに集計）してダウンロードさせる。"""
    cfg = registry.get(industry)
    if "billing" not in cfg:
        return Response("この業種には請求設定がありません。", status_code=400)
    data = json.loads(payload)
    results = data if isinstance(data, list) else [data]
    invoices = invoice.build_invoices(cfg, results, month=month or None)
    bio = io.BytesIO()
    invoice.build_xlsx(cfg, invoices, bio)
    return Response(
        content=bio.getvalue(),
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="invoice_{cfg["id"]}.xlsx"'},
    )


@app.post("/payroll")
def make_payroll(industry: str = Form(...), payload: str = Form(...), month: str = Form("")):
    """確定データから給与素データ(.xlsx)を生成（対象者ごと）。"""
    cfg = registry.get(industry)
    if "payroll" not in cfg:
        return Response("この業種には給与設定がありません。", status_code=400)
    data = json.loads(payload)
    results = data if isinstance(data, list) else [data]
    pays = payroll.build(cfg, results, month=month or None)
    bio = io.BytesIO()
    payroll.build_xlsx(cfg, pays, bio)
    return Response(
        content=bio.getvalue(),
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="payroll_{cfg["id"]}.xlsx"'},
    )


@app.post("/report")
def make_report(industry: str = Form(...), payload: str = Form(...), month: str = Form("")):
    """確定データから集計レポート(.xlsx)を生成（会社別・車番別 等）。"""
    cfg = registry.get(industry)
    if "analytics" not in cfg:
        return Response("この業種には集計設定がありません。", status_code=400)
    data = json.loads(payload)
    results = data if isinstance(data, list) else [data]
    rep = report.aggregate(cfg, results, month=month or None)
    bio = io.BytesIO()
    report.build_xlsx(cfg, rep, bio)
    return Response(
        content=bio.getvalue(),
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="report_{cfg["id"]}.xlsx"'},
    )


@app.post("/report-sheet", response_class=HTMLResponse)
async def report_from_sheet(request: Request, industry: str = Form(...), sheet: UploadFile = File(...)):
    """確認シート（複数伝票）を取り込み、会社別・車番別の集計レポートをプレビュー表示。"""
    cfg = registry.get(industry)
    if "analytics" not in cfg:
        return Response("この業種には集計設定がありません。", status_code=400)
    suffix = Path(sheet.filename or "review.xlsx").suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await sheet.read())
        tmp_path = Path(tmp.name)
    try:
        results = review_sheet.read(cfg, tmp_path, learn=False)  # 集計だけ。学習は /learn 側
    finally:
        os.unlink(tmp_path)
    rep = report.aggregate(cfg, results)
    return templates.TemplateResponse(
        request,
        "report.html",
        {"cfg": cfg, "report_html": report.render_html(cfg, rep), "slip_count": rep["slip_count"],
         "payload": _pretty(results), "month": ""},
    )


@app.post("/learn", response_class=HTMLResponse)
async def learn(request: Request, industry: str = Form(...), sheet: UploadFile = File(...)):
    """人が訂正した確認シートを受け取り、学習を更新して結果を表示する。"""
    cfg = registry.get(industry)
    before = learning.summary(cfg)
    suffix = Path(sheet.filename or "review.xlsx").suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await sheet.read())
        tmp_path = Path(tmp.name)
    try:
        review_sheet.read(cfg, tmp_path, learn=True)
    finally:
        os.unlink(tmp_path)
    return templates.TemplateResponse(
        request,
        "learned.html",
        {
            "cfg": cfg,
            "before": before,
            "after": learning.summary(cfg),
            "block": learning.as_prompt_block(cfg),
        },
    )


def _pretty(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
