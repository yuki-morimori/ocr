# -*- coding: utf-8 -*-
"""コマンドラインから業種を切り替えて読み取る。

例:
    python cli.py --industry sanpai サンプル.jpg
    python cli.py --industry unso   運行日報.jpg --form out.html
    python cli.py --list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engine import core, forms, registry


def main() -> None:
    p = argparse.ArgumentParser(description="業種切替式 帳票読み取りデモ（CLI）")
    p.add_argument("image", nargs="?", type=Path, help="伝票画像のパス")
    p.add_argument("--industry", "-i", help="業種ID（例 sanpai / unso）")
    p.add_argument("--list", action="store_true", help="登録済み業種の一覧を表示")
    p.add_argument("--form", type=Path, help="帳票HTMLの出力先（省略時はJSONのみ）")
    args = p.parse_args()

    if args.list or not args.industry:
        print("登録済み業種:")
        for it in registry.list_industries():
            print(f"  {it['id']:8s} {it['display_name']}（{it['doc_label']}）")
        if args.list:
            return
        if not args.industry:
            raise SystemExit("\n--industry <id> を指定してください。")

    if not args.image or not args.image.exists():
        raise SystemExit(f"画像が見つかりません: {args.image}")

    cfg = registry.get(args.industry)
    mode = "実OCR(Claude)" if core.available() else "モック(APIキー未設定)"
    print(f"[{cfg['display_name']}] {mode} で読み取り中…", file=sys.stderr)

    data = core.extract(args.image, cfg)
    print(json.dumps(data, ensure_ascii=False, indent=2))

    if args.form:
        html = forms.render_form(cfg, data)
        args.form.write_text(html, encoding="utf-8")
        print(f"帳票HTMLを保存しました: {args.form}", file=sys.stderr)


if __name__ == "__main__":
    main()
