# -*- coding: utf-8 -*-
"""汎用 帳票読み取りエンジン（コア）。

公開API:
    registry.list_industries() / registry.get(id)   業種設定の取得
    core.extract(image_path, cfg)                    画像→構造化データ
    forms.render_form(cfg, data)                     構造化データ→帳票HTML
"""
from . import core, forms, invoice, learning, registry, review_sheet  # noqa: F401

__all__ = ["core", "forms", "invoice", "learning", "registry", "review_sheet"]
