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

from engine import core, forms, invoice, learning, payroll, registry, report, review_sheet


def main() -> None:
    p = argparse.ArgumentParser(description="業種切替式 帳票読み取りデモ（CLI）")
    p.add_argument("images", nargs="*", type=Path, help="伝票画像（複数可）")
    p.add_argument("--industry", "-i", help="業種ID（例 sanpai / unso）")
    p.add_argument("--list", action="store_true", help="登録済み業種の一覧を表示")
    p.add_argument("--review", type=Path, help="確認シート(.xlsx)の出力先")
    p.add_argument("--apply", type=Path, help="訂正済み確認シート(.xlsx)を読み戻してJSON出力（学習更新）")
    p.add_argument("--form", type=Path, help="先頭伝票の帳票HTML出力先")
    p.add_argument("--invoice", type=Path, help="請求書(.xlsx)の出力先（請求先ごとに集計）")
    p.add_argument("--report", type=Path, help="集計レポート(.xlsx)の出力先（会社別・車番別 等）")
    p.add_argument("--payroll", type=Path, help="給与素データ(.xlsx)の出力先（対象者ごと）")
    p.add_argument("--month", help="対象月 YYYY-MM（請求書・集計・給与の絞り込み。省略時は全期間）")
    p.add_argument("--learned", action="store_true", help="蓄積された学習内容を表示")
    p.add_argument("--seed-master", dest="seed_master", type=Path,
                   help="名簿/取引先リスト(CSV/Excel)を先読み辞書に取り込む")
    p.add_argument("--map", help="先読みの列対応 例 'name=氏名,prime_contractor=元請'")
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

    # マスタ/名簿の先読み（cold-start辞書。初回からその会社の固有名詞が効く）
    if args.seed_master:
        if not args.seed_master.exists():
            raise SystemExit(f"マスタが見つかりません: {args.seed_master}")
        if not args.map:
            raise SystemExit("--map で列対応を指定してください 例 'prime_contractor=元請,name=氏名'")
        field_map = {}
        for pair in args.map.split(","):
            k, _, col = pair.partition("=")
            k, col = k.strip(), col.strip()
            if k and col:
                field_map[k] = int(col) if col.isdigit() else col
        res = learning.import_master_file(cfg, args.seed_master, field_map)
        print(f"先読み辞書に取り込みました（追加 {res['added']} 件 / 語彙 {res['total_vocabulary']} 件）",
              file=sys.stderr)
        return

    # データ源：訂正済みシートの読み戻し（ループを閉じる＝学習更新）か、画像の読み取り
    if args.apply:
        if not args.apply.exists():
            raise SystemExit(f"確認シートが見つかりません: {args.apply}")
        before = learning.summary(cfg)
        results = review_sheet.read(cfg, args.apply)
        after = learning.summary(cfg)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"[学習更新] 訂正 {before['corrections']}→{after['corrections']} 件 / "
              f"語彙 {before['vocabulary']}→{after['vocabulary']} 件 / "
              f"累計 {after['sheets_learned']} 伝票", file=sys.stderr)
    else:
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
    if args.invoice:
        if "billing" not in cfg:
            raise SystemExit(f"{cfg['display_name']} には billing 設定がありません。")
        invs = invoice.build_invoices(cfg, results, month=args.month)
        invoice.build_xlsx(cfg, invs, args.invoice)
        tot = sum(i["total"] for i in invs)
        print(f"請求書を保存しました: {args.invoice}"
              f"（請求先 {len(invs)} 件 / 合計 {tot:,}円）", file=sys.stderr)
    if args.report:
        if "analytics" not in cfg:
            raise SystemExit(f"{cfg['display_name']} には analytics 設定がありません。")
        rep = report.aggregate(cfg, results, month=args.month)
        report.build_xlsx(cfg, rep, args.report)
        dims = " / ".join(f"{d['label']}{len(d['rows'])}件" for d in rep["dimensions"])
        print(f"集計レポートを保存しました: {args.report}（{dims}）", file=sys.stderr)
    if args.payroll:
        if "payroll" not in cfg:
            raise SystemExit(f"{cfg['display_name']} には payroll 設定がありません。")
        pays = payroll.build(cfg, results, month=args.month)
        payroll.build_xlsx(cfg, pays, args.payroll)
        tot = sum(p["total"] for p in pays)
        print(f"給与素データを保存しました: {args.payroll}"
              f"（対象 {len(pays)} 名 / 支給合計 {tot:,}円）", file=sys.stderr)


def _print_industries() -> None:
    print("登録済み業種:")
    for it in registry.list_industries():
        print(f"  {it['id']:8s} {it['display_name']}（{it['doc_label']}）")


if __name__ == "__main__":
    main()
