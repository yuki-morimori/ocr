# -*- coding: utf-8 -*-
"""汎用 帳票読み取りコア（全業種共通・無改変で使う層）。

役割：
    画像 + 業種設定 → Claude Vision → 構造化JSON（業種設定で指定した型）

業種ごとに変わるのは「プロンプト設定（industries/*.json）」だけ。

# 段階エスカレーション（汚い手書きの自動救済）
通常はもっとも安い Haiku で読む。各項目に自信度(high/medium/low)を返させ、
`low` が含まれる、または重要項目(critical)が空の伝票だけ、自動で上位モデルに再投入する。

    Haiku 4.5 → Sonnet 4.6 → Opus 4.8 → 「要人間確認」フラグ

終点は Opus ではなく『人間の目視』。Opus は人に回す前の最後の自動救済にすぎない。
汚い数字を上位モデルに無理やり推測させて請求書に載せるのは厳禁（請求ミス→信用失墜）。
補完抑制ルール（無い項目は空 / 読めない箇所は推測せず low）とセットで効かせる。

大多数の伝票は Haiku のままなので追加コストは誤差レベル。
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

# ── モデル段位（用途別。引き継ぎメモの方針）──────────────────────────────────
# id …… Claude のモデルID
# thinking …… 適応的思考(adaptive)を使うか（Haiku 4.5 は使わない＝最安・抽出に十分）
# effort …… 出力 effort（Haiku 4.5 は effort 非対応のため None）
# in/out …… 100万トークンあたりの料金(USD)。損益・コスト表示に使う
MODELS = {
    "haiku":  {"id": "claude-haiku-4-5",  "thinking": False, "effort": None,     "in": 1.0, "out": 5.0},
    "sonnet": {"id": "claude-sonnet-4-6", "thinking": True,  "effort": "medium", "in": 3.0, "out": 15.0},
    "opus":   {"id": "claude-opus-4-8",   "thinking": True,  "effort": "high",   "in": 5.0, "out": 25.0},
}
DEFAULT_LADDER = ["haiku", "sonnet", "opus"]  # この順に自動エスカレーション

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


def resolve_ladder() -> list[str]:
    """エスカレーションの段位列を決める（環境変数 OCR_LADDER で上書き可）。

    例: OCR_LADDER="haiku,sonnet" で Opus を使わない運用にできる。
    """
    raw = os.getenv("OCR_LADDER")
    if raw:
        tiers = [t.strip() for t in raw.split(",") if t.strip() in MODELS]
        if tiers:
            return tiers
    return list(DEFAULT_LADDER)


# ── 重要項目（critical）＝間違うと事故る欄（金額・重量・距離）──────────────────
def _critical_header_keys(cfg: dict) -> list[str]:
    return [f["key"] for f in cfg["header_fields"] if f.get("critical")]


def _critical_line_cols(cfg: dict) -> tuple[list[str], str]:
    li = cfg["line_items"]
    return [c["key"] for c in li["columns"] if c.get("critical")], li["key"]


def _valid_keys(cfg: dict) -> set[str]:
    keys = {f["key"] for f in cfg["header_fields"]}
    keys |= {c["key"] for c in cfg["line_items"]["columns"]}
    return keys


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
        mark = " ★金額/重量/距離など要正確（間違うと請求事故）" if f.get("critical") else ""
        lines.append(f'  - "{f["key"]}" … {f["label"]}（{f.get("type","text")}）: {f.get("desc","")}{mark}')
    li = cfg["line_items"]
    lines.append(f'明細配列 "{li["key"]}" … {li["label"]}（{li.get("row_hint","")}）。各行のキー:')
    for c in li["columns"]:
        mark = " ★要正確（間違うと請求事故）" if c.get("critical") else ""
        lines.append(f'    - "{c["key"]}" … {c["label"]}（{c.get("type","text")}）: {c.get("desc","")}{mark}')
    return "\n".join(lines)


def _confidence_block() -> str:
    return (
        "# 自信度の申告（エスカレーションの判定に使う）\n"
        "読み取りに自信が無い項目だけ confidence に記載する。\n"
        '- "high" は記載しない（記載の無い項目は high とみなす）。\n'
        '- 迷い・かすれ・崩し字で確信が持てない → "medium"。\n'
        '- ほぼ判読不能で推測になる → "low"。\n'
        'confidence は {"項目キー": "medium"|"low", ...} の形（明細の列キーも可）。\n'
        "low を含む、または ★の重要項目が空の伝票は、より賢いモデルや人の目視へ自動で回す。\n"
        "だから読めない箇所を無理に埋めず、正直に null＋low/unreadable で申告すること。\n"
        '読めず空にした項目のキー名は unreadable_fields（配列）に入れる。'
    )


def build_system_prompt(cfg: dict) -> str:
    """業種設定からシステムプロンプトを生成（業種転換はここだけが変わる）。"""
    keys = [f["key"] for f in cfg["header_fields"]]
    keys += [cfg["line_items"]["key"], "confidence", "unreadable_fields"]
    suppression = (
        "# 補完抑制ルール（最重要）\n"
        "手書きの金額・重量・距離を勝手に補完すると請求ミス→信用失墜に直結する。"
        "ここだけは保守的に、読めたものだけを入れる。\n"
        "- 無い項目は null（空）で返す。\n"
        "- 読めない箇所は推測せず null にし、その項目名を unreadable_fields と confidence(low) に入れる。\n"
    )
    for r in cfg.get("suppression_rules", []):
        suppression += f"- {r}\n"
    from . import learning  # 遅延import（循環回避）

    parts = [
        f"あなたは「{cfg['display_name']}」の帳票を読み取る専属オペレーターです。",
        f"# 文脈宣言\n{cfg['context_declaration']}",
        f"対象帳票: {cfg['doc_label']}。",
        _COMMON_RULES,
        _glossary_block(cfg),
        learning.as_prompt_block(cfg),  # 確認シートの訂正から自動蓄積（読み戻すほど賢くなる）
        _schema_block(cfg),
        _confidence_block(),
        suppression,
        "# 出力形式の厳守\n"
        "上記の型に適合するJSONオブジェクトを1つだけ出力する。"
        "前置き・説明・マークダウンのコードフェンス(```)は一切付けない。JSONのみ。\n"
        f"トップレベルのキーは必ず次の順で含めること: {json.dumps(keys, ensure_ascii=False)}",
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
        if isinstance(r, dict):
            norm_rows.append({c["key"]: r.get(c["key"]) for c in li["columns"]})
    out[li["key"]] = norm_rows
    valid = _valid_keys(cfg)
    conf = data.get("confidence") if isinstance(data.get("confidence"), dict) else {}
    out["confidence"] = {k: v for k, v in conf.items() if k in valid}
    out["unreadable_fields"] = [k for k in (data.get("unreadable_fields") or []) if k in valid]
    return out


# ── エスカレーション判定 ───────────────────────────────────────────────────────
def _empty(v) -> bool:
    return v is None or v == ""


def problem_fields(cfg: dict, data: dict) -> set[str]:
    """上位モデルや人へ回すべき問題項目（low自信度 / 重要項目が空 / 読めず空）。"""
    problems: set[str] = set()
    conf = data.get("confidence") or {}
    for k, v in conf.items():
        if str(v).strip().lower() == "low":
            problems.add(k)
    problems |= set(data.get("unreadable_fields") or [])
    # 重要項目（critical）が空 → 申告が無くても回す（保守側に倒す）
    for k in _critical_header_keys(cfg):
        if _empty(data.get(k)):
            problems.add(k)
    cols, lik = _critical_line_cols(cfg)
    rows = data.get(lik) or []
    for c in cols:
        if any(_empty(r.get(c)) for r in rows):
            problems.add(c)
    return problems


# ── コスト計算 ─────────────────────────────────────────────────────────────────
def _usd_jpy() -> float:
    try:
        return float(os.getenv("USD_JPY", "150"))
    except ValueError:
        return 150.0


def _cost_from_usage(tier: str, usage) -> float:
    """実呼び出しの usage から概算コスト（円）。キャッシュは read≈0.1x / write≈1.25x。"""
    m = MODELS[tier]
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    usd = (
        (inp / 1e6) * m["in"]
        + (cr / 1e6) * m["in"] * 0.1
        + (cw / 1e6) * m["in"] * 1.25
        + (out / 1e6) * m["out"]
    )
    return round(usd * _usd_jpy(), 4)


def _est_cost(tier: str) -> float:
    """伝票1枚 ≈ 入力2,500・出力500トークンと仮定した概算コスト（円）。モック表示用。"""
    m = MODELS[tier]
    usd = (2500 / 1e6) * m["in"] + (500 / 1e6) * m["out"]
    return round(usd * _usd_jpy(), 4)


# ── 抽出（コア本体：段階エスカレーション）─────────────────────────────────────
def extract(image_path: str | Path, cfg: dict) -> dict:
    """画像1枚 + 業種設定 → 構造化dict（自信度・エスカレーション履歴つき）。

    APIキーが無ければモックを返す（デモが止まらない）。実OCR失敗時もモックにフォールバック。
    最後に名寄せ（既知マスタ/学習語彙への寄せ）をかけてから返す。
    """
    if not available():
        data = _mock_result(cfg)
    else:
        try:
            data = _extract_with_escalation(Path(image_path), cfg)
        except Exception as e:  # noqa: BLE001 — デモ継続のためフォールバック
            data = _mock_result(cfg)
            data["_error"] = f"実OCRに失敗したためサンプルを表示しています: {e}"
    _apply_name_matching(cfg, data)
    return data


def _apply_name_matching(cfg: dict, data: dict) -> None:
    """会社名・氏名を既知の正式表記へ寄せる（完全一致は自動、あいまいは要確認）。

    cfg["matching"] が無い、または既知語彙が無ければ何もしない（安全な no-op）。
    """
    mc = cfg.get("matching")
    if not mc:
        return
    from . import learning, matching

    review = set(data.get("needs_human_review") or [])
    conf = data.get("confidence") or {}
    li_key = cfg["line_items"]["key"]

    def _fix(key: str, company: bool, value):
        cands = learning.candidates(cfg, key)
        if not value or not cands:
            return value
        status, canonical = matching.match(str(value), cands, company=company)
        if status == "exact" and canonical and canonical != value:
            return canonical                       # 正式表記へ自動で寄せる
        if status == "candidate":
            review.add(key)                        # あいまい → 要確認（自動では替えない）
            conf[key] = "low"
        return value

    for f in mc.get("fields", []):
        data[f["key"]] = _fix(f["key"], f.get("company", False), data.get(f["key"]))
    for col in mc.get("line_fields", []):
        for row in data.get(li_key) or []:
            row[col["key"]] = _fix(col["key"], col.get("company", False), row.get(col["key"]))

    data["confidence"] = conf
    data["needs_human_review"] = sorted(review)


def _extract_with_escalation(path: Path, cfg: dict) -> dict:
    ladder = resolve_ladder()
    steps: list[dict] = []
    data: dict = {}
    problems: set[str] = set()
    for tier in ladder:
        data, usage = _call_claude(path, cfg, tier)
        problems = problem_fields(cfg, data)
        steps.append(
            {
                "tier": tier,
                "model": MODELS[tier]["id"],
                "problems": sorted(problems),
                "cost_yen": _cost_from_usage(tier, usage),
            }
        )
        if not problems:
            break  # きれいに読めた → これ以上は上げない（コスト最小）
    data["needs_human_review"] = sorted(problems)  # 最終段でも残った＝要人間確認
    data["_escalation"] = _escalation_summary(steps, problems, mock=False)
    return data


def _call_claude(path: Path, cfg: dict, tier: str):
    import anthropic

    m = MODELS[tier]
    media_type, image_data = load_image(path)
    client = anthropic.Anthropic()
    # システムプロンプト（業種別の用語辞書を含む）をキャッシュ → 同業種の連続読取で入力コスト削減
    kwargs = dict(
        model=m["id"],
        max_tokens=8192,
        system=[{"type": "text", "text": build_system_prompt(cfg), "cache_control": {"type": "ephemeral"}}],
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                    {"type": "text", "text": f"この{cfg['doc_label']}を読み取り、指定した型のJSONだけを出力してください。"},
                ],
            }
        ],
    )
    if m["thinking"]:  # Haiku は付けない（最安・抽出に十分）
        kwargs["thinking"] = {"type": "adaptive"}
    if m["effort"]:  # Haiku 4.5 は effort 非対応のため付けない
        kwargs["output_config"] = {"effort": m["effort"]}
    resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _normalize(cfg, _json_from_text(text)), resp.usage


def _escalation_summary(steps: list[dict], problems: set[str], mock: bool) -> dict:
    return {
        "steps": steps,
        "final_model": steps[-1]["model"] if steps else None,
        "final_tier": steps[-1]["tier"] if steps else None,
        "escalated": len(steps) > 1,
        "total_cost_yen": round(sum(s["cost_yen"] for s in steps), 4),
        "human_review_required": bool(problems),
        "mock": mock,
    }


# ── モック（APIキー無し）：エスカレーションの流れも擬似再現してデモで見せる ────────
def _mock_result(cfg: dict) -> dict:
    data = _normalize(cfg, cfg.get("mock", {}))
    ladder = resolve_ladder()
    # 設定の mock.low_first_pass があれば「Haikuではlow→上位で解決」を擬似再現
    low_first = [k for k in (cfg.get("mock", {}).get("low_first_pass") or []) if k in _valid_keys(cfg)]
    steps: list[dict] = []
    if low_first and len(ladder) >= 2:
        steps.append({"tier": ladder[0], "model": MODELS[ladder[0]]["id"],
                      "problems": sorted(low_first), "cost_yen": _est_cost(ladder[0])})
        steps.append({"tier": ladder[1], "model": MODELS[ladder[1]]["id"],
                      "problems": [], "cost_yen": _est_cost(ladder[1])})
        problems: set[str] = set()
    else:
        steps.append({"tier": ladder[0], "model": MODELS[ladder[0]]["id"],
                      "problems": [], "cost_yen": _est_cost(ladder[0])})
        problems = set()
    data["needs_human_review"] = sorted(problems)
    data["_escalation"] = _escalation_summary(steps, problems, mock=True)
    return data
