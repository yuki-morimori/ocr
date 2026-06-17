# -*- coding: utf-8 -*-
"""自動学習（全業種共通）。

確認シートを読み戻すたびに『人手の訂正』を蓄積し、次回以降のプロンプトに注入する。
コードもモデルも変えず、現場の訂正が増えるほど読み取りが賢くなる仕組み。

蓄積する2種類:
  - corrections … よくある誤読の訂正（例「丸北健設」→「丸北建設」）。回数つき。
  - vocabulary … 現場で実在が確認済みの固有名詞（候補に優先させる）。

これを build_system_prompt が「# 現場で確定した学習メモ」として system に積む。
system は cache_control でキャッシュするので、学習が増えた時だけ書き換わる。

保存先: 既定 <repo>/learnings/<業種ID>.json（環境変数 OCR_LEARN_DIR で変更可）。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_MAX_CORRECTIONS = 60          # 蓄積上限（プロンプト肥大とキャッシュ不安定を防ぐ）
_MAX_VOCAB_PER_FIELD = 40
_PROMPT_CORRECTIONS = 15       # プロンプトに載せる上限
_PROMPT_VOCAB_PER_FIELD = 25


def _dir() -> Path:
    d = Path(os.getenv("OCR_LEARN_DIR") or (_REPO / "learnings"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_for(cfg: dict) -> Path:
    return _dir() / f"{cfg['id']}.json"


def _new(cfg: dict) -> dict:
    return {"industry": cfg["id"], "updated_at": None, "sheets_learned": 0,
            "corrections": [], "vocabulary": {}}


def load(cfg: dict) -> dict:
    p = path_for(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 壊れていても学習は止めない
            pass
    return _new(cfg)


def save(cfg: dict, store: dict) -> None:
    store["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    path_for(cfg).write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def _text_keys(cfg: dict) -> set[str]:
    """学習対象は文字フィールドだけ（数値・日付の誤読は現場固有で再利用できない）。"""
    keys = {f["key"] for f in cfg["header_fields"] if f.get("type", "text") == "text"}
    keys |= {c["key"] for c in cfg["line_items"]["columns"] if c.get("type", "text") == "text"}
    return keys


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _cells(cfg: dict, slip: dict):
    """1伝票ぶんの (キー, 値) を、ヘッダ＋明細全行ぶん列挙する。"""
    for f in cfg["header_fields"]:
        yield f["key"], slip.get(f["key"])
    li = cfg["line_items"]
    for row in slip.get(li["key"]) or []:
        for c in li["columns"]:
            yield c["key"], row.get(c["key"])


def learn_from(cfg: dict, original: list[dict], corrected: list[dict]) -> dict:
    """OCR結果(original)と人手訂正後(corrected)を突き合わせて学習を更新する。

    返り値は学習の増分（新規訂正数・語彙追加数・累計）。
    """
    store = load(cfg)
    text_keys = _text_keys(cfg)

    # 既存をインデックス化
    corr_idx = {(c["field"], c["from"], c["to"]): c for c in store["corrections"]}
    vocab = {k: list(v) for k, v in store.get("vocabulary", {}).items()}

    new_corr = 0
    new_vocab = 0
    for o, c in zip(original, corrected):
        ocells = list(_cells(cfg, o))
        ccells = list(_cells(cfg, c))
        for (k, ov), (_k2, cv) in zip(ocells, ccells):
            if k not in text_keys:
                continue
            ov_s, cv_s = _s(ov), _s(cv)
            if not cv_s:
                continue
            # 確定値は語彙に追加（誤読でなくても、実在する固有名詞として候補に効く）
            lst = vocab.setdefault(k, [])
            if cv_s not in lst:
                lst.append(cv_s)
                new_vocab += 1
            # 誤読の訂正を記録
            if ov_s and ov_s != cv_s:
                key = (k, ov_s, cv_s)
                if key in corr_idx:
                    corr_idx[key]["count"] += 1
                else:
                    entry = {"field": k, "from": ov_s, "to": cv_s, "count": 1}
                    corr_idx[key] = entry
                    new_corr += 1

    # まとめ直し（頻度降順、上限でカット）
    corrections = sorted(corr_idx.values(), key=lambda e: -e["count"])[:_MAX_CORRECTIONS]
    vocab = {k: lst[-_MAX_VOCAB_PER_FIELD:] for k, lst in vocab.items() if lst}
    store["corrections"] = corrections
    store["vocabulary"] = vocab
    store["sheets_learned"] = store.get("sheets_learned", 0) + len(corrected)
    save(cfg, store)

    return {
        "new_corrections": new_corr,
        "new_vocabulary": new_vocab,
        "total_corrections": len(corrections),
        "total_vocabulary": sum(len(v) for v in vocab.values()),
        "sheets_learned": store["sheets_learned"],
    }


def as_prompt_block(cfg: dict) -> str:
    """蓄積した学習を system プロンプト用のテキストにする（無ければ空）。"""
    store = load(cfg)
    corrections = store.get("corrections") or []
    vocab = store.get("vocabulary") or {}
    if not corrections and not vocab:
        return ""
    label = {f["key"]: f["label"] for f in cfg["header_fields"]}
    label.update({c["key"]: c["label"] for c in cfg["line_items"]["columns"]})

    lines = ["# 現場で確定した学習メモ（過去の人手訂正から自動蓄積。最優先で参照）"]
    if corrections:
        lines.append("よくある誤読の訂正（左に読めても、この現場では右が正しい）:")
        for e in corrections[:_PROMPT_CORRECTIONS]:
            lines.append(f'  - {label.get(e["field"], e["field"])}: 「{e["from"]}」→「{e["to"]}」'
                         f'（{e["count"]}回）')
    if vocab:
        lines.append("この現場で実在が確認済みの値（候補に迷ったら優先）:")
        for k, vals in vocab.items():
            if not vals:
                continue
            shown = " / ".join(vals[-_PROMPT_VOCAB_PER_FIELD:])
            lines.append(f"  - {label.get(k, k)}: {shown}")
    return "\n".join(lines)


def summary(cfg: dict) -> dict:
    store = load(cfg)
    return {
        "industry": cfg["id"],
        "sheets_learned": store.get("sheets_learned", 0),
        "corrections": len(store.get("corrections") or []),
        "vocabulary": sum(len(v) for v in (store.get("vocabulary") or {}).values()),
        "updated_at": store.get("updated_at"),
    }
