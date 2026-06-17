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
