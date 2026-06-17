# -*- coding: utf-8 -*-
"""コア＋設定＋帳票の最小テスト（APIキー不要＝モード非依存で動く）。

実行: cd ocr && python -m pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import core, forms, registry  # noqa: E402


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
