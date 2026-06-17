# -*- coding: utf-8 -*-
"""コア＋設定＋帳票の最小テスト（APIキー不要＝モード非依存で動く）。

実行: cd ocr && python -m pytest -q
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import core, forms, learning, registry, review_sheet  # noqa: E402

from openpyxl import load_workbook  # noqa: E402


def test_industries_registered():
    ids = {it["id"] for it in registry.list_industries()}
    assert {"sanpai", "unso"} <= ids


def test_each_config_builds_prompt_with_4_elements():
    """各業種設定から、文脈宣言・用語辞書・出力の型・補完抑制を含むプロンプトが組める。"""
    for it in registry.list_industries():
        cfg = registry.get(it["id"])
        sp = core.build_system_prompt(cfg)
        assert cfg["context_declaration"] in sp        # 文脈宣言
        assert "用語辞書" in sp                          # 用語辞書
        assert "出力するJSONの型" in sp                  # 出力の型
        assert "補完抑制ルール" in sp                    # 補完抑制ルール
        # トップレベルのキー順が固定で指示されている
        for f in cfg["header_fields"]:
            assert f'"{f["key"]}"' in sp


def test_normalize_fills_and_orders_keys():
    cfg = registry.get("sanpai")
    raw = {"emitter": "テスト商事", "items": [{"item": "混合", "net": 1000}], "extra": "捨てる"}
    out = core._normalize(cfg, raw)
    # ヘッダの全キーが存在し、未記入は None
    for f in cfg["header_fields"]:
        assert f["key"] in out
    assert out["emitter"] == "テスト商事"
    assert out["date"] is None
    # 明細は列キーに正規化される
    assert out["items"][0]["item"] == "混合"
    assert out["items"][0]["net"] == 1000
    assert "tare" in out["items"][0]
    assert "extra" not in out


def test_extract_mock_runs_without_apikey(monkeypatch):
    """APIキーが無くてもモックで構造化結果が返る（デモが止まらない）。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = registry.get("unso")
    data = core.extract("dummy.jpg", cfg)
    assert data["driver"] is not None
    assert isinstance(data["trips"], list) and data["trips"]


def test_model_ladder_and_capabilities():
    """段位はHaiku→Sonnet→Opus。Haikuはeffort非対応なので付けない。"""
    assert core.resolve_ladder() == ["haiku", "sonnet", "opus"]
    assert core.MODELS["haiku"]["id"] == "claude-haiku-4-5"
    assert core.MODELS["sonnet"]["id"] == "claude-sonnet-4-6"
    assert core.MODELS["opus"]["id"] == "claude-opus-4-8"
    assert core.MODELS["haiku"]["effort"] is None          # Haiku 4.5 は effort 非対応
    assert core.MODELS["haiku"]["thinking"] is False        # 最安・抽出に十分
    assert core.MODELS["opus"]["thinking"] is True


def test_ladder_override(monkeypatch):
    monkeypatch.setenv("OCR_LADDER", "haiku,sonnet")
    assert core.resolve_ladder() == ["haiku", "sonnet"]


def test_problem_fields_low_confidence_and_critical_empty():
    cfg = registry.get("sanpai")
    # low自信度 → 問題、medium → 問題ではない
    d1 = core._normalize(cfg, {"confidence": {"emitter": "low", "site": "medium"}})
    p1 = core.problem_fields(cfg, d1)
    assert "emitter" in p1 and "site" not in p1
    # 重要項目(net)が空の明細 → 申告が無くても問題（保守側）
    d2 = core._normalize(cfg, {"items": [{"item": "混合", "net": None}]})
    assert "net" in core.problem_fields(cfg, d2)
    # 重要項目が埋まっていれば問題なし
    d3 = core._normalize(cfg, {"emitter": "A社", "items": [{"item": "混合", "gross": 100, "tare": 30, "net": 70}]})
    assert core.problem_fields(cfg, d3) == set()


def test_mock_escalation_trace_present():
    # unso は low_first_pass を持つので Haiku→Sonnet の擬似トレースが出る
    cfg = registry.get("unso")
    data = core.extract("dummy.jpg", cfg)
    esc = data["_escalation"]
    assert esc["mock"] is True
    assert [s["tier"] for s in esc["steps"]] == ["haiku", "sonnet"]
    assert esc["escalated"] is True
    assert data["needs_human_review"] == []         # Sonnetで解決
    # sanpai は一発で確定（Haikuのみ）
    s = core.extract("dummy.jpg", registry.get("sanpai"))["_escalation"]
    assert [x["tier"] for x in s["steps"]] == ["haiku"]


def test_cost_estimate_matches_handoff():
    # 引き継ぎメモの実測: Haiku≈0.75円, Sonnet≈2.25円, Opus≈3.75円/枚（1ドル150円）
    assert abs(core._est_cost("haiku") - 0.75) < 0.01
    assert abs(core._est_cost("sonnet") - 2.25) < 0.01
    assert abs(core._est_cost("opus") - 3.75) < 0.01


def test_prompt_declares_confidence_and_critical():
    cfg = registry.get("unso")
    sp = core.build_system_prompt(cfg)
    assert "自信度の申告" in sp
    assert "confidence" in sp
    assert "★" in sp  # 重要項目(critical)の印
    assert '"confidence"' in sp and '"needs_human_review"' not in sp  # confidenceは出力キー


def test_render_form_sums_numeric_columns():
    cfg = registry.get("sanpai")
    data = core.extract("dummy.jpg", cfg)
    html = forms.render_form(cfg, data)
    assert cfg["form"]["title"] in html
    assert "合計" in html  # sum:true 列の合計行
    assert "正味重量" in html


def test_unknown_industry_raises():
    try:
        registry.get("no-such")
        assert False, "should raise"
    except KeyError:
        pass


def _hl(cfg, d):
    """比較用にヘッダ＋明細だけ取り出す。"""
    li = cfg["line_items"]["key"]
    return ({f["key"]: d.get(f["key"]) for f in cfg["header_fields"]}, d.get(li))


def test_review_sheet_roundtrip_preserves_values():
    """確認シートを出力→読み戻して、ヘッダ・明細が一致する。"""
    cfg = registry.get("sanpai")
    results = [core.extract("a.jpg", cfg), core.extract("b.jpg", cfg)]
    bio = io.BytesIO()
    review_sheet.build(cfg, results, bio)
    bio.seek(0)
    back = review_sheet.read(cfg, bio, learn=False)
    assert len(back) == len(results)
    for src, got in zip(results, back):
        assert _hl(cfg, src) == _hl(cfg, got)


def test_review_sheet_flags_human_review(tmp_path):
    """要人間確認の伝票は『要確認』、それ以外は『OK』が入る。"""
    cfg = registry.get("unso")
    res = core.extract("x.jpg", cfg)
    res["needs_human_review"] = ["expense_amount"]  # 強制的に要確認に
    p = tmp_path / "review.xlsx"
    review_sheet.build(cfg, [res], p)
    wb = load_workbook(p)
    ws = wb["確認シート"]
    confirm_col = [c[1] for c in review_sheet._columns(cfg)].index("確認") + 1
    vals = [ws.cell(r, confirm_col).value for r in range(5, ws.max_row + 1)]
    assert "要確認" in vals


def test_learning_makes_it_smarter(monkeypatch, tmp_path):
    """訂正を読み戻すと、誤読の訂正と語彙が蓄積され、次回プロンプトに載る。"""
    monkeypatch.setenv("OCR_LEARN_DIR", str(tmp_path))
    cfg = registry.get("sanpai")
    # OCRが「丸北健設」と誤読 → 人が「丸北建設」に訂正
    original = [core._normalize(cfg, {"emitter": "丸北健設", "items": [{"item": "混合廃棄物"}]})]
    corrected = [core._normalize(cfg, {"emitter": "丸北建設", "items": [{"item": "混合廃棄物"}]})]
    stats = learning.learn_from(cfg, original, corrected)
    assert stats["new_corrections"] == 1
    assert stats["total_vocabulary"] >= 2  # 排出事業者＋品目

    block = learning.as_prompt_block(cfg)
    assert "丸北健設" in block and "丸北建設" in block      # 誤読→正の訂正
    assert "混合廃棄物" in block                            # 確認済み語彙

    # 次回の system プロンプトに学習が差し込まれている＝賢くなっている
    sp = core.build_system_prompt(cfg)
    assert "現場で確定した学習メモ" in sp
    assert "丸北建設" in sp

    # もう一度同じ訂正 → 回数が増える（累積する）
    learning.learn_from(cfg, original, corrected)
    again = learning.load(cfg)["corrections"][0]
    assert again["count"] == 2


def test_learning_loop_through_sheet(monkeypatch, tmp_path):
    """確認シート経由でも学習が回る（隠しシートの元値と差分を取る）。"""
    monkeypatch.setenv("OCR_LEARN_DIR", str(tmp_path))
    cfg = registry.get("unso")
    res = core.extract("r.jpg", cfg)  # mock。driver="濱崎"
    p = tmp_path / "review.xlsx"
    review_sheet.build(cfg, [res], p)
    # 人が乗務員名を訂正（濱崎→浜崎）
    wb = load_workbook(p)
    ws = wb["確認シート"]
    driver_col = [c[0] for c in review_sheet._columns(cfg)].index("driver") + 1
    ws.cell(5, driver_col).value = "浜崎"
    wb.save(p)
    before = learning.summary(cfg)["corrections"]
    review_sheet.read(cfg, p, learn=True)
    after = learning.summary(cfg)["corrections"]
    assert after == before + 1
