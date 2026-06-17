# -*- coding: utf-8 -*-
"""コマンドラインから業種を切り替えて読み取り、確認シートまで出す。

例:
    python cli.py --list
    python cli.py -i sanpai 伝票1.jpg 伝票2.jpg --review 確認シート.xlsx
    python cli.py -i unso 運行日報.jpg --form out.html
    python cli.py -i sanpai --apply 確認シート.xlsx        # 訂正済みシートを読み戻す
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engine import core, forms, learning, registry, review_sheet


def main() -> None:
    p = argparse.ArgumentParser(description="業種切替式 帳票読み取りデモ（CLI）")
    p.add_argument("images", nargs="*", type=Path, help="伝票画像（複数可）")
    p.add_argument("--industry", "-i", help="業種ID（例 sanpai / unso）")
    p.add_argument("--list", action="store_true", help="登録済み業種の一覧を表示")
    p.add_argument("--review", type=Path, help="確認シート(.xlsx)の出力先")
    p.add_argument("--apply", type=Path, help="訂正済み確認シート(.xlsx)を読み戻してJSON出力（学習更新）")
    p.add_argument("--form", type=Path, help="先頭伝票の帳票HTML出力先")
    p.add_argument("--learned", action="store_true", help="蓄積された学習内容を表示")
    args = p.parse_args()

    if args.list:
        _print_industries()
        return
    if not args.industry:
        _print_industries()
        raise SystemExit("\n--industry <id> を指定してください。")
    cfg = registry.get(args.industry)

    if args.learned:
        print(learning.as_prompt_block(cfg) or "（まだ学習はありません）")
        print(f"\n[学習サマリ] {json.dumps(learning.summary(cfg), ensure_ascii=False)}", file=sys.stderr)
        return

    # 訂正済みシートの読み戻し（ループを閉じる＝読み戻すたびに賢くなる）
    if args.apply:
        if not args.apply.exists():
            raise SystemExit(f"確認シートが見つかりません: {args.apply}")
        before = learning.summary(cfg)
        corrected = review_sheet.read(cfg, args.apply)
        after = learning.summary(cfg)
        print(json.dumps(corrected, ensure_ascii=False, indent=2))
        print(f"[学習更新] 訂正 {before['corrections']}→{after['corrections']} 件 / "
              f"語彙 {before['vocabulary']}→{after['vocabulary']} 件 / "
              f"累計 {after['sheets_learned']} 伝票", file=sys.stderr)
        return

    if not args.images:
        raise SystemExit("画像を1枚以上指定してください（または --apply）。")
    for img in args.images:
        if not img.exists():
            raise SystemExit(f"画像が見つかりません: {img}")

    mode = "実OCR(エスカレーション)" if core.available() else "モック(APIキー未設定)"
    print(f"[{cfg['display_name']}] {mode} で {len(args.images)} 枚を読み取り中…", file=sys.stderr)
    results = [core.extract(img, cfg) for img in args.images]
    print(json.dumps(results, ensure_ascii=False, indent=2))

    if args.form:
        Path(args.form).write_text(forms.render_form(cfg, results[0]), encoding="utf-8")
        print(f"帳票HTMLを保存しました: {args.form}", file=sys.stderr)
    if args.review:
        review_sheet.build(cfg, results, args.review)
        flagged = sum(1 for r in results if r.get("needs_human_review"))
        print(f"確認シートを保存しました: {args.review}"
              f"（要人間確認 {flagged}/{len(results)} 枚）", file=sys.stderr)


def _print_industries() -> None:
    print("登録済み業種:")
    for it in registry.list_industries():
        print(f"  {it['id']:8s} {it['display_name']}（{it['doc_label']}）")


if __name__ == "__main__":
    main()
