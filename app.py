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

import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from engine import core, forms, registry

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

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "cfg": cfg,
            "data": data,
            "form_html": form_html,
            "json_text": _pretty(data),
            "live": core.available(),
            "filename": image.filename,
        },
    )


def _pretty(data: dict) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
