# -*- coding: utf-8 -*-
"""汎用 帳票読み取りコア（全業種共通・無改変で使う層）。

役割は1つだけ：
    画像 + 業種設定 → Claude Vision → 構造化JSON（業種設定で指定した型）

業種ごとに変わるのは「プロンプト設定（industries/*.json）」だけ。このファイルは
どの業種でも同じコードで動く。プロンプトは設定から動的に組み立てる。

APIキー（ANTHROPIC_API_KEY）が無い環境では、設定内の mock を返してデモが動く。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

try:  # .env を読めれば読む（無くても可）
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

MODEL = os.getenv("OCR_MODEL", "claude-opus-4-8")

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# ── 全業種共通の読み取りルール（コア。業種に依存しない普遍ルール）──────────────
_COMMON_RULES = """\
読み取りの共通ルール:
- 印刷された枠線・項目名そのものは出力しない。記入・印字された『値』だけを抽出する。
- レイアウトは固定ではない。様式が違っても、項目の意味から対応する値を探して入れる。
- 日付は和暦の可能性が高い（R=令和 / H=平成 / S=昭和）。西暦 YYYY-MM-DD に変換して入れる。
  例『R8年3月15日』→『2026-03-15』。読めなければ null。
- 手書き文字・崩し字・半角カナ略記も、前後の枠・用語辞書から丁寧に推定する。
- 数値項目は数字だけにする（単位・カンマ・『約』等は除く。例『1,720kg』→ 1720）。
- 書かれていない項目は null、明細が無ければ空配列にする。項目を勝手に創作しない。
"""


def available() -> bool:
    """実OCR（Claude）が使えるか（APIキーがあるか）。"""
    return bool(os.getenv("ANTHROPIC_API_KEY"))


# ── プロンプト組み立て（設定 → system prompt）──────────────────────────────────
def _glossary_block(cfg: dict) -> str:
    if not cfg.get("glossary"):
        return ""
    lines = ["# 用語辞書（この業界特有の語・略記の意味）"]
    for g in cfg["glossary"]:
        lines.append(f"- {g['term']}: {g['note']}")
    return "\n".join(lines) + "\n"


def _schema_block(cfg: dict) -> str:
    """出力の型（項目順を固定したJSON）を人間可読＋機械可読で提示する。"""
    lines = ["# 出力するJSONの型（このキー・この順番で必ず返す）"]
    lines.append("トップレベルの項目（ヘッダ）:")
    for f in cfg["header_fields"]:
        lines.append(f'  - "{f["key"]}" … {f["label"]}（{f.get("type","text")}）: {f.get("desc","")}')
    li = cfg["line_items"]
    lines.append(f'明細配列 "{li["key"]}" … {li["label"]}（{li.get("row_hint","")}）。各行のキー:')
    for c in li["columns"]:
        lines.append(f'    - "{c["key"]}" … {c["label"]}（{c.get("type","text")}）: {c.get("desc","")}')
    lines.append('読めなかった項目名の配列 "unreadable_fields" … 推測せず空にした項目のキー名を入れる。')
    return "\n".join(lines)


def _suppression_block(cfg: dict) -> str:
    rules = list(cfg.get("suppression_rules", []))
    head = (
        "# 補完抑制ルール（最重要）\n"
        "手書きの金額・重量・距離を勝手に補完すると請求ミス→信用失墜に直結する。"
        "ここだけは保守的に、読めたものだけを入れる。\n"
        "- 無い項目は null（空）で返す。\n"
        "- 読めない箇所は推測せず null にし、その項目名を unreadable_fields に入れる。\n"
    )
    for r in rules:
        head += f"- {r}\n"
    return head


def build_system_prompt(cfg: dict) -> str:
    """業種設定からシステムプロンプトを生成（業種転換はここだけが変わる）。"""
    example_keys = [f["key"] for f in cfg["header_fields"]] + [cfg["line_items"]["key"], "unreadable_fields"]
    parts = [
        f"あなたは「{cfg['display_name']}」の帳票を読み取る専属オペレーターです。",
        f"# 文脈宣言\n{cfg['context_declaration']}",
        f"対象帳票: {cfg['doc_label']}。",
        _COMMON_RULES,
        _glossary_block(cfg),
        _schema_block(cfg),
        _suppression_block(cfg),
        "# 出力形式の厳守\n"
        "上記の型に適合するJSONオブジェクトを1つだけ出力する。"
        "前置き・説明・マークダウンのコードフェンス(```)は一切付けない。JSONのみ。\n"
        f"トップレベルのキーは必ず次の順で含めること: {json.dumps(example_keys, ensure_ascii=False)}",
    ]
    return "\n\n".join(p for p in parts if p.strip())


# ── 画像読み込み ───────────────────────────────────────────────────────────────
def load_image(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    media_type = MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise ValueError(
            f"対応していない画像形式です: {suffix}（対応: {', '.join(MEDIA_TYPES)}）"
        )
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return media_type, data


def _json_from_text(text: str) -> dict:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("モデルがJSONを返しませんでした（画像が不鮮明な可能性）。")
    return json.loads(text[start : end + 1])


def _normalize(cfg: dict, data: dict) -> dict:
    """設定の型に沿って、欠けたキーを補い順序を揃える（後段の帳票生成を安定させる）。"""
    out: dict = {}
    for f in cfg["header_fields"]:
        out[f["key"]] = data.get(f["key"])
    li = cfg["line_items"]
    rows = data.get(li["key"]) or []
    norm_rows = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        norm_rows.append({c["key"]: r.get(c["key"]) for c in li["columns"]})
    out[li["key"]] = norm_rows
    out["unreadable_fields"] = data.get("unreadable_fields") or []
    return out


# ── 抽出（コア本体）───────────────────────────────────────────────────────────
def extract(image_path: str | Path, cfg: dict) -> dict:
    """画像1枚 + 業種設定 → 構造化dict。

    APIキーが無ければ設定内 mock を返す（デモが止まらない）。実OCR失敗時も mock に
    フォールバックする。
    """
    path = Path(image_path)
    if not available():
        return _normalize(cfg, cfg.get("mock", {}))
    try:
        return _extract_claude(path, cfg)
    except Exception as e:  # noqa: BLE001 — デモ継続のためフォールバック
        fallback = _normalize(cfg, cfg.get("mock", {}))
        fallback["_error"] = f"実OCRに失敗したためサンプルを表示しています: {e}"
        return fallback


def _extract_claude(path: Path, cfg: dict) -> dict:
    import anthropic

    media_type, image_data = load_image(path)
    client = anthropic.Anthropic()
    system_prompt = build_system_prompt(cfg)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": image_data},
                    },
                    {
                        "type": "text",
                        "text": f"この{cfg['doc_label']}を読み取り、指定した型のJSONだけを出力してください。",
                    },
                ],
            }
        ],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = _json_from_text(text)
    return _normalize(cfg, data)
