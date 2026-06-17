# -*- coding: utf-8 -*-
"""名寄せ（全業種共通）。

OCRの表記ゆれ・旧字体・会社種別語を吸収し、既知の値（マスタ/学習語彙）へ寄せる。
既存の建設アプリ namematch.py / client_match.py の設計を業種非依存に一般化したもの。

方針（請求事故を起こさないため安全側）:
  - 完全一致（正規化後に一致）… 正式表記へ自動で寄せる（株式会社/(株)/空白/旧字体/長音の揺れ）
  - 部分一致（姓だけ等・氏名のみ、候補が1人に絞れる）… 自動で寄せる
  - あいまい一致（似てるが別キー）… 自動では替えず『要確認』として人へ
  - 未一致 … そのまま

会社名(company=True)は誤統合が請求事故に直結するため、部分一致では寄せない（完全一致のみ）。
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# 旧字体→新字体（氏名で同一人物とみなして安全なものだけ。斉/斎/齋は統合しない）
_VARIANTS = str.maketrans({
    "眞": "真", "栁": "柳", "髙": "高", "﨑": "崎", "濵": "浜", "濱": "浜",
    "邉": "辺", "邊": "辺", "廣": "広", "德": "徳", "國": "国", "淺": "浅",
    "槇": "槙", "渕": "淵", "瀨": "瀬", "莊": "荘", "鐡": "鉄", "嶋": "島", "澤": "沢",
})
# 会社種別語（比較時に無視）
_COMPANY_WORDS = ["株式会社", "有限会社", "合同会社", "合資会社", "合名会社",
                  "(株)", "(有)", "(同)", "㈱", "㈲"]
# 会社名比較で落とす記号（長音・各種ハイフン・中点・読点など）
_DROP = re.compile(r"[ 　ー\-‐‑‒–—―－・･、,．。\.]+")


def normalize(s: str | None, company: bool = False) -> str:
    """比較用の正規化キー（表示用ではない）。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    for ch in (" ", "　", "\t", "\n", "\r"):
        s = s.replace(ch, "")
    s = s.translate(_VARIANTS)
    if company:
        for w in _COMPANY_WORDS:
            s = s.replace(w, "")
        s = _DROP.sub("", s)
    return s.strip().lower()


def _index(candidates, company):
    idx: dict[str, list[str]] = {}
    for c in candidates:
        k = normalize(c, company)
        if k:
            idx.setdefault(k, []).append(c)
    return idx


def match(query: str, candidates, company: bool = False, cutoff: float = 0.72):
    """query を candidates（正式表記の一覧）へ照合。

    返り値: (status, canonical)
      status ∈ "exact" | "candidate" | "none"
      canonical … 寄せ先の正式表記（none のとき None）
    """
    q = normalize(query, company)
    if not q:
        return ("none", None)
    idx = _index(candidates, company)

    # 1) 完全一致
    if q in idx:
        canons = list(dict.fromkeys(idx[q]))
        return ("exact", canons[0]) if len(canons) == 1 else ("candidate", canons[0])

    keys = list(idx.keys())
    # 2) 部分一致（氏名のみ・会社名はやらない）
    if not company and len(q) >= 2:
        part = []
        for k in keys:
            if len(k) >= 2 and (q in k or k in q):
                part.extend(idx[k])
        part = list(dict.fromkeys(part))
        if len(part) == 1:
            return ("exact", part[0])
        if part:
            return ("candidate", part[0])

    # 3) あいまい一致 → 候補（自動では替えない）
    close = difflib.get_close_matches(q, keys, n=1, cutoff=cutoff)
    if close:
        return ("candidate", idx[close[0]][0])

    return ("none", None)
