# -*- coding: utf-8 -*-
"""業種設定（プロンプト設定）のレジストリ。

industries/*.json を読み込むだけ。新しい業種を増やすときは JSON を1枚足すだけで、
コア・アプリ側のコードは一切触らない（＝差し替えだけで業種転換できる構造）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

INDUSTRY_DIR = Path(__file__).resolve().parent.parent / "industries"

# 設定JSONに必須のキー（読み込み時に最低限を検証）
_REQUIRED = ("id", "display_name", "context_declaration", "header_fields", "line_items")


def _validate(cfg: dict, path: Path) -> dict:
    missing = [k for k in _REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f"業種設定 {path.name} に必須キーがありません: {missing}")
    cfg.setdefault("glossary", [])
    cfg.setdefault("suppression_rules", [])
    cfg.setdefault("doc_label", cfg["display_name"])
    cfg.setdefault("form", {})
    return cfg


@lru_cache(maxsize=1)
def _load_all() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(INDUSTRY_DIR.glob("*.json")):
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg = _validate(cfg, path)
        out[cfg["id"]] = cfg
    return out


def list_industries() -> list[dict]:
    """登録済み業種の一覧（id, display_name, doc_label）。"""
    return [
        {"id": c["id"], "display_name": c["display_name"], "doc_label": c["doc_label"]}
        for c in _load_all().values()
    ]


def get(industry_id: str) -> dict:
    """業種IDで設定を取得。無ければ KeyError。"""
    cfgs = _load_all()
    if industry_id not in cfgs:
        raise KeyError(
            f"未知の業種です: {industry_id}（登録済み: {', '.join(cfgs)}）"
        )
    return cfgs[industry_id]
