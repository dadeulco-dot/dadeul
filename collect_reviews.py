#!/usr/bin/env python3
"""
다들 · 리뷰 수집 스크립트 (로컬 실행 전용)

명세서 docs/collect-reviews.md 를 따릅니다.

⚠️ 이 스크립트는 본인 PC에서 수동으로 돌리는 도구입니다.
   Render 등 서버에 올려 자동 실행하지 마세요.

⚠️ 실행 전에 대상 사이트의 이용약관과 robots.txt를 직접 확인하세요.


무엇을 하는가
-------------
  제품 상세 페이지 → 「리뷰 전체보기」 팝업 → (대표 옵션 체크) → 최신순
  → 페이지를 끝까지 넘기며 리뷰를 한 건씩 뜯어냅니다.

  뜯어내는 값 (명세서 4장):
    review_key   data-shp-contents-id — 네이버가 붙인 리뷰 id. 30일 재조회의 열쇠
    body         p[id^="review_content_"] 의 textContent
    rating       별점. ★ 라벨 계산에 쓰지 않습니다. 텍스트와 어긋나는지 대조용
    written_at   작성일 (구매일이 아닙니다)
    author_hash  마스킹된 ID를 해시로. 원문은 CSV에 쓰지 않습니다
    is_month     「한달사용」 배지 — 내구성 문제의 분모
    is_repeat    「재구매」 배지 — 가장 강한 만족 신호
    option_text  이 리뷰가 어느 옵션의 것인지
    has_photo    사진 유무만. 사진 자체는 가져오지 않습니다

  그리고 1단 하드 배제(코드)까지 여기서 끝냅니다.
  2·3단(AI)은 어드민에서 돌립니다. 이 스크립트는 AI를 부르지 않습니다 — 돈이 안 듭니다.


셀렉터를 고른 기준 ★
--------------------
  네이버 브랜드스토어는 클래스명이 난독화돼 있고 빌드마다 바뀝니다.
  (PYRRKjHPB6 · rQ3tSUjii3 · ibGaqMAxQ2 …)
  그래서 클래스명은 하나도 쓰지 않습니다. 의미가 붙은 속성만 씁니다.

    div[data-shp-area-id="review"]        리뷰 한 건의 안쪽 블록
    └ data-shp-contents-id               리뷰 id
    p[id^="review_content_"]             본문
    div[id^="review_option_"] button      옵션
    em                                    배지 (한달사용 / 재구매)
    img[alt="review_image"]               사진

  ★ 별점·작성자·작성일은 리뷰 블록의 「형제」에 있습니다.
    구조상 부모를 한 칸 올라가야 합니다 (아래 JS 의 card = el.parentElement).

  ★ 팝업과 페이지 안쪽 리뷰 목록은 클래스명이 전혀 다릅니다.
    옵션 필터도 팝업 안에만 있으므로, 수집은 항상 팝업에서 합니다.


사용법
------
  pip install playwright
  # (Chromium 이 이미 깔려 있으면 playwright install 은 생략)

  # 제품 하나 · 대표 옵션만
  python collect_reviews.py \\
      --product "https://brand.naver.com/dorco/products/13743566149" \\
      --option "01. 헤이즐 IH 팬 24cm" \\
      --brand 도루코 --category 프라이팬 --limit-pages 5

  # collect_store.py 가 뽑은 products CSV 를 그대로 먹입니다 (권장)
  python collect_reviews.py --file products_20260925_1930.csv --limit-pages 10

  # 눈으로 보면서 (셀렉터가 깨졌을 때)
  python collect_reviews.py --product "..." --headful --debug

  # 브라우저 없이 파서만 검사 — 저장해 둔 리뷰 HTML 조각으로
  python collect_reviews.py --selftest sample_review.html

결과
----
  reviews_YYYYMMDD_HHMM.csv
"""

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, date, timedelta

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


# ══════════════════════════════════════════════════
# 수집 규칙 (명세서 4장 「수집 규칙」)
# ══════════════════════════════════════════════════
PAGE_DELAY_MIN = 2.0      # 페이지당 최소 대기 (초)
PAGE_DELAY_MAX = 4.0      # 페이지당 최대 대기 — 무작위 지터
CONCURRENCY = 1           # 동시성 1. 늘리지 마세요
MAX_PAGES_DEFAULT = 0     # 0 = 끝까지

# ★ 표본 상한 — 기본값이 있는 이유
#   명세서는 「페이지네이션을 끝까지」라고 했지만, 실측으로 계산해 보니
#   리뷰 6,030건짜리 제품 하나가 20분입니다. 갈래에 제품 30개면 10시간이고,
#   주 1회 갱신이면 매주 10시간입니다 — 노트북 한 대로는 성립하지 않습니다.
#
#   라벨은 「모집단」이 아니라 「언급률」로 정해집니다. 1,000건이면
#     1단 통과 ~680건 → 2·3단 통과 ~150~300건 → 한달 사용 후기 ~50~70건
#   으로, 명세서의 최소선(모은 후기 30건 · 한달 사용 후기 10건)을 한참 넘습니다.
#   언급률 10%를 잴 때 표본 240건이면 95% 신뢰구간 ±5%p, 전수 6,030건이면 ±2%p.
#   8배 시간을 써서 오차 3%p 를 줄이는 것이라 4개 라벨을 가르는 데 의미가 없습니다.
#
#   ★ 대신 잃는 것: 게시일 군집(체험단 살포)은 전 기간 분포를 봐야 성립합니다.
#     그래서 제품 등록 때 한 번은 --limit-reviews 0 으로 전수를 돌려 확인하고,
#     이후 주간 갱신은 상한 + 증분으로 갑니다.
DEFAULT_LIMIT_REVIEWS = 1000

# 증분 수집에서 「이미 아는 리뷰」를 몇 건 만나면 멈출지.
#   최신순이라 아는 리뷰가 나오면 그 뒤는 전부 아는 것입니다. 다만 리뷰가
#   삭제되거나 순서가 미세하게 흔들릴 수 있어 여유를 둡니다.
SEEN_STOP_HITS = 5
MAX_EMPTY_PAGES = 2       # 빈 페이지가 2번 연속이면 끝으로 봅니다

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 배지 건강도 — 명세서 4장의 경고
#   「배지가 하나도 안 잡히는 제품이 전체의 90%를 넘으면 셀렉터가 깨진 것」
BADGE_NULL_THRESHOLD = 0.90


def polite_sleep():
    time.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))


# ══════════════════════════════════════════════════
# 1단 하드 배제 (코드)
# ══════════════════════════════════════════════════
# ★ main.py 의 hard_filter_review 와 글자 하나까지 같아야 합니다.
#   여기서 걸러 둔 결과를 어드민이 다시 계산하므로, 어긋나면 수치가 달라집니다.

# ★ 「배송·포장만 언급」 규칙은 폐기했습니다 (명세서 2026-09-30 결정).
#
#   바뀐 이유: 배송·포장 이야기는 버리지 않고 「구매 경험」 카테고리로 남깁니다.
#   라벨 계산에서만 빠지고, 화면에는 보여줍니다.
#   「빨리 왔어요」처럼 내용 없는 것은 어차피 짧음 규칙이 잡습니다.
#
#   그리고 이 규칙은 넓히는 순간 위험했습니다. 제품 단어 사전이 프라이팬 전용이라
#   (팬·냄비·코팅·인덕션…) 뷰티·육아 리뷰에는 맞는 단어가 하나도 없습니다.
#   「피부가 진정되는 느낌이고 포장도 꼼꼼했어요」가 통째로 버려졌습니다.
#   갈래마다 사전을 새로 쓰는 대신, 규칙 자체를 없애는 쪽이 맞습니다.


def hard_filter_review(body, written_at=None):
    """1단 하드 배제. (배제여부, 사유).

    ★ 날짜로는 배제하지 않습니다 (명세서 3장).
      네이버는 리뷰 포인트가 있어 대부분 받자마자 씁니다.
      체험단 살포는 게시일 군집 규칙이 따로 담당합니다.
    """
    t = (body or "").strip()
    if not t:
        return (True, "너무짧음")

    stripped = re.sub(r"\s", "", t)
    syllables = len(re.findall(r"[가-힣]", t))

    if len(stripped) >= 15 and syllables < 5:
        return (True, "이모지만")
    if syllables == 0 and len(stripped) >= 3:
        return (True, "이모지만")
    if len(stripped) < 15:
        return (True, "너무짧음")
    return (False, None)


def _ngrams(text, n=4):
    t = re.sub(r"\s", "", (text or ""))
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def jaccard(a, b, n=4):
    ga, gb = _ngrams(a, n), _ngrams(b, n)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def find_dup_clusters(reviews, threshold=0.45, n=4):
    """복붙 클러스터 — 4-gram 자카드 ≥ 0.45. {review_key: cluster_id}

    ★ 6,030건짜리 제품에서 1,800만 쌍을 비교해야 합니다. 실측으로 최악의 경우
      20분이 걸렸습니다 — 크롤링을 마친 뒤 멈춘 것처럼 보입니다. 두 가지로 줄입니다.

      ① 4-gram 집합을 리뷰마다 한 번만 만듭니다.
         예전에는 jaccard() 를 부를 때마다 양쪽을 다시 만들어, 쌍마다 집합을
         두 번씩 새로 쌓았습니다. 여기가 시간의 대부분이었습니다.

      ② 크기 비율로 미리 자릅니다. 자카드는 |교집합|/|합집합| 이므로
         작은 집합 / 큰 집합 보다 클 수 없습니다. 크기가 2.2배(=1/0.45) 넘게
         차이 나면 0.45 에 도달할 수 없어 비교 자체가 필요 없습니다.
         부등식이라 놓치는 쌍이 없습니다 — 근사가 아닙니다.
    """
    items = []
    for r in reviews:
        rid = r.get("review_key")
        if not rid:
            continue
        g = _ngrams(r.get("body") or "", n)
        if g:
            items.append((rid, g))

    # 크기 순으로 세워 두면, 비율 조건을 벗어나는 순간 뒤는 다 볼 필요가 없습니다
    items.sort(key=lambda t: len(t[1]))

    # 6,000건이면 몇 분 걸립니다. 말없이 멈춰 있으면 죽은 줄 압니다.
    verbose = len(items) >= 1500
    if verbose:
        print(f"    · 복붙 검사 {len(items):,}건 (몇 분 걸립니다)", end="", flush=True)
    step = max(1, len(items) // 20)

    cluster_of = {}
    next_id = 1
    for i, (rid, ga) in enumerate(items):
        if verbose and i % step == 0:
            print(".", end="", flush=True)
        if rid in cluster_of:
            continue
        la = len(ga)
        members = [rid]
        for oid, gb in items[i + 1:]:
            if len(gb) * threshold > la:      # |a| / |b| < threshold → 불가능
                break
            if oid in cluster_of:
                continue
            inter = len(ga & gb)
            if inter and inter / (la + len(gb) - inter) >= threshold:
                members.append(oid)
        if len(members) > 1:
            cid = f"dup{next_id}"
            next_id += 1
            for mid in members:
                cluster_of[mid] = cid

    if verbose:
        print(f" 끝 · {len(set(cluster_of.values()))}개 묶음")
    return cluster_of


def detect_date_cluster(dates, ratio=0.40, window_days=7):
    """게시일 군집 — 특정 7일 구간에 40% 초과. 체험단 살포 신호."""
    ds = sorted(d for d in dates if d)
    if len(ds) < 10:
        return (False, None)
    total = len(ds)
    for i, start in enumerate(ds):
        end = start + timedelta(days=window_days)
        cnt = sum(1 for d in ds[i:] if d <= end)
        if cnt / total > ratio:
            return (True, f"{start} ~ {end} 구간에 {cnt}/{total}건")
    return (False, None)


# ══════════════════════════════════════════════════
# 값 다듬기
# ══════════════════════════════════════════════════
def to_int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None


def parse_review_date(raw):
    """네이버 리뷰 작성일을 date 로. 「26.09.23.」 → 2026-09-23

    ★ 두 자리 연도를 20xx 로 봅니다. 브랜드스토어 리뷰에 1900년대는 없습니다.
    """
    if not raw:
        return None
    s = str(raw).strip()

    m = re.search(r"(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", s)
        if not m:
            return None
        y, mo, d = 2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))

    try:
        return date(y, mo, d)
    except ValueError:
        return None


def hash_author(masked):
    """마스킹된 작성자 ID를 해시로. 원문은 어디에도 남기지 않습니다.

    ★ 이미 「sunn****」 처럼 마스킹된 값이지만, 그대로 쌓으면 같은 사람의
      리뷰를 스토어 사이에서 이어 붙일 수 있게 됩니다. 명세서대로 해시만 씁니다.
    """
    if not masked:
        return None
    return hashlib.sha256(masked.strip().encode("utf-8")).hexdigest()[:24]


def clean_option_text(raw):
    """옵션 버튼 문구에서 값만 남깁니다.

      「선택: 01. 헤이즐 IH 팬 24cm」        → 01. 헤이즐 IH 팬 24cm
      「더치커피 선택: 05_케냐AA 1000ml」    → 05_케냐AA 1000ml

    ★ 옵션 그룹명이 앞에 붙는 제품이 있어 「선택:」 뒤만 취합니다.
      구분자가 없으면 원문을 그대로 둡니다 — 지어내지 않습니다.
    """
    if not raw:
        return None
    t = re.sub(r"\s+", " ", str(raw)).strip()
    if "선택:" in t:
        t = t.rsplit("선택:", 1)[1].strip()
    return t or None


def _norm_option(text):
    """옵션명 비교용. 공백·기호를 없애 표기 차이를 흡수합니다."""
    t = (text or "").strip().lower()
    return re.sub(r"[\s.·,/\-()_]+", "", t)


def match_option(review_option, target_option):
    """대표 옵션과 일치하는가. (match, unsure)

    ★ 애매하면 unsure 로 넘겨 사람이 봅니다 (명세서 4장).
      28cm 팬의 라벨에 26cm 후기가 섞이면 안 됩니다.
    """
    if not target_option:
        return (None, False)          # 대표 옵션이 정해지지 않은 제품 — 판정 보류
    if not review_option:
        return (None, True)           # 리뷰에 옵션 표기가 없음 → 사람이 확인
    a, b = _norm_option(review_option), _norm_option(target_option)
    if not a or not b:
        return (None, True)
    if a == b:
        return (True, False)
    if a in b or b in a:
        return (True, False)

    # 치수가 둘 다 있고 서로 다르면 확실히 다른 옵션입니다
    sa = set(re.findall(r"\d{1,3}(?:cm|mm|ml|l|호|인치)", a))
    sb = set(re.findall(r"\d{1,3}(?:cm|mm|ml|l|호|인치)", b))
    if sa and sb and not (sa & sb):
        return (False, False)

    return (False, True)              # 다른 것 같지만 확실하지 않음


# ══════════════════════════════════════════════════
# 화면에서 리뷰 뜯어내기
# ══════════════════════════════════════════════════
# ★ 리뷰 20~30건을 한 건씩 Playwright 로 훑으면 왕복이 수백 번입니다.
#   한 번의 evaluate 로 브라우저 안에서 다 뜯어 JSON 으로 받습니다.
#
# ★ textContent 를 씁니다. innerText 는 CSS 로 잘린(line-clamp) 본문을
#   잘린 채로 돌려줍니다. 「더보기」를 누르지 않아도 DOM 에는 전문이 있습니다.

#
# ★ offset 이 있는 이유 — 이 팝업은 무한 스크롤입니다 (2026-09-25 실측).
#   스크롤할 때마다 리뷰가 20건씩 「덧붙습니다」. 매번 전부 다시 뜯으면
#   5,944건짜리 제품에서 300라운드 × 평균 3,000건 = 90만 건을 헛돈 뜯고,
#   오가는 JSON 이 수백 MB 가 됩니다. 새로 붙은 것만 뜯습니다.

#
# ★ 팝업만 봅니다 (실측 2026-09-25).
#   「리뷰 전체보기」 팝업을 열어도 페이지 본문의 리뷰 목록이 DOM 에 그대로
#   남아 있습니다. 문서 전체를 훑으면 두 목록이 섞이고, 같은 id 라 중복 제거에
#   가려져 조용히 어긋납니다 — 특히 「뒤에 덧붙는다」를 가정하는 증분 파싱이 깨집니다.
#   팝업의 리뷰에는 data-shp-inventory 가 「_l」(layer) 로 끝납니다. 그것만 씁니다.
#   ★ 정렬 버튼이 화면에 두 벌 보이는 것도 같은 원인입니다.

_JS_PICK_CARDS = r"""
  const _pick = () => {
    const sel = 'div[data-shp-area-id="review"][data-shp-contents-id]';
    const layer = document.querySelectorAll(sel + '[data-shp-inventory$="_l"]');
    if (layer.length) return { nodes: [...layer], scope: 'layer' };
    return { nodes: [...document.querySelectorAll(sel)], scope: 'all' };
  };
"""

_JS_COUNT = r"""
() => {
""" + _JS_PICK_CARDS + r"""
  const p = _pick();
  return { count: p.nodes.length, scope: p.scope };
}
"""

_JS_EXTRACT = r"""
(offset) => {
""" + _JS_PICK_CARDS + r"""
  const out = [];
  const all = _pick().nodes;
  const nodes = offset ? all.slice(offset) : all;

  for (const el of nodes) {
    const rid = el.getAttribute('data-shp-contents-id');
    if (!rid) continue;

    // 별점·작성자·배지는 리뷰 블록의 형제에 있습니다. 한 칸 올라갑니다.
    const card = el.parentElement || el;

    // ── 본문 ──
    const p = card.querySelector('p[id^="review_content_"]')
           || el.querySelector('p[id^="review_content_"]');
    const body = p ? (p.textContent || '').replace(/\s+/g, ' ').trim() : null;
    const innerLen = p ? (p.innerText || '').trim().length : 0;

    // ── 옵션 ──
    const optBtn = card.querySelector('div[id^="review_option_"] button');
    const optionRaw = optBtn ? (optBtn.textContent || '').trim() : null;

    // ── 메타 블록 (별점·배지·작성자·작성일) ──
    // 리뷰 블록이 아닌 쪽에서 별 svg 를 가진 요소를 찾습니다.
    let ratingBox = null;
    for (const svg of card.querySelectorAll('svg')) {
      if (el.contains(svg)) continue;              // 리뷰 블록 안(더보기 화살표 등)은 건너뜀
      const par = svg.parentElement;
      if (!par) continue;
      const txt = (par.textContent || '').trim();
      if (/^[0-5]/.test(txt)) { ratingBox = par; break; }
    }

    let rating = null, badges = [];
    if (ratingBox) {
      const c = ratingBox.cloneNode(true);
      c.querySelectorAll('span, em, button, a').forEach(n => n.remove());
      const m = (c.textContent || '').match(/([0-5](?:\.\d)?)/);
      if (m) rating = parseFloat(m[1]);
      ratingBox.querySelectorAll('em').forEach(n => {
        const t = (n.textContent || '').trim();
        if (t) badges.push(t);
      });
    }
    if (!badges.length) {
      // 배지가 rating 블록 밖으로 옮겨간 빌드 대비 — 리뷰 블록 밖의 em 전체
      card.querySelectorAll('em').forEach(n => {
        if (el.contains(n)) return;
        const t = (n.textContent || '').trim();
        if (t && t.length <= 10) badges.push(t);
      });
    }

    // ── 작성자 · 작성일 ──
    let author = null, written = null;
    const infoRow = ratingBox ? ratingBox.nextElementSibling : null;
    const spanPool = infoRow ? infoRow.querySelectorAll('span')
                             : card.querySelectorAll('span');
    for (const s of spanPool) {
      if (el.contains(s)) continue;
      if (s.querySelector('*')) continue;          // 자식이 있으면 래퍼입니다
      const t = (s.textContent || '').trim();
      if (!t) continue;
      if (!written && /^\d{2,4}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2}\.?$/.test(t)) {
        written = t; continue;
      }
      if (!author && t.includes('*')) { author = t; continue; }
    }

    // ── 사진 ──
    const hasPhoto = !!card.querySelector('img[alt="review_image"]');

    out.push({
      rid, body, innerLen, optionRaw, rating,
      badges, author, written, hasPhoto,
      hasMore: !!card.querySelector('a[aria-labelledby]')
    });
  }
  return out;
}
"""


def count_reviews_in_dom(page, with_scope=False):
    """팝업에 실려 있는 리뷰 카드 수. 무한 스크롤이 진행됐는지 판정합니다."""
    try:
        res = page.evaluate(_JS_COUNT) or {}
        cnt, scope = int(res.get("count") or 0), res.get("scope")
    except Exception:
        cnt, scope = 0, None
    return (cnt, scope) if with_scope else cnt


def extract_reviews_from_page(page, debug=False, offset=0):
    """팝업의 리뷰를 뜯어냅니다. offset 이후만 읽을 수 있습니다. [dict] 반환."""
    try:
        raw = page.evaluate(_JS_EXTRACT, offset)
    except Exception as e:
        print(f"      ⚠️ 리뷰 추출 실패: {str(e)[:80]}")
        return []

    rows = []
    for r in raw:
        badges = [b for b in (r.get("badges") or []) if b]
        badges_raw = " ".join(badges) if badges else None
        body = r.get("body")
        option_text = clean_option_text(r.get("optionRaw"))

        # 본문이 CSS 로 잘려 있었는지 기록합니다. textContent 를 쓰므로
        # 값 자체는 전문이지만, 셀렉터가 깨졌을 때 신호가 됩니다.
        clipped = bool(body) and r.get("innerLen", 0) and len(body) > r["innerLen"] + 10

        rows.append({
            "review_key": f"nv:{r['rid']}",
            "nv_review_id": r["rid"],
            "body": body,
            "rating": r.get("rating"),
            "written_at": parse_review_date(r.get("written")),
            "written_raw": r.get("written"),
            "author_hash": hash_author(r.get("author")),
            "has_photo": bool(r.get("hasPhoto")),
            "is_month": any("한달" in b for b in badges),
            "is_repeat": any("재구매" in b for b in badges),
            "badges_raw": badges_raw,
            "option_text": option_text,
            "body_clipped": clipped,
        })

    if debug:
        print(f"      · 추출 {len(rows)}건")
        for r in rows[:2]:
            print(f"        {r['review_key']} ★{r['rating']} "
                  f"{r['written_at']} [{r['badges_raw']}] "
                  f"opt={r['option_text']} / {(r['body'] or '')[:40]}")
    return rows


# ══════════════════════════════════════════════════
# 팝업 열기 · 옵션 · 정렬 · 페이지 넘기기
# ══════════════════════════════════════════════════
def open_review_popup(page, product_url, debug=False):
    """제품 상세 → 리뷰 전체보기 팝업.

    ★ 팝업 안에만 옵션 필터가 있고, 팝업과 페이지 목록은 클래스명이 전혀 다릅니다.
      수집은 반드시 팝업에서 합니다.
    """
    try:
        page.goto(f"{product_url}#REVIEW", timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"      ⚠️ 리뷰 페이지 열기 실패: {str(e)[:60]}")
        return False

    for pat in ("리뷰 전체보기", "리뷰 더보기", "전체 리뷰"):
        try:
            btn = page.get_by_text(re.compile(pat)).first
            if btn and btn.is_visible():
                btn.click(timeout=5000)
                page.wait_for_timeout(2500)
                if debug:
                    print(f"      · 팝업 열기: 「{pat}」")
                return True
        except Exception:
            continue

    print("      ⚠️ 「리뷰 전체보기」 버튼을 못 찾았습니다.")
    return False


def option_match_ratio(rows, target_option):
    """지금 보이는 리뷰 중 대표 옵션과 일치하는 비율. 필터가 실제로 걸렸는지 봅니다."""
    checked = [r for r in rows if r.get("option_text")]
    if not checked:
        return None
    hit = sum(1 for r in checked
              if match_option(r["option_text"], target_option)[0] is True)
    return hit / len(checked)


def apply_option_filter(page, option_name, debug=False):
    """팝업에서 대표 옵션 하나만 체크합니다.

    ★ 인덱스(checkbox_0_N)로 추측하지 않습니다 — 제품마다 순서가 어긋납니다.
    ★ 텍스트만으로 찾지도 않습니다 — 아래 리뷰 본문의 「선택: …」을 집습니다.
      → 팝업의 체크박스 라벨만 모아 그 안에서 고릅니다.
    """
    try:
        page.get_by_text(re.compile(r"상품옵션")).first.click(timeout=5000)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"      ⚠️ 상품옵션 열기 실패: {str(e)[:60]}")
        return False

    want = _norm_option(option_name)
    try:
        labels = page.locator('label[for^="checkbox_"]')
        n = labels.count()
        for k in range(n):
            lb = labels.nth(k)
            try:
                txt = _norm_option(lb.inner_text())
            except Exception:
                continue
            if txt and (txt == want or want in txt or txt in want):
                lb.scroll_into_view_if_needed(timeout=3000)
                lb.click(timeout=5000)
                page.wait_for_timeout(1800)
                if debug:
                    print(f"      · 옵션 체크: {option_name}")

                # ★ 체크만으로는 목록이 걸러지지 않습니다 (실측 2026-09-25).
                #   체크박스는 「5,944개의 리뷰보기」 버튼의 숫자만 바꾸고,
                #   그 버튼을 눌러야 목록에 적용됩니다.
                #   이걸 빼먹어 「옵션 체크 성공」인데 28cm 리뷰가 섞여 들어왔습니다.
                try:
                    res = page.evaluate(_JS_CLICK_IN_POPUP, {
                        "pattern": r"개의?리뷰보기",
                        "notPattern": r"포토리뷰",
                    })
                    if debug:
                        print(f"      · 옵션 적용 버튼: "
                              f"{'「' + res.get('text', '') + '」 클릭' if res.get('ok') else res.get('reason')}")
                    page.wait_for_timeout(2500)
                except Exception as e:
                    if debug:
                        print(f"      · 옵션 적용 버튼 실패: {str(e)[:60]}")

                # 실제로 걸러졌는지 확인합니다
                ratio = option_match_ratio(extract_reviews_from_page(page), option_name)
                if ratio is None:
                    if debug:
                        print("      · 옵션 표기가 없어 적용 여부를 확인 못 했습니다")
                    return False
                if ratio >= 0.9:
                    if debug:
                        print(f"      · 옵션 필터 적용 확인 (일치 {ratio*100:.0f}%)")
                    return True
                print(f"    ⚠️ 옵션 필터가 안 걸렸습니다 (일치 {ratio*100:.0f}%).")
                print("       다른 옵션 리뷰가 섞입니다 — option_match=false 로 표시해 둡니다.")
                return False
        found = []
        for k in range(min(n, 8)):
            try:
                found.append(labels.nth(k).inner_text().strip()[:24])
            except Exception:
                pass
        print(f"      ⚠️ 옵션을 못 찾았습니다. 기대='{option_name[:24]}' / 화면={found}")
    except Exception as e:
        print(f"      ⚠️ 옵션 탐색 실패: {str(e)[:60]}")
    return False


# 팝업 안의 정렬 버튼을 골라 누릅니다.
#   페이지 본문에도 같은 버튼이 있고 팝업에 가려져 있어, 「보이는가」만으로는
#   부족합니다. 지금 읽고 있는 리뷰 카드의 조상 중 정렬 버튼을 품은 가장 가까운
#   것을 찾아, 그 안의 버튼을 누릅니다. = 팝업 자신의 버튼.
_JS_CLICK_IN_POPUP = r"""
(arg) => {
"""  + _JS_PICK_CARDS + r"""
  const want = new RegExp(arg.pattern);
  const skip = arg.notPattern ? new RegExp(arg.notPattern) : null;
  const vis = (n) => !!(n.offsetParent || n.getClientRects().length);
  const norm = (n) => (n.innerText || n.textContent || '').replace(/\s+/g, '');

  let btns = [...document.querySelectorAll(
    'button, a, [role="button"], label'
  )].filter(n => want.test(norm(n)) && !(skip && skip.test(norm(n))));

  const total = btns.length;
  const shown = btns.filter(vis);
  if (!shown.length) {
    return { ok: false, reason: `후보 ${total}개인데 보이는 것이 없음`, total };
  }

  let pick = shown[shown.length - 1];
  const card = _pick().nodes[0];
  if (card) {
    let n = card.parentElement;
    while (n && n !== document.body) {
      const inside = shown.filter(b => n.contains(b));
      if (inside.length) { pick = inside[inside.length - 1]; break; }
      n = n.parentElement;
    }
  }

  pick.scrollIntoView({ block: 'center' });
  pick.click();
  return {
    ok: true,
    text: (pick.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 30),
    total, shown: shown.length
  };
}
"""


def set_sort_latest(page, ids_before, debug=False):
    """정렬을 최신순으로 바꿉니다.

    ★ 랭킹순으로 페이지를 넘기면 순서가 흔들려 같은 리뷰를 다시 보거나 빠뜨립니다.
      최신순은 순서가 고정이라 페이지네이션이 안전합니다.
      (명세서: 「최신순이 기본이면 그대로」 — 기본이 랭킹순이면 바꿉니다)
    """
    # 실측(2026-09-25): 정렬 버튼의 이름은 「최신순」이 아니라 「최신순 정렬하기」이고
    #   data-shp-area-id="sortfilter" 가 붙어 있습니다.
    #
    # ★ 같은 버튼이 화면에 두 벌 있습니다 — 팝업 안의 것과, 팝업 뒤에 가려진
    #   페이지 본문의 것. Playwright 의 .first 는 DOM 순서로 집으므로 가려진
    #   쪽을 골라 「보이지 않음」으로 타임아웃이 납니다. 여기서 실패했습니다.
    #   → 지금 읽고 있는 리뷰 카드에서 조상을 거슬러 올라가, 그 안에 있는
    #     정렬 버튼을 고릅니다. 그게 팝업의 버튼입니다.
    candidates = [
        ('팝업 스코프', None),     # 아래 JS 로
        ('속성', '[data-shp-area-id="sortfilter"]'),
        ('속성2', '[data-shp-area$=".sortfilter"]'),
        ('역할', 'role'),
    ]

    def _dates_desc(rows):
        """작성일이 내림차순인가 — 최신순이 실제로 걸렸는지의 직접 증거.

        ★ id 집합 변화로 판정하면 안 됩니다. 정렬은 「순서」만 바꾸는 동작이라
          리뷰가 20건 이하인 제품에서는 집합이 그대로여서 실패로 오판합니다.
        """
        ds = [r["written_at"] for r in rows if r.get("written_at")]
        if len(ds) < 3:
            return None                     # 판정 불가
        return all(ds[i] >= ds[i + 1] for i in range(len(ds) - 1))

    def _verify_sorted():
        rows = extract_reviews_from_page(page)
        desc = _dates_desc(rows)
        if desc is True:
            return True
        if desc is False:
            return False
        # 날짜가 3건 미만이라 판정 못 하면, 목록이 바뀌었는지로 갈음합니다
        ids_after = {r["review_key"] for r in rows}
        return bool(ids_after) and ids_after != ids_before

    for label, sel in candidates:
        try:
            if sel is None:
                res = page.evaluate(_JS_CLICK_IN_POPUP, {
                    "pattern": r"최신순",
                    "notPattern": r"(평점|랭킹)",
                })
                if not res.get("ok"):
                    if debug:
                        print(f"      · 최신순({label}) 후보 없음: {res.get('reason')}")
                    continue
                if debug:
                    print(f"      · 최신순({label}) 클릭 「{res.get('text')}」"
                          f" · 화면의 같은 버튼 {res.get('total')}개")
            else:
                if sel == 'role':
                    loc = page.get_by_role("button", name=re.compile(r"최신순"))
                else:
                    loc = page.locator(sel).filter(has_text=re.compile(r"최신순"))
                # ★ 가려진 것을 집지 않도록 보이는 것만 남깁니다
                loc = loc.locator("visible=true")
                n = loc.count()
                if not n:
                    if debug:
                        print(f"      · 최신순({label}) 보이는 후보 없음")
                    continue
                el = loc.last
                el.scroll_into_view_if_needed(timeout=3000)
                el.click(timeout=5000)
                if debug:
                    print(f"      · 최신순({label}) 클릭 · 보이는 후보 {n}개")
        except Exception as e:
            # ★ 예외를 조용히 넘기다가 「왜 실패했는지 모르는」 상태를 만들었습니다.
            if debug:
                print(f"      · 최신순({label}) 실패: {str(e)[:80]}")
            continue

        page.wait_for_timeout(2500)
        if _verify_sorted():
            if debug:
                print(f"      · 최신순 적용 ({label}) — 작성일 내림차순 확인")
            return True
        if debug:
            print(f"      · 최신순({label}) 눌렀지만 작성일이 내림차순이 아닙니다")

    if debug:
        print("      · 최신순 적용 실패 — 랭킹순 그대로입니다")
    return False


# 팝업 안에서 스크롤되는 컨테이너를 찾아 끝까지 내립니다.
# 페이저가 없고 무한 스크롤인 빌드를 대비한 경로입니다.
_JS_SCROLL_POPUP = r"""
() => {
"""  + _JS_PICK_CARDS + r"""
  const rev = _pick().nodes[0];
  if (!rev) { window.scrollTo(0, document.body.scrollHeight); return 'window'; }
  let n = rev;
  while (n && n !== document.body) {
    const st = getComputedStyle(n);
    if (n.scrollHeight > n.clientHeight + 40 &&
        /(auto|scroll)/.test(st.overflowY)) {
      n.scrollTop = n.scrollHeight;
      return 'container';
    }
    n = n.parentElement;
  }
  window.scrollTo(0, document.body.scrollHeight);
  return 'window';
}
"""


# ★ 절대 누르면 안 되는 버튼들 (실측 2026-09-25)
#     「다음」「이전」  area=clinext / clprev  → 리뷰 목록이 아니라 사진 캐러셀 화살표
#     「포토리뷰 더보기 + 10,055」            → 사진 있는 리뷰만 남겨 표본을 망칩니다
#     「이 구매자의 처음 리뷰 보기」            → 다른 제품의 리뷰로 넘어갑니다
#     「서비스 더보기」                        → 네이버 전역 GNB
_FORBIDDEN_CONTROL = re.compile(r"(포토리뷰|구매자|서비스|평점|이전)")


def load_more_reviews(page, round_no, count_before, ids_before, debug=False):
    """리뷰를 더 불러옵니다. (새 rows, 새 총개수) 또는 None.

    ★ 이 팝업은 페이지네이션이 아니라 무한 스크롤입니다 (실측 2026-09-25).
      페이저 숫자가 화면에 하나도 없고, 스크롤 한 번에 20건이 덧붙습니다.
      그래서 스크롤을 먼저 시도하고, 덧붙은 만큼만 뜯습니다.

    ★ 「했다」와 「됐다」를 구분합니다. 클릭·스크롤이 성공해도 목록이
      안 변하는 일이 흔해서, 카드 수와 id 집합으로 확인합니다.
    """
    def _verify(label):
        page.wait_for_timeout(2200)
        count_after = count_reviews_in_dom(page)

        # ① 덧붙은 경우 (무한 스크롤) — 새로 붙은 것만 뜯습니다
        if count_after > count_before:
            rows = extract_reviews_from_page(page, offset=count_before)
            if rows:
                if debug:
                    print(f"      · {round_no}차 로드 ({label}) · +{len(rows)}건")
                return (rows, count_after)

        # ② 목록이 바뀐 경우 (페이저가 있는 다른 빌드) — 전부 다시 뜯습니다
        rows = extract_reviews_from_page(page)
        ids_after = {r["review_key"] for r in rows}
        if ids_after and ids_after != ids_before:
            if debug:
                print(f"      · {round_no}차 로드 ({label}) · 목록 교체 {len(rows)}건")
            return (rows, count_after)

        if debug:
            print(f"      · {label} → 변화 없음 (카드 {count_before}→{count_after})")
        return None

    def _click_safe(getter):
        """금지 버튼을 피해서 누릅니다."""
        def inner():
            loc = getter()
            n = min(loc.count(), 6)
            for k in range(n):
                el = loc.nth(k)
                try:
                    txt = (el.inner_text() or "").strip()
                except Exception:
                    continue
                if _FORBIDDEN_CONTROL.search(txt):
                    continue
                el.scroll_into_view_if_needed(timeout=3000)
                el.click(timeout=5000)
                return
            raise RuntimeError("누를 수 있는 버튼이 없습니다")
        return inner

    attempts = [
        # 실측으로 확인된 경로 — 이게 먹힙니다
        ("스크롤", lambda: page.evaluate(_JS_SCROLL_POPUP)),
        # 「더보기」 버튼으로 이어 붙이는 빌드
        ("더보기", _click_safe(lambda: page.locator(
            '[data-shp-area-id="more"], [data-shp-area$=".more"]'))),
        ("더보기(역할)", _click_safe(lambda: page.get_by_role(
            "button", name=re.compile(r"더\s*보기")))),
        # 페이저가 있는 빌드 대비
        ("번호 링크", _click_safe(lambda: page.get_by_role(
            "link", name=str(round_no + 1), exact=True))),
        ("번호 버튼", _click_safe(lambda: page.get_by_role(
            "button", name=str(round_no + 1), exact=True))),
    ]

    for label, act in attempts:
        try:
            act()
        except Exception:
            continue
        got = _verify(label)
        if got:
            return got
    return None


# ══════════════════════════════════════════════════
# 진단 — 화면의 조작 요소를 전부 훑습니다
# ══════════════════════════════════════════════════
# ★ 개발자 도구를 뒤지지 않아도 되게 하는 장치입니다.
#   리뷰 블록 밖의 짧은 버튼·링크만 모아 보여 줍니다 —
#   페이저(1 2 3 …)와 정렬(랭킹순·최신순)이 여기 나옵니다.
_JS_PROBE = r"""
() => {
"""  + _JS_PICK_CARDS + r"""
  const cards = _pick().nodes.map(r => r.parentElement || r);
  const inCard = (n) => cards.some(c => c.contains(n));

  const seen = new Set(), out = [];
  for (const el of document.querySelectorAll('a, button, [role="button"], select, option')) {
    if (inCard(el)) continue;
    const visible = !!(el.offsetParent || el.getClientRects().length);
    if (!visible) continue;
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 24) continue;
    // ★ 같은 텍스트의 버튼이 두 벌 있는 것 자체가 중요한 정보입니다
    //   (팝업 안 / 팝업 뒤 페이지 본문). 합치지 않고 개수를 셉니다.
    const key = el.tagName + '|' + t;
    if (seen.has(key)) {
      const prev = out.find(o => o.tag === el.tagName.toLowerCase() && o.text === t);
      if (prev) prev.dup = (prev.dup || 1) + 1;
      continue;
    }
    seen.add(key);
    out.push({
      tag: el.tagName.toLowerCase(),
      text: t,
      aria_label: el.getAttribute('aria-label') || '',
      aria_current: el.getAttribute('aria-current') || '',
      aria_expanded: el.getAttribute('aria-expanded') || '',
      shp_area: el.getAttribute('data-shp-area-id') || el.getAttribute('data-shp-area') || '',
      href: (el.getAttribute('href') || '').slice(0, 48),
      cls: String(el.className || '').slice(0, 32)
    });
  }
  return { total_reviews: cards.length, controls: out };
}
"""


def probe_popup(page, debug=True):
    """팝업의 조작 요소를 뽑아 보여 줍니다. 클릭하지 않습니다."""
    try:
        res = page.evaluate(_JS_PROBE)
    except Exception as e:
        print(f"  ⚠️ 진단 실패: {str(e)[:80]}")
        return

    ctrls = res.get("controls") or []
    print(f"\n  화면의 리뷰 {res.get('total_reviews')}건 · 조작 요소 {len(ctrls)}개")

    def show(title, rows):
        if not rows:
            print(f"\n  [{title}] 없음")
            return
        print(f"\n  [{title}]")
        for c in rows:
            extra = " ".join(
                f"{k}={v}" for k, v in (
                    ("aria", c["aria_label"]), ("cur", c["aria_current"]),
                    ("exp", c["aria_expanded"]), ("area", c["shp_area"]),
                    ("href", c["href"]),
                ) if v
            )
            dup = f"  ×{c['dup']}" if c.get("dup") else ""
            print(f"    <{c['tag']}> 「{c['text']}」{dup}  class={c['cls']}  {extra}")

    nums = [c for c in ctrls if re.fullmatch(r"\d{1,3}", c["text"])]
    navs = [c for c in ctrls if re.search(r"(다음|이전|처음|마지막|더보기|더 보기)", c["text"])]
    sorts = [c for c in ctrls if re.search(r"(랭킹|최신|평점|추천|순)", c["text"])]
    rest = [c for c in ctrls if c not in nums + navs + sorts]

    show("페이저로 보이는 숫자", nums)
    show("이동 버튼", navs)
    show("정렬", sorts)
    show("나머지", rest[:25])
    print()


# ══════════════════════════════════════════════════
# 제품 하나 수집
# ══════════════════════════════════════════════════
def read_seen_keys(path):
    """이미 수집한 리뷰 key 를 읽습니다 (증분 수집용).

    ★ 이전 수집 CSV 든, 어드민에서 내린 목록이든, review_key 컬럼만 있으면 됩니다.
    """
    keys = set()
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            head = f.readline()
            f.seek(0)
            delim = "\t" if head.count("\t") > head.count(",") else ","
            for row in csv.DictReader(f, delimiter=delim):
                k = (row.get("review_key") or "").strip()
                if k and k != "review_key":
                    keys.add(k)
    except FileNotFoundError:
        print(f"⚠️ --seen 파일을 찾지 못했습니다: {path}")
    return keys


def collect_product_reviews(page, prod, limit_pages=0, debug=False, limit_reviews=0,
                            seen_keys=None):
    """제품 하나의 리뷰를 팝업에서 끝까지 긁습니다."""
    name = prod.get("name") or prod.get("nv_product_no") or "?"
    target_option = (prod.get("nv_option_name") or "").strip() or None
    product_url = prod.get("product_url")

    print(f"\n  ▸ {prod.get('brand','?')} · {name[:40]}")
    if target_option:
        print(f"    대표 옵션: {target_option}")
    else:
        print("    ⚠️ 대표 옵션이 비어 있습니다 — 옵션 일치 판정을 보류합니다")

    if not product_url:
        print("    ⚠️ product_url 이 없습니다. 건너뜁니다.")
        return []

    known = prod.get("nv_reviews") or prod.get("nv_reviews_total")
    try:
        known = int(re.sub(r"[^\d]", "", str(known))) if known else 0
    except Exception:
        known = 0
    target = min(known, limit_reviews) if (known and limit_reviews) else (known or limit_reviews)
    if target:
        rounds = max(1, target // 20)
        capped = " (표본 상한)" if limit_reviews and known and limit_reviews < known else ""
        print(f"    예상: 리뷰 {target:,}건{capped} ≈ {rounds}회 로드 ≈ {rounds * 3 / 60:.1f}분"
              + (f" · 전체 {known:,}건" if capped else ""))

    if not open_review_popup(page, product_url, debug):
        return []

    n0, scope = count_reviews_in_dom(page, with_scope=True)
    if debug:
        print(f"      · 리뷰 카드 {n0}개 · 범위={scope}")
    if scope == "all":
        # ★ 팝업 리뷰(data-shp-inventory 가 _l 로 끝남)를 못 찾았습니다.
        #   페이지 본문 목록과 섞일 수 있어, 증분 파싱의 전제가 깨집니다.
        print("    ⚠️ 팝업 리뷰를 구분하지 못했습니다 (범위=all).")
        print("       페이지 본문 목록과 섞일 수 있습니다 — --probe 로 확인하세요.")

    first = extract_reviews_from_page(page, debug)
    if not first:
        print("    ⚠️ 팝업에서 리뷰를 하나도 못 읽었습니다. --headful --debug 로 확인하세요.")
        return []

    ids_seen = {r["review_key"] for r in first}

    # ★ 옵션 필터와 최신순은 서로를 되돌립니다 (실측 2026-09-25).
    #   최신순을 누르면 옵션 필터가 풀려 28cm 리뷰가 섞여 들어왔습니다.
    #   그래서 「걸고 → 확인하고 → 풀렸으면 다시 걸기」를 두 번까지 돕니다.
    # ★ 순서가 중요합니다: 최신순 먼저, 옵션 필터 나중.
    #   정렬을 누르면 옵션 필터가 풀리지만(실측), 그 반대는 일어나지 않습니다.
    #   처음엔 옵션부터 걸었다가 정렬 한 번에 다 풀려 28cm 리뷰가 섞였습니다.
    option_applied = False
    sorted_latest = False

    def _is_desc(rs):
        ds = [r["written_at"] for r in rs if r.get("written_at")]
        if len(ds) < 3:
            return True                 # 판정 불가는 통과로 봅니다
        return all(ds[i] >= ds[i + 1] for i in range(len(ds) - 1))

    for attempt in range(2):
        if not sorted_latest:
            sorted_latest = set_sort_latest(page, ids_seen, debug)
            ids_seen = {r["review_key"] for r in extract_reviews_from_page(page)}

        if target_option and not option_applied:
            option_applied = apply_option_filter(page, target_option, debug)

        rows_now = extract_reviews_from_page(page, debug and attempt == 0)

        # 옵션을 거는 동안 정렬이 풀렸으면 다시 겁니다
        if sorted_latest and not _is_desc(rows_now):
            print("    ⚠️ 옵션 적용 후 최신순이 풀렸습니다. 다시 겁니다")
            sorted_latest = False
            continue
        # 정렬 때문에 옵션이 풀렸으면 다시 겁니다
        if target_option and option_applied:
            ratio = option_match_ratio(rows_now, target_option)
            if ratio is not None and ratio < 0.9:
                print(f"    ⚠️ 옵션 필터가 풀렸습니다 (일치 {ratio*100:.0f}%). 다시 겁니다")
                option_applied = False
                continue
        break

    rows = extract_reviews_from_page(page, False) or first
    if target_option and not option_applied:
        print("    ℹ️ 옵션 필터 없이 수집합니다 — 다른 옵션 리뷰는 option_match=false 로 남습니다")

    seen = seen_keys or set()
    collected = {}
    seen_hits = 0

    def _take(batch):
        """새 리뷰만 담습니다. (새로 담은 수, 이미 아는 수)"""
        nonlocal seen_hits
        new = known = 0
        for r in batch:
            k = r["review_key"]
            if k in seen:
                known += 1
                continue
            if k not in collected:
                collected[k] = r
                new += 1
        seen_hits += known
        return (new, known)

    _take(rows)

    dom_count = count_reviews_in_dom(page)
    page_no = 1
    empty_streak = 0
    complete = False          # 끝까지 읽었는가 — 게시일 군집 판정의 전제
    while True:
        # ★ 증분 수집 — 최신순이라 아는 리뷰가 나오면 그 뒤는 전부 아는 것입니다.
        #   주 1회 갱신에서 새 리뷰 100건이면 5회 스크롤, 15초면 끝납니다.
        if seen and seen_hits >= SEEN_STOP_HITS:
            print(f"    · 이미 아는 리뷰 {seen_hits}건을 만났습니다 — 새 리뷰만 받고 멈춥니다")
            complete = True
            break
        if limit_pages and page_no >= limit_pages:
            print(f"    · --limit-pages {limit_pages} 에 도달해 멈춥니다")
            break
        if limit_reviews and len(collected) >= limit_reviews:
            print(f"    · --limit-reviews {limit_reviews} 에 도달해 멈춥니다")
            break

        polite_sleep()
        # 화면에 떠 있는 모든 리뷰가 기준입니다 (아는 것 포함) — 아는 것을
        # collected 에서 빼 두므로, collected 만으로 비교하면 매번 「변화 있음」이 됩니다.
        ids_before = {r["review_key"] for r in rows} | set(collected.keys())
        got = load_more_reviews(page, page_no, dom_count, ids_before, debug)
        if not got:
            print(f"    · {page_no}차에서 더 불러올 것이 없습니다 (끝)")
            complete = True
            break

        nxt, dom_count = got
        page_no += 1
        new, known = _take(nxt)
        rows = nxt
        tail = f" · 이미 아는 것 {known}건" if known else ""
        print(f"    · {page_no}차 · 새 리뷰 {new}건 (누적 {len(collected)}){tail}")

        if new == 0:
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_PAGES:
                print("    · 새 리뷰가 계속 없습니다. 끝으로 봅니다")
                complete = True
                break
        else:
            empty_streak = 0

    out = list(collected.values())

    # ── 제품 정보 · 옵션 일치 · 1단 배제 ──
    for r in out:
        r["brand"] = prod.get("brand")
        r["category"] = prod.get("category")
        r["nv_product_no"] = prod.get("nv_product_no")
        r["product_url"] = product_url
        r["option_target"] = target_option
        r["option_filtered"] = option_applied
        r["sorted_latest"] = sorted_latest
        r["pages_read"] = page_no
        r["complete"] = complete

        m, unsure = match_option(r.get("option_text"), target_option)
        r["option_match"] = m
        r["option_unsure"] = unsure

        excluded, reason = hard_filter_review(r.get("body"), r.get("written_at"))
        r["hard_excluded"] = excluded
        r["hard_reason"] = reason
        r["collected_at"] = datetime.now().isoformat(timespec="seconds")

    # ── 복붙 클러스터 (제품 단위) ──
    cluster_of = find_dup_clusters(out)
    for r in out:
        cid = cluster_of.get(r["review_key"])
        r["dup_cluster"] = cid
        if cid and not r["hard_excluded"]:
            r["hard_excluded"] = True
            r["hard_reason"] = "복붙"

    # ── 게시일 군집 (제품 단위) ──
    #   ★ 개별 리뷰를 배제하는 규칙이 아닙니다. 「이 제품의 리뷰 풀이 살포됐다」는
    #     신호라서, 리뷰마다 표시만 해 두고 판단은 라벨 단계에서 합니다.
    # ★ 게시일 군집은 「전체 리뷰의 분포」를 보는 규칙입니다.
    #   최신순으로 앞 60건만 가져오면 전부 최근이라 100% 군집으로 나옵니다.
    #   끝까지 읽지 않았으면 계산 자체를 하지 않습니다.
    if complete:
        clustered, detail = detect_date_cluster([r.get("written_at") for r in out])
    else:
        clustered, detail = (False, None)
    for r in out:
        r["date_cluster"] = detail if clustered else None

    report_health(out, clustered, detail, sorted_latest, page_no, complete,
                  target_option, option_applied)
    return out


def report_health(rows, date_clustered, date_detail,
                  sorted_latest=True, pages_read=1, complete=False,
                  target_option=None, option_applied=False):
    """수집 건강도 — 셀렉터가 깨졌는지 즉시 알 수 있게."""
    n = len(rows)
    if not n:
        return

    no_badge = sum(1 for r in rows if not r.get("badges_raw"))
    no_rating = sum(1 for r in rows if r.get("rating") is None)
    no_date = sum(1 for r in rows if not r.get("written_at"))
    no_author = sum(1 for r in rows if not r.get("author_hash"))
    no_option = sum(1 for r in rows if not r.get("option_text"))
    month = sum(1 for r in rows if r.get("is_month"))
    repeat = sum(1 for r in rows if r.get("is_repeat"))
    kept = sum(1 for r in rows if not r.get("hard_excluded"))

    reasons = {}
    for r in rows:
        if r.get("hard_excluded"):
            reasons[r.get("hard_reason") or "?"] = reasons.get(r.get("hard_reason") or "?", 0) + 1

    print(f"    ── 수집 {n}건 · 1단 통과 {kept}건 ({kept/n*100:.0f}%)")
    if reasons:
        print(f"       배제 사유: {reasons}")
    print(f"       한달사용 {month}건 · 재구매 {repeat}건")

    # 명세서 4장: 배지가 90% 이상 비면 셀렉터가 깨진 것
    if no_badge / n > BADGE_NULL_THRESHOLD:
        print(f"    ⚠️ 배지가 {no_badge}/{n}건 비어 있습니다 ({no_badge/n*100:.0f}%).")
        print("       셀렉터가 깨졌을 수 있습니다 — 수집을 멈추고 화면을 확인하세요.")
    if no_rating / n > 0.20:
        print(f"    ⚠️ 별점이 {no_rating}/{n}건 비어 있습니다 — 파서 의심")
    if no_date / n > 0.20:
        print(f"    ⚠️ 작성일이 {no_date}/{n}건 비어 있습니다 — 파서 의심")
    if no_author / n > 0.20:
        print(f"    ⚠️ 작성자가 {no_author}/{n}건 비어 있습니다 — 파서 의심")
    if no_option / n > 0.50:
        print(f"    ⚠️ 옵션 표기가 {no_option}/{n}건 비어 있습니다 — 옵션 한 개인 제품일 수 있습니다")

    # ★ 표본이 편향됐으면 아래 수치를 읽어선 안 됩니다.
    #   랭킹순은 사진 있고 길고 추천 많은 리뷰를 앞에 세웁니다. 그 20건으로
    #   「통과율」이나 「게시일 군집」을 판단하면 전부 과대평가됩니다.
    biased = (not sorted_latest) or not complete
    if biased:
        why = []
        if not sorted_latest:
            why.append("랭킹순 그대로")
        if not complete:
            why.append(f"{len(rows)}건만 읽고 멈춤")
        print(f"    ⚠️ 표본이 편향됐습니다 ({' · '.join(why)}).")
        print("       아래 통과율·게시일 군집은 참고만 하세요 — 전체 리뷰의 결론이 아닙니다.")

    ratio = kept / n
    if not biased:
        if ratio < 0.30:
            print(f"    ℹ️ 1단만으로 {ratio*100:.0f}%가 남았습니다. 2·3단을 더 거치면 더 줄어듭니다")
        if ratio > 0.60:
            print(f"    ⚠️ 1단 통과가 {ratio*100:.0f}%입니다 — 하드 배제가 느슨한지 확인하세요")

    if date_clustered:
        print(f"    ⚠️ 게시일 군집: {date_detail} — 체험단 살포 의심")
    elif not complete:
        print("       (게시일 군집은 전수 수집에서만 판정합니다 — 이번엔 건너뜀)")

    # 옵션이 섞였는지
    if target_option:
        mism = sum(1 for r in rows if r.get("option_match") is False)
        uns = sum(1 for r in rows if r.get("option_unsure"))
        if mism or uns:
            print(f"       옵션 불일치 {mism}건 · 애매 {uns}건 "
                  f"→ 라벨 계산에서 빠집니다 (필터 {'적용' if option_applied else '미적용'})")


# ══════════════════════════════════════════════════
# 입력 · 출력
# ══════════════════════════════════════════════════
FIELDS = [
    "brand", "category", "nv_product_no", "product_url",
    "option_target", "option_filtered", "sorted_latest", "pages_read", "complete",
    "review_key", "nv_review_id",
    "rating", "written_at", "author_hash", "has_photo",
    "is_month", "is_repeat", "badges_raw",
    "option_text", "option_match", "option_unsure",
    "hard_excluded", "hard_reason", "dup_cluster", "date_cluster",
    "body_clipped", "body",
    "collected_at",
]


def _read_xlsx_rows(path):
    """엑셀(.xlsx) 첫 시트를 [dict] 로 읽습니다. 첫 줄이 헤더입니다."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        print("❌ 엑셀을 읽으려면 openpyxl 이 필요합니다.")
        print("   pip install openpyxl")
        print("   (또는 엑셀에서 「다른 이름으로 저장 → CSV UTF-8」 로 바꿔 주세요)")
        sys.exit(1)

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = next(rows_iter)
    except StopIteration:
        return []
    # ★ 엑셀은 빈 칸을 None 으로 줍니다. 숫자 칸도 int/float 로 오므로 문자열로 맞춥니다.
    keys = [str(h).strip() if h is not None else "" for h in header]
    out = []
    for vals in rows_iter:
        if vals is None or all(v is None or str(v).strip() == "" for v in vals):
            continue
        row = {}
        for k, v in zip(keys, vals):
            if not k:
                continue
            if v is None:
                row[k] = ""
            elif isinstance(v, float) and v.is_integer():
                row[k] = str(int(v))       # 6545672838.0 → "6545672838"
            else:
                row[k] = str(v).strip()
        out.append(row)
    wb.close()
    print(f"📗 엑셀 「{ws.title}」 시트에서 {len(out)}행 읽음")
    return out


def _read_csv_rows(path):
    """CSV/TSV 를 [dict] 로. 탭·콤마 구분을 자동으로 가립니다."""
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            with open(path, encoding=enc, newline="") as f:
                head = f.readline()
                f.seek(0)
                delim = "\t" if head.count("\t") > head.count(",") else ","
                return [r for r in csv.DictReader(f, delimiter=delim)]
        except UnicodeDecodeError:
            continue
    print(f"❌ 인코딩을 알 수 없습니다: {path}")
    sys.exit(1)


# 엑셀·CSV 에서 꼭 필요한 칸
REQUIRED_COLS = ["product_url"]
USEFUL_COLS = ["brand", "category", "nv_option_name", "nv_product_no", "name", "nv_reviews"]


def read_products_file(path):
    """제품 목록을 읽습니다. .xlsx 도, CSV/TSV 도 됩니다.

    ★ collect_store.py 가 뽑은 products CSV 를 그대로 쓸 수 있습니다.
      엑셀로 직접 관리하시려면 아래 칸만 있으면 됩니다.

        product_url      (필수) 제품 상세 URL
        nv_option_name   대표 옵션. 비우면 옵션 필터 없이 전체 리뷰를 받습니다
        brand · category 결과 CSV 에 그대로 실립니다
        nv_product_no    어드민에서 제품을 찾는 열쇠. 비면 URL 에서 뽑습니다
        nv_reviews       있으면 예상 시간을 알려 줍니다
    """
    low = path.lower()
    rows = _read_xlsx_rows(path) if low.endswith((".xlsx", ".xlsm")) else _read_csv_rows(path)
    if low.endswith(".xls"):
        print("⚠️ 옛 .xls 형식은 못 읽습니다. .xlsx 로 저장해 주세요")
        sys.exit(1)

    out, dropped = [], 0
    seen_urls = set()
    for r in rows:
        r = {(k or "").strip(): ("" if v is None else str(v).strip())
             for k, v in r.items()}
        url = r.get("product_url") or ""
        if not url or r.get("brand") == "brand":   # 빈 줄 · 중간에 섞인 헤더
            dropped += 1
            continue
        if not url.startswith("http"):
            # ★ 서식의 한글 설명 줄(「제품 상세 URL」)까지 경고하면 시끄럽습니다.
            #   URL 로 적으려다 잘못된 것만 알려 줍니다.
            if re.search(r"[./]|naver", url):
                print(f"⚠️ URL 이 아닌 값을 건너뜁니다: {url[:40]}")
            dropped += 1
            continue
        if url in seen_urls:                       # 같은 제품이 두 번 적힌 경우
            dropped += 1
            continue
        seen_urls.add(url)

        # nv_product_no 가 없으면 URL 에서 뽑습니다 (products/<숫자>)
        if not r.get("nv_product_no"):
            m = re.search(r"products/(\d{6,})", url)
            if m:
                r["nv_product_no"] = m.group(1)
        out.append(r)

    if not out:
        print(f"❌ 쓸 수 있는 행이 없습니다. 「product_url」 칸이 있는지 확인하세요: {path}")
        sys.exit(1)

    have = set(out[0].keys())
    missing = [c for c in USEFUL_COLS if c not in have]
    if missing:
        print(f"ℹ️ 없는 칸: {missing} — 없어도 돌아가지만 있으면 결과가 풍부해집니다")
    no_opt = sum(1 for r in out if not r.get("nv_option_name"))
    if no_opt:
        print(f"ℹ️ 대표 옵션이 빈 제품 {no_opt}개 — 옵션 필터 없이 전체 리뷰를 받습니다")
    if dropped:
        print(f"ℹ️ 건너뛴 행 {dropped}개 (빈 줄·중복·URL 아님)")
    return out


# 예전 이름도 남겨 둡니다
read_products_csv = read_products_file


def _csv_row(r):
    row = dict(r)
    for k in ("written_at",):
        if row.get(k) is not None:
            row[k] = str(row[k])
    return row


def write_csv(rows, path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(_csv_row(r))


def append_csv(rows, path):
    """제품 하나가 끝날 때마다 이어 붙입니다.

    ★ 제품 20개를 50분 돌리다 15번째에서 막히면, 맨 끝에 한 번만 쓰는 구조에서는
      앞의 14개가 전부 날아갑니다. 그래서 제품마다 바로 씁니다.
      Ctrl+C 로 끊어도, 노트북이 잠들어도 거기까지는 남습니다.
    """
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow(_csv_row(r))


def read_done_products(path):
    """이어 하기 — 결과 CSV 에 이미 들어 있는 제품을 알려줍니다.

    반환: (완료된 nv_product_no 집합, 완료된 product_url 집합)
    """
    pnos, urls = set(), set()
    if not os.path.exists(path):
        return (pnos, urls)
    try:
        for r in _read_csv_rows(path):
            p = (r.get("nv_product_no") or "").strip()
            u = (r.get("product_url") or "").strip()
            if p and p != "nv_product_no":
                pnos.add(p)
            if u and u.startswith("http"):
                urls.add(u)
    except Exception as e:
        print(f"⚠️ 이어 하기 파일을 읽지 못했습니다: {str(e)[:60]}")
    return (pnos, urls)


# ══════════════════════════════════════════════════
# 파서 단독 검사 (브라우저로 사이트에 가지 않습니다)
# ══════════════════════════════════════════════════
def selftest(html_path, debug=True):
    """저장해 둔 리뷰 HTML 조각으로 파서만 검사합니다.

    ★ 셀렉터를 고칠 때마다 사이트에 다시 들어가지 않아도 되게 하는 장치입니다.
      네이버에 요청을 한 번도 보내지 않습니다.
    """
    if sync_playwright is None:
        print("❌ playwright 가 없어 selftest 를 돌릴 수 없습니다.")
        return 1

    with open(html_path, encoding="utf-8") as f:
        frag = f.read()

    full = (
        "<!doctype html><html><head><meta charset='utf-8'></head>"
        "<body>" + frag + "</body></html>"
    )
    tmp = os.path.abspath("_selftest_review.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(full)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(f"file://{tmp}", wait_until="domcontentloaded")
        rows = extract_reviews_from_page(page, debug=False)
        browser.close()

    os.remove(tmp)

    print(f"\n파서 검사 — {html_path}")
    print(f"리뷰 {len(rows)}건 인식\n")
    if not rows:
        print("❌ 하나도 못 읽었습니다.")
        return 1

    ok = True
    for r in rows:
        excluded, reason = hard_filter_review(r["body"])
        print(f"  review_key   {r['review_key']}")
        print(f"  rating       {r['rating']}")
        print(f"  written_at   {r['written_at']}  (원문 {r['written_raw']})")
        print(f"  author_hash  {r['author_hash']}")
        print(f"  badges_raw   {r['badges_raw']}")
        print(f"  is_month     {r['is_month']}")
        print(f"  is_repeat    {r['is_repeat']}")
        print(f"  option_text  {r['option_text']}")
        print(f"  has_photo    {r['has_photo']}")
        print(f"  1단          {'배제 (' + str(reason) + ')' if excluded else '통과'}")
        print(f"  body         {r['body']}")
        print()

        for field in ("rating", "written_at", "author_hash", "body"):
            if r.get(field) in (None, ""):
                print(f"  ❌ {field} 가 비었습니다")
                ok = False

    print("✅ 파서 정상" if ok else "❌ 비어 있는 값이 있습니다")
    return 0 if ok else 1


# ══════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="다들 · 브랜드스토어 리뷰 수집 (로컬 전용)")
    ap.add_argument("--product", help="제품 상세 URL 하나")
    ap.add_argument("--option", help="대표 옵션명 (--product 와 함께)")
    ap.add_argument("--brand", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--file", help="제품 목록 파일 — 엑셀(.xlsx) 또는 CSV/TSV. "
                                   "collect_store.py 가 뽑은 CSV 를 그대로 쓸 수 있습니다")
    ap.add_argument("--resume", action="store_true",
                    help="--out 에 이미 들어 있는 제품은 건너뜁니다 (중간에 끊겼을 때)")
    ap.add_argument("--limit-pages", type=int, default=MAX_PAGES_DEFAULT,
                    help="제품당 최대 로드 횟수. 무한 스크롤이라 1회 = 리뷰 20건 (0=끝까지)")
    ap.add_argument("--limit-reviews", type=int, default=DEFAULT_LIMIT_REVIEWS,
                    help=f"제품당 최대 리뷰 수 (기본 {DEFAULT_LIMIT_REVIEWS}). "
                         "0 을 주면 전수 수집 — 제품 등록 때 한 번만 권합니다")
    ap.add_argument("--seen", help="이미 수집한 CSV. review_key 를 읽어 새 리뷰만 받습니다 (증분)")
    ap.add_argument("--limit-products", type=int, default=0)
    ap.add_argument("--out", help="출력 CSV 경로")
    ap.add_argument("--headful", action="store_true", help="브라우저를 보면서")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--selftest", help="저장한 리뷰 HTML 조각으로 파서만 검사")
    ap.add_argument("--probe", action="store_true",
                    help="팝업의 버튼·링크를 훑어 보여 줍니다 (수집하지 않습니다)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest(args.selftest, args.debug))

    if sync_playwright is None:
        print("❌ playwright 가 설치돼 있지 않습니다.  pip install playwright")
        sys.exit(1)

    products = []
    if args.file:
        products = read_products_file(args.file)
        print(f"제품 {len(products)}개를 읽었습니다: {args.file}")
    elif args.product:
        # ★ 상품번호를 URL 에서 뽑습니다.
        #   이게 없으면 어드민이 리뷰를 붙일 제품을 못 찾아 전부 건너뜁니다.
        #   --file 경로에는 있었는데 여기만 빠져 있어, 1,000건이 통째로 버려졌습니다.
        m = re.search(r"products/(\d{6,})", args.product)
        products = [{
            "brand": args.brand, "category": args.category,
            "product_url": args.product, "nv_option_name": args.option or "",
            "nv_product_no": m.group(1) if m else "",
            "name": args.product.rsplit("/", 1)[-1],
        }]
        if not m:
            print("⚠️ URL 에서 상품번호를 못 뽑았습니다. 어드민에서 제품을 못 찾을 수 있습니다")
    else:
        ap.error("--product 또는 --file 중 하나가 필요합니다 (--selftest 도 가능)")

    if args.limit_products:
        products = products[:args.limit_products]

    seen_keys = read_seen_keys(args.seen) if args.seen else set()
    if seen_keys:
        print(f"증분 수집: 이미 아는 리뷰 {len(seen_keys):,}건 — 새 리뷰만 받습니다")

    out = args.out or f"reviews_{datetime.now():%Y%m%d_%H%M}.csv"

    # ── 이어 하기 ──
    if args.resume:
        done_p, done_u = read_done_products(out)
        if done_p or done_u:
            before = len(products)
            products = [r for r in products
                        if (r.get("nv_product_no") or "") not in done_p
                        and (r.get("product_url") or "") not in done_u]
            print(f"이어 하기: 이미 끝난 제품 {before - len(products)}개를 건너뜁니다 → {out}")
            if not products:
                print("✅ 목록의 제품이 모두 끝나 있습니다. 할 일이 없습니다.")
                sys.exit(0)
        else:
            print(f"이어 하기: {out} 에 끝난 제품이 없습니다. 처음부터 시작합니다")
    elif os.path.exists(out) and len(products) > 1:
        # 덮어쓰기 전에 알려 줍니다 — 50분 돌린 결과를 조용히 지우면 안 됩니다
        print(f"⚠️ {out} 가 이미 있습니다. 새로 씁니다 — 이어 하려면 --resume 을 붙이세요")
        os.remove(out)

    # 여러 제품이면 예상 시간을 먼저 말해 줍니다
    if len(products) > 1:
        per = (args.limit_reviews or 1000) / 20 * 3 / 60
        print(f"제품 {len(products)}개 · 제품당 최대 {args.limit_reviews or '전수'}건 "
              f"≈ 총 {per * len(products):.0f}분 예상")
        print("   제품이 끝날 때마다 바로 파일에 씁니다 — 중간에 끊겨도 앞의 것은 남습니다")

    all_count = 0
    done_rows_summary = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="ko-KR",
                                  viewport={"width": 1440, "height": 960})
        page = ctx.new_page()

        # ── 진단만 하고 끝냅니다 ──
        if args.probe:
            prod = products[0]
            print(f"\n진단: {prod.get('product_url')}")
            if open_review_popup(page, prod["product_url"], True):
                opt = (prod.get("nv_option_name") or "").strip()
                if opt:
                    apply_option_filter(page, opt, True)
                probe_popup(page)
                page.evaluate(_JS_SCROLL_POPUP)
                page.wait_for_timeout(2000)
                print("  ── 끝까지 스크롤한 뒤 ──")
                probe_popup(page)
            if args.headful:
                input("  창을 닫으려면 Enter...")
            browser.close()
            sys.exit(0)

        for i, prod in enumerate(products, 1):
            label = (prod.get("name") or prod.get("nv_product_no") or "?")[:30]
            print(f"\n[{i}/{len(products)}]", end="")
            try:
                rows = collect_product_reviews(page, prod, args.limit_pages, args.debug,
                                               args.limit_reviews, seen_keys)
            except KeyboardInterrupt:
                print(f"\n중단했습니다. {out} 에 여기까지 저장돼 있습니다.")
                break
            except Exception as e:
                print(f"    ⚠️ 제품 수집 실패: {str(e)[:100]}")
                done_rows_summary.append((label, 0, 0, "실패"))
                polite_sleep()
                continue

            if rows:
                # ★ 제품이 끝나는 즉시 씁니다. 다음 제품에서 막혀도 이건 남습니다.
                append_csv(rows, out)
                all_count += len(rows)
                kept = sum(1 for r in rows if not r.get("hard_excluded"))
                month = sum(1 for r in rows if r.get("is_month"))
                done_rows_summary.append((label, len(rows), kept, f"한달사용 {month}"))
                # 같은 실행 안에서 중복을 막습니다 (같은 리뷰가 두 제품에 걸릴 일은
                # 없지만, 목록에 같은 제품이 두 번 적혀 있을 때를 대비합니다)
                seen_keys.update(r["review_key"] for r in rows)
                print(f"    💾 {out} 에 {len(rows)}건 추가 (누적 {all_count})")
            else:
                done_rows_summary.append((label, 0, 0, "리뷰 없음"))
            polite_sleep()

        browser.close()

    if not all_count:
        print("\n❌ 저장할 리뷰가 없습니다.")
        sys.exit(1)

    print(f"\n🎉 리뷰 {all_count}건 → {out}")
    if len(done_rows_summary) > 1:
        print("   제품별:")
        for label, n, kept, note in done_rows_summary:
            if n:
                print(f"     {label:32} {n:5}건 · 1단 통과 {kept:5}건 · {note}")
            else:
                print(f"     {label:32} {'—':>5}   {note}")
    print("   어드민 「리뷰 CSV 가져오기」에 올리면 2·3단(AI)으로 넘어갑니다.")


if __name__ == "__main__":
    main()
