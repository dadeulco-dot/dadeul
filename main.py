import os
import json
import re
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from supabase import create_client, Client
from dotenv import load_dotenv

# SQLAdmin & SQLAlchemy 관련 임포트
from sqladmin import Admin, ModelView, expose
from sqlalchemy import create_engine, Column, String, BigInteger, Integer, Float, Boolean, Text, JSON, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import declarative_base
from markupsafe import Markup

# Anthropic Claude SDK 임포트
import anthropic

# ----------------------------------------------------
# 1. 환경 변수 및 DB 연결 설정
# ----------------------------------------------------
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
# YouTube Data API v3 — 댓글은 AI가 아니라 이 API로 가져옵니다 (CLAUDE.md)
#   search.list        100 units/호출  → 하루 100회가 실질 상한
#   commentThreads.list  1 unit/호출   → 댓글 100개
#   일일 무료 한도    10,000 units
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()

# ----------------------------------------------------
# 2. SQLAdmin용 SQLAlchemy ORM 모델 정의
# ----------------------------------------------------
class ProductCandidateAdminModel(Base):
    __tablename__ = "product_candidates"

    # ── 기본 정보 ──
    id = Column(String, primary_key=True, index=True)
    brand = Column(String, nullable=True)
    name = Column(String, nullable=True)
    category = Column(String, nullable=True)  # 갈래(sub)
    verdict = Column(String, default="keep")  # 후보 통과 여부: keep / drop (명세서상 "제품 라벨"이 아니라 "후보 채택 여부"임에 유의)
    reject_reason = Column(String, nullable=True)
    status = Column(String, default="PENDING_APPROVAL")
    stage = Column(String, default="stage1_only")  # stage1_only / stage2_verified

    # ── 가격 3종 (HANDOFF 6-3-1 ★ 섞으면 확인된 최저가가 오염됨) ──
    price_krw = Column(BigInteger, nullable=True)      # 판매가 — confirmed_low() 의 유일한 입력
    list_price = Column(BigInteger, nullable=True)     # 표시가 — 믿지 않음, 기록만
    member_price = Column(BigInteger, nullable=True)   # 조건부가 — 카드·멤버십·쿠폰
    price_inflated = Column(Boolean, default=False)    # 표시가가 판매가의 2배 초과 시 자동 플래그

    # ── 판매처 · 가격 감시 채널 (HANDOFF 6-3-2) ──
    # site_url = 브랜드 자사몰(기준가) · watch_naver = 시장 최저가 탐색 · watch_toss = 프로모션 감시(가격만)
    site_url = Column(String, nullable=True)
    watch_naver = Column(String, nullable=True)
    watch_toss = Column(String, nullable=True)
    watch_coupang = Column(String, nullable=True)

    # ── 브랜드 검증 (모기업 확인 등, 명세서 5-2) ──
    brand_scale = Column(String, nullable=True)
    brand_evidence = Column(Text, nullable=True)

    # ── 유튜브 근거 · 파이프라인 입력 (yt_must ★ 오염 방지 핵심) ──
    yt_review_count = Column(Integer, nullable=True)
    yt_evidence = Column(JSON, default=list)
    yt_queries = Column(JSON, default=list)
    yt_must = Column(JSON, default=list)
    aliases = Column(JSON, default=list)

    # ── 시장 신호 (참고용 · 후보 발굴 단계의 정량 데이터일 뿐, 다들 라벨이 아님) ──
    # ⚠ market_rating/market_reviews 자체가 "다들의 판단"이 되어서는 안 됨 (명세서 2-2).
    #    라벨(대체로 만족 등)은 여기서 산출하지 않고, 유튜브 댓글이 모인 뒤 label_of() 가 계산한다.
    market_rating = Column(Float, nullable=True)
    market_reviews = Column(Integer, nullable=True)
    market_orders = Column(Integer, nullable=True)
    market_url = Column(String, nullable=True)

    release = Column(String, nullable=True)  # "2024-03" 또는 null

    # ── 근거 · 불확실성 · 사람 확인 대상 ──
    sources = Column(JSON, default=list)
    uncertain = Column(JSON, default=list)
    check_by_human = Column(JSON, default=list)

    # ── B단계 수집값 (스토어에서 코드가 긁어온 값) ──
    # nv_product_no 가 제품 고유 키입니다. 같은 제품이 여러 스토어에 있어도 이 값으로 중복을 잡습니다.
    nv_product_no = Column(String, nullable=True, index=True)
    store_url = Column(String, nullable=True)      # 스토어 홈
    product_url = Column(String, nullable=True)    # 개별 제품 페이지 · 「상세 보기」가 향하는 곳
    nv_rating = Column(Float, nullable=True)       # 스토어 별점 (주 1회 갱신)
    nv_reviews = Column(Integer, nullable=True)    # 스토어 리뷰 수 (주 1회 갱신)
    # ★ URL만 저장합니다. 내려받아 우리 서버에 재호스팅하지 않습니다.
    #   URL 참조는 링크지만 내려받아 올리면 복제입니다 (명세서 B단계).
    thumb_url = Column(String, nullable=True)
    # 어느 경로로 수집했는지 (render / state_json) — 이상 데이터 추적용
    parse_method = Column(String, nullable=True)
    collected_at = Column(String, nullable=True)   # 수집 시점 · 화면에 「9.02 확인」처럼 병기

    # ── 원본 스냅샷 (감사용) ──
    raw_stage1 = Column(JSON, nullable=True)
    raw_stage2 = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BrandAdminModel(Base):
    """카테고리(갈래)별 브랜드 시드 목록.

    이 테이블의 목적은 「LLM이 매번 돈 주고 브랜드를 찾아내지 않게 하는 것」입니다.
    브랜드 구성은 자주 바뀌지 않으므로, 사람이 한 번 채워두고 계속 재사용합니다.

    tier 는 「인기순 상위 N개」로 자르는 게 아니라 명세서 후보 조건 ①
    (1인이 협상 가능한 국내 중소 브랜드)에 따라 나눕니다.
    다들의 타겟은 오히려 무명 중소 브랜드이므로, 인기 상위권을 남기는 방식은 취지에 어긋납니다.
    """
    __tablename__ = "brands"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)   # 브랜드명
    category = Column(String, nullable=True, index=True)  # 갈래 (예: 프라이팬)

    # candidate        : 국내 중소로 보임 — 실제 후보 발굴 대상
    # excluded_global  : 해외 브랜드 (르크루제, 휘슬러 등)
    # excluded_large   : 대기업·상장사·계열 (테팔=그룹세브, 락앤락 등)
    # excluded_pb      : 유통사 PB·오픈마켓 (노브랜드, 탐사, 쿠팡 등)
    # unknown          : 판단 보류 — 사람이 직접 확인해야 함
    tier = Column(String, default="unknown", index=True)
    tier_reason = Column(String, nullable=True)  # 왜 이 등급인지 (자동분류 근거 또는 사람 메모)

    is_active = Column(Boolean, default=True)  # 후보 발굴에 실제로 쓸지 여부
    note = Column(Text, nullable=True)

    # ── A단계 산출: 브랜드 공식 스마트스토어 ──
    # HANDOFF「스마트스토어 기준」(2026-09-03): 제품 수집은 스마트스토어 등록분만.
    # 판매자가 브랜드 본사여야 하며, 총판·리셀러 스토어는 협상 상대가 아니므로 official=False.
    store_url = Column(String, nullable=True)       # brand.naver.com/... 또는 smartstore.naver.com/...
    store_type = Column(String, nullable=True)      # "brand"(브랜드스토어) | "smartstore"(일반)
    official = Column(Boolean, nullable=True)       # 브랜드 본사 운영으로 확인됐는가
    official_evidence = Column(Text, nullable=True) # 배지·사업자명 일치·홈페이지 안내 등 판별 근거
    scale_evidence = Column(Text, nullable=True)    # 중소 브랜드 판단 근거 · 모기업 확인 결과
    store_sources = Column(JSON, default=list)      # 근거 URL
    store_uncertain = Column(JSON, default=list)    # 확신 낮은 항목
    store_checked_at = Column(DateTime(timezone=True), nullable=True)  # A단계를 마지막으로 돌린 시각

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ----------------------------------------------------
# 2-2. 유튜브 후기 수집 파이프라인
# ----------------------------------------------------
class YtVideoModel(Base):
    """A단계 · 영상 선별 결과.

    search.list 가 100 units/호출로 비쌉니다(일일 무료 10,000 units).
    한 번 판정한 영상은 저장해 두고 재사용합니다.
    """
    __tablename__ = "yt_videos"

    id = Column(String, primary_key=True, index=True)
    product_id = Column(String, nullable=True, index=True)
    video_id = Column(String, nullable=False, index=True)
    channel_id = Column(String, nullable=True)
    channel_title = Column(String, nullable=True)
    title = Column(Text, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    view_count = Column(BigInteger, nullable=True)
    found_by_query = Column(String, nullable=True)   # 어떤 검색어로 찾았는지

    # ── A단계 판정 (AI) ──
    verdict = Column(String, nullable=True)          # keep / drop
    route = Column(String, nullable=True)            # title / category / shorts
    tier = Column(Integer, nullable=True)            # 1 / 2 / 3
    reason = Column(String, nullable=True)
    sponsor_evidence = Column(Text, nullable=True)
    confidence = Column(String, nullable=True)       # high / low — low 는 사람이 확인
    human_checked = Column(Boolean, default=False)

    comments_disabled = Column(Boolean, default=False)
    comments_fetched_at = Column(DateTime(timezone=True), nullable=True)
    comment_count = Column(Integer, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class YtCommentModel(Base):
    """댓글 원문 — 코드가 API에서 받은 그대로.

    ★ AI가 되돌려준 텍스트를 여기 저장하지 마세요 (CLAUDE.md 2-3).
      조용히 다듬어 놓기 때문에 나중에 원문 대조가 불가능해집니다.
    ★ YouTube API 약관상 30일 주기 재검증이 필요합니다.
      원문은 지워도 yt_extractions 의 추출값은 남습니다.
    """
    __tablename__ = "yt_comments"

    id = Column(String, primary_key=True, index=True)
    video_id = Column(String, nullable=False, index=True)
    product_id = Column(String, nullable=True, index=True)
    comment_id = Column(String, nullable=False, index=True)
    text = Column(Text, nullable=True)               # 원문 · 30일 주기 재검증 대상
    author_hash = Column(String, nullable=True)      # 작성자는 해시로만
    like_count = Column(Integer, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)

    tier = Column(Integer, nullable=True)
    route = Column(String, nullable=True)

    collected_at = Column(DateTime(timezone=True), server_default=func.now())
    rechecked_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(Boolean, default=False)      # 재조회 시 사라졌으면 true
    text_purged_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class YtExtractionModel(Base):
    """B단계 추출값 — 영구 보관.

    ★ 정확도 85% 를 재는 대상이 이 테이블입니다.
    """
    __tablename__ = "yt_extractions"

    id = Column(String, primary_key=True, index=True)
    comment_id = Column(String, nullable=False, index=True)
    product_id = Column(String, nullable=True, index=True)

    # ── AI 추출값 ──
    months = Column(Float, nullable=True)            # 2주 = 0.5
    issues = Column(JSON, default=list)
    sentiment = Column(String, nullable=True)        # 긍정 / 부정 / 중립
    irony = Column(Boolean, default=False)           # 반어 · true 는 사람이 따로 봄
    product_match = Column(Boolean, nullable=True)   # title 이면 null
    exclude = Column(String, nullable=True)          # 협찬 / 사용 전 / 제품 무관 / 광고 스팸
    unsure = Column(Boolean, default=False)          # 판단이 안 서면 true. 찍지 않음

    # ── 코드 추출값 (규칙 기반) · C단계 이중 추출 대조 ──
    months_rule = Column(Float, nullable=True)
    conflict = Column(Boolean, default=False)        # 규칙과 AI가 어긋난 건

    # ── 코드 판정 (AI에게 시키면 숫자를 지어냅니다) ──
    similarity_dup = Column(Boolean, default=False)  # 4-gram 자카드 ≥ 0.45
    date_cluster = Column(Boolean, default=False)    # 출시 2주 내 게시일 군집

    tier = Column(Integer, nullable=True)
    route = Column(String, nullable=True)
    counted = Column(Boolean, default=True)          # 라벨 계산에 포함되는가

    extracted_at = Column(DateTime(timezone=True), server_default=func.now())
    model = Column(String, nullable=True)
    human_checked = Column(Boolean, default=False)
    human_note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


app = FastAPI(title="다들 (DADEUL) - Claude 웹 검색 기반 제품 발굴 파이프라인", version="7.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------
# 4. SQLAdmin 관리자 페이지 설정 (/admin)
# ----------------------------------------------------
if engine:
    # 💡 templates_dir 연결로 모바일 대응 custom_list.html 적용
    admin = Admin(app, engine, title="다들(DADEUL) 어드민", templates_dir="templates")

    def _fmt_link(url, label="🔗 이동"):
        return Markup(f'<a href="{url}" target="_blank" class="btn btn-sm btn-outline-primary">{label}</a>') if url else "-"

    def _fmt_won(v):
        return f"{v:,}원" if isinstance(v, (int, float)) and v is not None else "-"

    def _fmt_flag(v):
        return Markup('<span class="badge bg-danger">부풀림 의심</span>') if v else "-"

    class ProductCandidateAdminView(ModelView, model=ProductCandidateAdminModel):
        name = "후보 제품"
        name_plural = "후보 제품 목록"

        # B단계 결과(CSV) 붙여넣기 UI를 주입한 템플릿.
        # 예전 custom_list.html 의 「제품 가져오기」 버튼은 A~D단계 분리로 폐지됐습니다.
        list_template = "product_list.html"

        # ── 리스트: 후보를 빠르게 훑어보기 위한 최소 정보 ──
        column_list = [
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.price_krw,
            ProductCandidateAdminModel.list_price,
            ProductCandidateAdminModel.price_inflated,
            ProductCandidateAdminModel.nv_rating,
            ProductCandidateAdminModel.nv_reviews,
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.product_url,
        ]

        # ── 상세: 승인 전 사람이 확인해야 할 순서대로 ──
        column_details_list = [
            ProductCandidateAdminModel.id,
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.reject_reason,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.stage,
            # B단계 수집값
            ProductCandidateAdminModel.nv_product_no,
            ProductCandidateAdminModel.product_url,
            ProductCandidateAdminModel.store_url,
            ProductCandidateAdminModel.nv_rating,
            ProductCandidateAdminModel.nv_reviews,
            ProductCandidateAdminModel.thumb_url,
            ProductCandidateAdminModel.parse_method,
            ProductCandidateAdminModel.collected_at,
            # 가격 3종
            ProductCandidateAdminModel.price_krw,
            ProductCandidateAdminModel.list_price,
            ProductCandidateAdminModel.member_price,
            ProductCandidateAdminModel.price_inflated,
            # 판매처 · 감시 채널
            ProductCandidateAdminModel.site_url,
            ProductCandidateAdminModel.watch_naver,
            ProductCandidateAdminModel.watch_toss,
            ProductCandidateAdminModel.watch_coupang,
            # 브랜드 검증
            ProductCandidateAdminModel.brand_scale,
            ProductCandidateAdminModel.brand_evidence,
            # 유튜브 · 파이프라인 입력
            ProductCandidateAdminModel.yt_review_count,
            ProductCandidateAdminModel.yt_evidence,
            ProductCandidateAdminModel.yt_queries,
            ProductCandidateAdminModel.yt_must,
            ProductCandidateAdminModel.aliases,
            # 시장 신호 (참고용)
            ProductCandidateAdminModel.market_rating,
            ProductCandidateAdminModel.market_reviews,
            ProductCandidateAdminModel.market_orders,
            ProductCandidateAdminModel.market_url,
            ProductCandidateAdminModel.release,
            # 근거 · 불확실성
            ProductCandidateAdminModel.sources,
            ProductCandidateAdminModel.uncertain,
            ProductCandidateAdminModel.check_by_human,
            # 원본 스냅샷
            ProductCandidateAdminModel.raw_stage1,
            ProductCandidateAdminModel.raw_stage2,
            ProductCandidateAdminModel.created_at,
            ProductCandidateAdminModel.updated_at,
        ]

        # ── 편집 폼: 승인 전 사람이 직접 고칠 수 있는 항목 전부 ──
        form_columns = [
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.reject_reason,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.price_krw,
            ProductCandidateAdminModel.list_price,
            ProductCandidateAdminModel.member_price,
            ProductCandidateAdminModel.site_url,
            ProductCandidateAdminModel.watch_naver,
            ProductCandidateAdminModel.watch_toss,
            ProductCandidateAdminModel.watch_coupang,
            ProductCandidateAdminModel.brand_scale,
            ProductCandidateAdminModel.brand_evidence,
            ProductCandidateAdminModel.yt_review_count,
            ProductCandidateAdminModel.yt_evidence,
            ProductCandidateAdminModel.yt_queries,
            ProductCandidateAdminModel.yt_must,
            ProductCandidateAdminModel.aliases,
            ProductCandidateAdminModel.market_rating,
            ProductCandidateAdminModel.market_reviews,
            ProductCandidateAdminModel.market_orders,
            ProductCandidateAdminModel.market_url,
            ProductCandidateAdminModel.release,
            ProductCandidateAdminModel.sources,
            ProductCandidateAdminModel.uncertain,
            ProductCandidateAdminModel.check_by_human,
        ]

        column_searchable_list = ["brand", "name"]

        column_labels = {
            ProductCandidateAdminModel.brand: "브랜드",
            ProductCandidateAdminModel.name: "제품명",
            ProductCandidateAdminModel.category: "갈래",
            ProductCandidateAdminModel.verdict: "후보 채택 여부",
            ProductCandidateAdminModel.reject_reason: "판정 사유",
            ProductCandidateAdminModel.status: "운영 상태",
            ProductCandidateAdminModel.stage: "파이프라인 단계",
            ProductCandidateAdminModel.nv_product_no: "네이버 상품번호",
            ProductCandidateAdminModel.product_url: "제품 페이지",
            ProductCandidateAdminModel.store_url: "스토어",
            ProductCandidateAdminModel.nv_rating: "스토어 별점",
            ProductCandidateAdminModel.nv_reviews: "스토어 리뷰 수",
            ProductCandidateAdminModel.thumb_url: "썸네일 URL",
            ProductCandidateAdminModel.parse_method: "수집 경로",
            ProductCandidateAdminModel.collected_at: "수집 시점",
            ProductCandidateAdminModel.price_krw: "판매가",
            ProductCandidateAdminModel.list_price: "표시가",
            ProductCandidateAdminModel.member_price: "조건부가",
            ProductCandidateAdminModel.price_inflated: "정가 부풀림 의심",
            ProductCandidateAdminModel.site_url: "자사몰(기준가)",
            ProductCandidateAdminModel.watch_naver: "네이버 쇼핑",
            ProductCandidateAdminModel.watch_toss: "토스 쇼핑",
            ProductCandidateAdminModel.watch_coupang: "기타 제휴 채널",
            ProductCandidateAdminModel.brand_scale: "브랜드 규모",
            ProductCandidateAdminModel.brand_evidence: "브랜드 판단 근거",
            ProductCandidateAdminModel.yt_review_count: "유튜브 리뷰 수",
            ProductCandidateAdminModel.yt_evidence: "유튜브 근거 URL",
            ProductCandidateAdminModel.yt_queries: "유튜브 검색어",
            ProductCandidateAdminModel.yt_must: "제목 필수 단어",
            ProductCandidateAdminModel.aliases: "검색 별칭",
            ProductCandidateAdminModel.market_rating: "판매처 별점(참고용)",
            ProductCandidateAdminModel.market_reviews: "누적 리뷰 수",
            ProductCandidateAdminModel.market_orders: "구매 건수",
            ProductCandidateAdminModel.market_url: "수치 확인 주소",
            ProductCandidateAdminModel.release: "출시 시점",
            ProductCandidateAdminModel.sources: "근거 URL",
            ProductCandidateAdminModel.uncertain: "확신 낮은 항목",
            ProductCandidateAdminModel.check_by_human: "사람이 볼 항목",
            ProductCandidateAdminModel.raw_stage1: "1단계 원본",
            ProductCandidateAdminModel.raw_stage2: "2단계 원본",
            ProductCandidateAdminModel.created_at: "생성일",
            ProductCandidateAdminModel.updated_at: "수정일",
        }

        column_formatters = {
            ProductCandidateAdminModel.site_url: lambda m, a: _fmt_link(m.site_url, "🔗 자사몰"),
            ProductCandidateAdminModel.product_url: lambda m, a: _fmt_link(m.product_url, "🛒 제품"),
            ProductCandidateAdminModel.price_krw: lambda m, a: _fmt_won(m.price_krw),
            ProductCandidateAdminModel.list_price: lambda m, a: _fmt_won(m.list_price),
            ProductCandidateAdminModel.price_inflated: lambda m, a: _fmt_flag(m.price_inflated),
        }

        column_formatters_detail = {
            ProductCandidateAdminModel.site_url: lambda m, a: _fmt_link(m.site_url, "🔗 자사몰"),
            ProductCandidateAdminModel.product_url: lambda m, a: _fmt_link(m.product_url, "🛒 제품 페이지"),
            ProductCandidateAdminModel.store_url: lambda m, a: _fmt_link(m.store_url, "🏪 스토어"),
            ProductCandidateAdminModel.watch_naver: lambda m, a: _fmt_link(m.watch_naver, "🔗 네이버"),
            ProductCandidateAdminModel.watch_toss: lambda m, a: _fmt_link(m.watch_toss, "🔗 토스"),
            ProductCandidateAdminModel.watch_coupang: lambda m, a: _fmt_link(m.watch_coupang, "🔗 제휴"),
            ProductCandidateAdminModel.market_url: lambda m, a: _fmt_link(m.market_url, "🔗 수치 확인"),
            ProductCandidateAdminModel.price_krw: lambda m, a: _fmt_won(m.price_krw),
            ProductCandidateAdminModel.list_price: lambda m, a: _fmt_won(m.list_price),
            ProductCandidateAdminModel.member_price: lambda m, a: _fmt_won(m.member_price),
            ProductCandidateAdminModel.price_inflated: lambda m, a: _fmt_flag(m.price_inflated),
            ProductCandidateAdminModel.thumb_url: lambda m, a: (
                Markup(f'<img src="{m.thumb_url}" style="max-width:160px;border-radius:6px">')
                if m.thumb_url else "-"
            ),
        }

        can_view_details = True
        can_edit = True
        can_delete = True
        can_create = True

        @expose("/fetch-products", methods=["POST"])
        async def fetch_products_action(self, request: Request):
            # ⚠️ 예전 「제품 가져오기」는 AI에게 제품·별점·가격까지 한꺼번에 물어보는 방식이었고,
            #    새 명세서(2026-09-03)에서 A~D 4단계로 분리되면서 폐지됐습니다.
            #    지금은 A단계(브랜드 공식 스토어 찾기)만 구현돼 있고, 브랜드 화면에서 실행합니다.
            #    B단계(스토어 크롤링)가 만들어지면 여기에 다시 연결합니다.
            print("⚠️ [폐지된 경로] 제품 가져오기는 A~D단계로 분리됐습니다. 브랜드 화면에서 A단계를 먼저 실행하세요.")
            return RedirectResponse(url="/admin/brand-admin-model/list", status_code=303)

        @expose("/import-products", methods=["POST"])
        async def import_products_action(self, request: Request):
            """B단계 결과(collect_store.py 가 만든 CSV)를 붙여넣어 저장합니다.

            서버가 직접 크롤링하지 않습니다. 수집은 로컬에서 사람이 돌리고,
            그 결과만 여기로 들어옵니다.
            """
            import csv as _csv
            import io as _io

            form_data = await request.form()
            category = (form_data.get("category") or "").strip()
            brand = (form_data.get("brand") or "").strip()

            # 파일 업로드를 우선하고, 없으면 붙여넣기 텍스트를 씁니다
            raw_csv = ""
            upload = form_data.get("csv_file")
            if upload is not None and hasattr(upload, "read"):
                content = await upload.read()
                if content:
                    # 엑셀이 저장한 한글 CSV는 보통 utf-8-sig 또는 cp949 입니다
                    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
                        try:
                            raw_csv = content.decode(enc)
                            print(f"📄 업로드 파일 인코딩: {enc}")
                            break
                        except UnicodeDecodeError:
                            continue
            if not raw_csv:
                raw_csv = form_data.get("csv_text") or ""

            if not raw_csv.strip() or not supabase:
                return RedirectResponse(url="/admin/product-candidate-admin-model/list", status_code=303)

            text = raw_csv.strip()

            # 💡 엑셀에서 복사해 붙여넣으면 쉼표가 아니라 탭으로 구분됩니다.
            #    첫 줄을 보고 구분자를 자동으로 판단합니다.
            first_line = text.split("\n", 1)[0]
            delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","

            reader = _csv.DictReader(_io.StringIO(text), delimiter=delimiter)
            rows = list(reader)
            print(f"\n📥 [제품 CSV 가져오기] 갈래='{category}' 브랜드='{brand}' · "
                  f"구분자={'탭' if delimiter == chr(9) else '쉼표'} · {len(rows)}행 파싱됨")

            # nv_product_no 로 중복을 잡습니다 (명세서 B단계)
            existing = set()
            try:
                res = supabase.table("product_candidates").select("nv_product_no").execute()
                for it in (res.data or []):
                    if it.get("nv_product_no"):
                        existing.add(str(it["nv_product_no"]))
            except Exception as e:
                print(f"⚠️ 기존 제품 조회 실패: {e}")

            payloads = []
            skipped = 0
            for r in rows:
                pno = (r.get("nv_product_no") or "").strip()
                # 같은 CSV를 두 번 붙여넣으면 헤더 줄이 데이터로 섞여 들어옵니다
                if pno == "nv_product_no":
                    skipped += 1
                    continue
                if not pno or pno in existing:
                    skipped += 1
                    continue
                existing.add(pno)

                # ★ CSV에 brand/category 가 있으면 행마다의 값을 씁니다.
                #   여러 브랜드를 한 파일로 올릴 수 있게 하기 위함입니다.
                #   비어 있으면 폼에 입력한 값으로 채웁니다.
                row_brand = (r.get("brand") or "").strip() or brand or None
                row_category = (r.get("category") or "").strip() or category or None

                price_krw = safe_int(r.get("price_krw"))
                list_price = safe_int(r.get("list_price"))
                payloads.append({
                    "nv_product_no": pno,
                    "brand": row_brand,
                    "name": (r.get("name") or "").strip() or None,
                    "category": row_category,
                    "store_url": (r.get("store_url") or "").strip() or None,
                    "product_url": (r.get("product_url") or "").strip() or None,
                    "price_krw": price_krw,
                    "list_price": list_price,
                    "member_price": safe_int(r.get("member_price")),
                    "price_inflated": bool(list_price and price_krw and list_price > 2 * price_krw),
                    "nv_rating": safe_float(r.get("nv_rating")),
                    "nv_reviews": safe_int(r.get("nv_reviews")),
                    "thumb_url": (r.get("thumb_url") or "").strip() or None,
                    "parse_method": (r.get("parse_method") or "").strip() or None,
                    "collected_at": (r.get("collected_at") or "").strip() or None,
                    "stage": "stage_b_collected",
                    "status": "PENDING_APPROVAL",
                })

            saved = 0
            if payloads:
                try:
                    res = supabase.table("product_candidates").insert(payloads).execute()
                    saved = len(res.data or [])
                except Exception as e:
                    print(f"❌ 제품 저장 실패: {e}")

            by_brand = {}
            for pl in payloads:
                b = pl.get("brand") or "(미지정)"
                by_brand[b] = by_brand.get(b, 0) + 1
            print(f"✅ 저장 {saved}개 / 중복·빈값 건너뜀 {skipped}개")
            if by_brand:
                print(f"   브랜드별: {by_brand}")
            return RedirectResponse(url="/admin/product-candidate-admin-model/list", status_code=303)

    admin.add_view(ProductCandidateAdminView)

    class BrandAdminView(ModelView, model=BrandAdminModel):
        name = "브랜드"
        name_plural = "브랜드 목록"

        # 붙여넣기 입력 UI를 주입한 템플릿 (templates/brand_list.html)
        list_template = "brand_list.html"

        column_list = [
            BrandAdminModel.name,
            BrandAdminModel.category,
            BrandAdminModel.tier,
            BrandAdminModel.store_url,
            BrandAdminModel.store_type,
            BrandAdminModel.official,
            BrandAdminModel.is_active,
        ]

        column_details_list = [
            BrandAdminModel.id,
            BrandAdminModel.name,
            BrandAdminModel.category,
            BrandAdminModel.tier,
            BrandAdminModel.tier_reason,
            BrandAdminModel.is_active,
            BrandAdminModel.note,
            # A단계 산출
            BrandAdminModel.store_url,
            BrandAdminModel.store_type,
            BrandAdminModel.official,
            BrandAdminModel.official_evidence,
            BrandAdminModel.scale_evidence,
            BrandAdminModel.store_sources,
            BrandAdminModel.store_uncertain,
            BrandAdminModel.store_checked_at,
            BrandAdminModel.created_at,
            BrandAdminModel.updated_at,
        ]

        form_columns = [
            BrandAdminModel.name,
            BrandAdminModel.category,
            BrandAdminModel.tier,
            BrandAdminModel.tier_reason,
            BrandAdminModel.is_active,
            BrandAdminModel.note,
            BrandAdminModel.store_url,
            BrandAdminModel.store_type,
            BrandAdminModel.official,
            BrandAdminModel.official_evidence,
            BrandAdminModel.scale_evidence,
        ]

        column_searchable_list = ["name", "category"]
        column_sortable_list = ["name", "category", "tier"]

        column_labels = {
            BrandAdminModel.name: "브랜드명",
            BrandAdminModel.category: "갈래",
            BrandAdminModel.tier: "등급",
            BrandAdminModel.tier_reason: "등급 근거",
            BrandAdminModel.is_active: "사용",
            BrandAdminModel.note: "메모",
            BrandAdminModel.store_url: "공식 스토어",
            BrandAdminModel.store_type: "스토어 유형",
            BrandAdminModel.official: "공식 확인",
            BrandAdminModel.official_evidence: "공식 판별 근거",
            BrandAdminModel.scale_evidence: "규모 판단 근거",
            BrandAdminModel.store_sources: "근거 URL",
            BrandAdminModel.store_uncertain: "확신 낮은 항목",
            BrandAdminModel.store_checked_at: "스토어 확인 시각",
            BrandAdminModel.created_at: "생성일",
            BrandAdminModel.updated_at: "수정일",
        }

        column_formatters = {
            BrandAdminModel.store_url: lambda m, a: _fmt_link(m.store_url, "🏪 스토어"),
        }
        column_formatters_detail = {
            BrandAdminModel.store_url: lambda m, a: _fmt_link(m.store_url, "🏪 스토어"),
        }

        can_view_details = True
        can_edit = True
        can_delete = True
        can_create = True

        @expose("/import-brands", methods=["POST"])
        async def import_brands_action(self, request: Request):
            """붙여넣은 브랜드 목록을 파싱해 저장합니다. (크롤링이 아니라 사람이 1회 입력)"""
            form_data = await request.form()
            category = (form_data.get("category") or "").strip()
            raw_text = form_data.get("brand_text") or ""

            names = parse_brand_input(raw_text)
            print(f"\n📥 [브랜드 가져오기] 갈래='{category}', 입력 {len(names)}개 파싱됨")

            if not supabase or not names:
                return RedirectResponse(url="/admin/brand-admin-model/list", status_code=303)

            # 같은 갈래에 이미 있는 브랜드는 건너뜁니다 (등급을 사람이 고쳐놨을 수 있으므로 덮어쓰지 않음)
            existing = set()
            try:
                res = supabase.table("brands").select("name, category").eq("category", category).execute()
                for it in (res.data or []):
                    existing.add(str(it.get("name") or "").strip().lower())
            except Exception as e:
                print(f"⚠️ 기존 브랜드 조회 실패: {e}")

            rows = []
            for nm in names:
                if nm.strip().lower() in existing:
                    continue
                tier, reason = classify_brand(nm)
                rows.append({
                    "name": nm,
                    "category": category,
                    "tier": tier,
                    "tier_reason": reason,
                    "is_active": True,
                })

            saved = 0
            if rows:
                try:
                    r = supabase.table("brands").insert(rows).execute()
                    saved = len(r.data or [])
                except Exception as e:
                    print(f"❌ 브랜드 저장 실패: {e}")

            skipped = len(names) - len(rows)
            counts = {}
            for row in rows:
                counts[row["tier"]] = counts.get(row["tier"], 0) + 1
            print(f"✅ 저장 {saved}개 / 중복 건너뜀 {skipped}개 / 등급별 {counts}")

            return RedirectResponse(url="/admin/brand-admin-model/list", status_code=303)

        @expose("/find-stores", methods=["POST"])
        async def find_stores_action(self, request: Request):
            """A단계 실행: 이 갈래 브랜드들의 공식 스마트스토어 주소를 찾습니다."""
            form_data = await request.form()
            category = (form_data.get("category") or "").strip()
            if not category:
                return RedirectResponse(url="/admin/brand-admin-model/list", status_code=303)
            await run_stage_a(category=category, auto_save_db=True)
            return RedirectResponse(url="/admin/brand-admin-model/list", status_code=303)

    admin.add_view(BrandAdminView)

    # ── 유튜브 후기 파이프라인 ──
    class YtVideoAdminView(ModelView, model=YtVideoModel):
        name = "유튜브 영상"
        name_plural = "유튜브 영상 (A단계)"

        column_list = [
            YtVideoModel.title,
            YtVideoModel.channel_title,
            YtVideoModel.verdict,
            YtVideoModel.tier,
            YtVideoModel.reason,
            YtVideoModel.confidence,
            YtVideoModel.comment_count,
            YtVideoModel.published_at,
        ]
        column_details_exclude_list = []
        form_columns = [
            YtVideoModel.verdict,
            YtVideoModel.route,
            YtVideoModel.tier,
            YtVideoModel.reason,
            YtVideoModel.sponsor_evidence,
            YtVideoModel.confidence,
            YtVideoModel.human_checked,
        ]
        column_searchable_list = ["title", "channel_title", "video_id"]
        column_sortable_list = ["published_at", "tier", "comment_count"]
        column_labels = {
            YtVideoModel.video_id: "영상 ID",
            YtVideoModel.title: "제목",
            YtVideoModel.channel_title: "채널",
            YtVideoModel.published_at: "게시일",
            YtVideoModel.view_count: "조회수",
            YtVideoModel.found_by_query: "찾은 검색어",
            YtVideoModel.verdict: "판정",
            YtVideoModel.route: "경로",
            YtVideoModel.tier: "단계",
            YtVideoModel.reason: "사유",
            YtVideoModel.sponsor_evidence: "협찬 근거",
            YtVideoModel.confidence: "확신",
            YtVideoModel.human_checked: "사람 확인",
            YtVideoModel.comments_disabled: "댓글 꺼짐",
            YtVideoModel.comment_count: "댓글 수",
        }
        column_formatters = {
            YtVideoModel.title: lambda m, a: Markup(
                f'<a href="https://www.youtube.com/watch?v={m.video_id}" target="_blank">{(m.title or "")[:60]}</a>'
            ) if m.video_id else (m.title or "-"),
        }
        can_view_details = True
        can_create = False

    admin.add_view(YtVideoAdminView)

    class YtCommentAdminView(ModelView, model=YtCommentModel):
        name = "유튜브 댓글"
        name_plural = "유튜브 댓글 (원문)"

        column_list = [
            YtCommentModel.text,
            YtCommentModel.video_id,
            YtCommentModel.tier,
            YtCommentModel.like_count,
            YtCommentModel.is_deleted,
            YtCommentModel.collected_at,
        ]
        column_searchable_list = ["text", "video_id", "comment_id"]
        column_sortable_list = ["collected_at", "like_count"]
        column_labels = {
            YtCommentModel.text: "원문",
            YtCommentModel.video_id: "영상 ID",
            YtCommentModel.comment_id: "댓글 ID",
            YtCommentModel.author_hash: "작성자(해시)",
            YtCommentModel.like_count: "좋아요",
            YtCommentModel.published_at: "작성일",
            YtCommentModel.tier: "단계",
            YtCommentModel.route: "경로",
            YtCommentModel.collected_at: "수집 시각",
            YtCommentModel.rechecked_at: "재조회 시각",
            YtCommentModel.is_deleted: "삭제됨",
            YtCommentModel.text_purged_at: "원문 삭제 시각",
        }
        can_view_details = True
        can_create = False
        can_edit = False   # 원문은 손대지 않습니다

    admin.add_view(YtCommentAdminView)

    class YtExtractionAdminView(ModelView, model=YtExtractionModel):
        name = "댓글 추출값"
        name_plural = "댓글 추출값 (B단계)"

        column_list = [
            YtExtractionModel.comment_id,
            YtExtractionModel.months,
            YtExtractionModel.issues,
            YtExtractionModel.sentiment,
            YtExtractionModel.exclude,
            YtExtractionModel.unsure,
            YtExtractionModel.irony,
            YtExtractionModel.conflict,
            YtExtractionModel.tier,
            YtExtractionModel.counted,
        ]
        form_columns = [
            YtExtractionModel.months,
            YtExtractionModel.issues,
            YtExtractionModel.sentiment,
            YtExtractionModel.irony,
            YtExtractionModel.product_match,
            YtExtractionModel.exclude,
            YtExtractionModel.unsure,
            YtExtractionModel.counted,
            YtExtractionModel.human_checked,
            YtExtractionModel.human_note,
        ]
        column_sortable_list = ["extracted_at", "months", "tier"]
        column_labels = {
            YtExtractionModel.comment_id: "댓글 ID",
            YtExtractionModel.months: "사용 기간(개월)",
            YtExtractionModel.issues: "문제유형",
            YtExtractionModel.sentiment: "감성",
            YtExtractionModel.irony: "반어",
            YtExtractionModel.product_match: "제품 일치",
            YtExtractionModel.exclude: "제외 사유",
            YtExtractionModel.unsure: "판단 보류",
            YtExtractionModel.months_rule: "기간(규칙)",
            YtExtractionModel.conflict: "규칙·AI 불일치",
            YtExtractionModel.similarity_dup: "유사 중복",
            YtExtractionModel.date_cluster: "게시일 군집",
            YtExtractionModel.tier: "단계",
            YtExtractionModel.route: "경로",
            YtExtractionModel.counted: "라벨 계산 포함",
            YtExtractionModel.extracted_at: "추출 시각",
            YtExtractionModel.model: "모델",
            YtExtractionModel.human_checked: "사람 확인",
            YtExtractionModel.human_note: "확인 메모",
        }
        can_view_details = True
        can_create = False

    admin.add_view(YtExtractionAdminView)

# ----------------------------------------------------
# 5. 사용자 앱 (index.html) 서빙
#    ※ 핸드오버 패키지의 prototype/index.html 과 이름을 맞췄습니다.
#      templates/ 의 어드민 템플릿과 헷갈리지 않도록 static/ 아래에 둡니다.
# ----------------------------------------------------
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_user_app():
    # index.html 이 기준입니다. 예전 이름(main.html)은 이전 기간 동안만 봐 줍니다.
    for path in ("static/index.html", "static/main.html"):
        if os.path.exists(path):
            return FileResponse(path)
    return {"message": "static/index.html 파일을 찾을 수 없습니다. 폴더 구조를 확인하세요."}

# ----------------------------------------------------
# 5-2. 브랜드 시드 분류 로직
# ----------------------------------------------------
# ⚠️ 아래 목록은 「확실히 아는 것만」 담습니다. 애매하면 unknown으로 남겨서 사람이 봅니다.
#    자동 분류가 틀릴 수 있으므로 어드민에서 언제든 등급을 고칠 수 있게 해두었습니다.

# 해외 브랜드 (수입·글로벌)
BRANDS_GLOBAL = {
    "르크루제", "스타우브", "휘슬러", "헹켈", "롯지", "WMF", "이딸라", "조셉조셉",
    "코렐", "코닝웨어", "파이렉스", "이케아", "MUJI", "샤오미", "비타크래프트",
    "비타그래프트", "실리트", "드부이에", "드메이어", "발라리니", "버미큘라",
    "스캔팬", "스칸팬 코리아", "스켑슐트", "브라반티아", "OXO", "피스카스",
    "타파웨어", "트라몬티나", "베르그호프", "버그호프", "구찌니", "포트메리온",
    "로얄코펜하겐", "니토리", "프랑프랑", "웨버", "페트로막스", "삼보넷",
    "스위스다이아몬드", "쿠진아트", "리버라이트", "타이거크라운", "이와츄",
    "파켈만", "페드리니", "에바솔로", "라바제", "INVICTA", "ELO", "AMT",
    "WOLL", "TVS", "SKATER", "스케이터코리아", "아놀론", "마이어", "그린팬",
    "헥스클래드", "파사바체", "베네통", "메종오브제", "라씨에뜨", "소리야나기",
    "요시가와", "스기야마", "나가타니", "아케보노", "호쿠리쿠", "후지호로",
    "하코야", "카모메키친", "키와메", "타케하라", "히로유키", "론네바이브룩",
}

# 대기업·상장사·계열 (1인 협상이 현실적으로 어려움)
BRANDS_LARGE = {
    "테팔",          # 프랑스 그룹세브 — 명세서에 명시된 제외 사례
    "락앤락", "쿠쿠", "롯데", "롯데알미늄", "롯데이라이프", "한샘",
    "신세계인터내셔날", "모던하우스", "자주(JAJU)", "JAJU", "글라스락",
    "삼양가전", "현대물산", "현대산업", "3M", "써모스", "한국도자기",
    "한국도자기리빙", "자이글", "쿠첸프로피", "애터미",
}

# 유통사 PB · 오픈마켓 · 브랜드 실체 불분명
BRANDS_PB = {
    "노브랜드", "탐사", "쿠팡", "오늘좋은", "오너클랜", "젊은이마켓",
    "기타", "홈플러스", "이마트", "에이치플러스몰",
}


def classify_brand(name: str):
    """브랜드명을 보고 1차 등급을 매깁니다. 확실하지 않으면 unknown으로 둡니다."""
    n = (name or "").strip()
    if not n:
        return ("unknown", None)
    if n in BRANDS_GLOBAL:
        return ("excluded_global", "해외 브랜드")
    if n in BRANDS_LARGE:
        return ("excluded_large", "대기업·상장사·계열")
    if n in BRANDS_PB:
        return ("excluded_pb", "유통사 PB·오픈마켓")
    return ("unknown", None)


def parse_brand_input(raw_text: str):
    """어드민에서 붙여넣은 브랜드 목록을 파싱합니다.

    다나와 필터 목록을 그대로 복사하면 '- [ ] 브랜드명' 같은 형태로 붙는 경우가 많아,
    체크박스 기호·불릿·번호를 벗겨냅니다. 줄바꿈/쉼표 둘 다 구분자로 받습니다.
    """
    if not raw_text:
        return []

    # 쉼표로 붙여넣은 경우도 줄바꿈으로 통일
    text = raw_text.replace(",", "\n")
    names = []
    seen = set()

    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        # '- [ ] ', '- [x] ', '* ', '- ', '1. ' 같은 접두 기호 제거
        s = re.sub(r'^[-*•]\s*', '', s)
        s = re.sub(r'^\[[ xX]?\]\s*', '', s)
        s = re.sub(r'^\d+[\.\)]\s*', '', s)
        s = s.strip()
        if not s:
            continue
        # 중복 제거 (대소문자·공백 무시)
        key = s.replace(" ", "").lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(s)

    return names

# ----------------------------------------------------
# 6. 파이프라인 로직 및 중복 제거 (Claude web_search 툴)
# ----------------------------------------------------
def clean_json_response(raw_text: str):
    """불순물이 섞여도 JSON 배열만 정확히 추출하는 방어 코드"""
    if not raw_text:
        return []
    start_idx = raw_text.find('[')
    end_idx = raw_text.rfind(']')
    if start_idx != -1 and end_idx != -1:
        clean_text = raw_text[start_idx:end_idx+1]
        try:
            return json.loads(clean_text)
        except Exception as e:
            print(f"⚠️ JSON 변환 오류: {str(e)}")
    print(f"⚠️ [JSON 파싱 실패] 원본 텍스트: {raw_text}")
    return []

def safe_int(val):
    """숫자 외의 문자(콤마 등)가 섞여도 안전하게 정수로 변환"""
    if isinstance(val, int): return val
    if not val: return None
    try: return int(re.sub(r'[^\d-]', '', str(val)))
    except: return None

def safe_float(val):
    """숫자 외의 문자가 섞여도 안전하게 실수로 변환"""
    if isinstance(val, (float, int)): return float(val)
    if not val: return None
    try: return float(re.sub(r'[^\d.-]', '', str(val)))
    except: return None

def extract_text(response) -> str:
    """Claude Messages API 응답에서 text 블록만 이어붙여 반환.
    web_search 툴을 쓰면 content 배열에 server_tool_use/web_search_tool_result
    블록이 text 블록과 섞여서 오므로, 순수 텍스트만 골라내야 JSON 파싱이 안전하다."""
    parts = []
    for block in (response.content or []):
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)

def filter_existing_db_products(stage1_data: list) -> list:
    if not supabase:
        return stage1_data

    try:
        response = supabase.table("product_candidates").select("brand, name").execute()
        existing_products = set()
        for item in (response.data or []):
            b = str(item.get("brand") or "").strip().lower()
            n = str(item.get("name") or "").strip().lower()
            if b and n:
                existing_products.add((b, n))

        filtered_list = []
        for item in stage1_data:
            item_brand = str(item.get("brand") or "").strip().lower()
            item_name = str(item.get("name") or "").strip().lower()

            if (item_brand, item_name) in existing_products:
                print(f"🚫 [기존 DB 중복] {item.get('brand')} - {item.get('name')} 제외")
                continue
            
            filtered_list.append(item)
        return filtered_list
    except Exception as e:
        print(f"⚠️ 중복 검사 에러: {str(e)}")
        return stage1_data

PROMPT_A_STORE = """
당신은 「다들」의 제품 발굴 담당자입니다.
다들은 흩어진 후기를 모아 제품을 고르고, 사려는 사람이 모이면 브랜드와 직접 가격을 협상합니다.
협상은 1인 사업자가 브랜드 담당자에게 직접 연락해서 진행합니다.

## 이번 작업
「{category}」 갈래에서 **네이버 스마트스토어를 직접 운영하는 국내 중소 브랜드**를 찾아 주세요.
제품이 아니라 **브랜드와 그 스토어 주소**를 찾는 단계입니다.
제품 목록·별점·리뷰 수·가격은 다음 단계에서 코드가 직접 수집하므로, 여기서 찾지 마세요.

★★★ 출력 규칙 — 다른 무엇보다 우선합니다 ★★★
작업 내내 사람에게 보이는 텍스트를 쓰지 않습니다. 검색 도구만 조용히 사용하세요.
- "~하겠습니다", "확인했습니다" 같은 진행 상황 설명을 단 한 글자도 쓰지 마세요.
- 브랜드 목록이나 검증 계획을 텍스트로 나열하지 마세요.
- 사람에게 보이는 응답은 맨 마지막에 JSON 배열 단 한 번뿐입니다.
- 이 규칙을 어기면 응답이 잘려 결과물이 통째로 사라집니다.

## 반드시 지킬 것 ★
1. 검색으로 확인한 것만 씁니다. 스토어 주소를 지어내지 마세요.
2. **주소를 실제로 확인하지 못했으면 그 브랜드는 빼세요.** null 로 남기지 마세요.
3. 브랜드가 좋은지 나쁜지 판단하지 마세요. 조건만 봅니다.

## 조건

**① 공식 스토어여야 합니다**
판매자가 **브랜드 본사**여야 합니다. 총판·리셀러가 운영하는 스토어는 협상 상대가 아닙니다.
- `brand.naver.com/{{브랜드}}` — 브랜드스토어. **공식일 가능성이 높습니다**
- `smartstore.naver.com/{{스토어}}` — 일반 스마트스토어. 스토어명이 브랜드명과 일치하는지 확인하세요
- 판별 근거: 「브랜드스토어」 배지 · 스토어명·사업자명이 브랜드와 일치 · 브랜드 홈페이지에서 이 스토어를 공식으로 안내

**② 1인이 협상할 수 있는 규모**
- 제외: 대기업·계열사·상장사·홈쇼핑 전속
- ★ **국내 브랜드처럼 보여도 모기업을 확인하세요.** 테팔은 프랑스 그룹세브 소속이라 제외입니다
- 제외: 브랜드 실체가 불분명한 노브랜드 수입품

**③ 취급 제품이 조건에 맞을 것**
내구재를 팔아야 합니다. 소모품·식품·화장품만 파는 브랜드는 제외합니다.

## 검색 방법 ★
스토어 주소는 **반드시 검색으로 확인해야 합니다.** 기억으로 답하지 마세요.
아래 브랜드를 하나씩, 빠짐없이 검색해서 공식 스토어가 있는지 확인하세요.

- 검색어 예시: `브랜드명 스마트스토어`, `브랜드명 네이버 브랜드스토어`, `브랜드명 공식몰`
- 한 브랜드에서 못 찾으면 그 브랜드만 건너뛰고 **다음 브랜드를 계속 확인하세요.**
- ★ 검색을 시작하지도 않고 빈 배열을 반환하지 마세요. 목록의 브랜드를 모두 확인한 뒤에 결론을 내세요.
- 일부만 찾아도 괜찮습니다. 찾은 것만 배열에 담으면 됩니다.

{brand_hint}

## 출력
설명 없이 JSON 배열만.

[
  {{
    "brand": "브랜드명",
    "store_url": "https://brand.naver.com/... 또는 https://smartstore.naver.com/...",
    "store_type": "brand" 또는 "smartstore",
    "official": true,
    "official_evidence": "공식으로 본 근거",
    "scale_evidence": "중소 브랜드로 본 근거 · 모기업 확인 결과",
    "sources": ["근거 URL"],
    "uncertain": ["확신이 낮은 항목"]
  }}
]

위 목록의 브랜드를 모두 확인한 뒤, **공식 스토어를 실제로 찾은 것만** 배열에 담으세요.
개수를 채우려고 억지로 넣지 마세요. 목록보다 적게 나오는 것은 정상입니다.
"""

PROMPT_STAGE_2 = """
아래는 1단계에서 뽑은 「다들」 제품 후보입니다.
이번에는 떨어뜨리는 쪽에 서서 다시 검토해 주세요.

## 검증 규칙
1. 카테고리가 「{category}」와 일치하지 않는 제품은 drop (사유: "카테고리 불일치").
2. 각 제품을 다시 검색해 브랜드·모델명·가격이 실제로 존재하는지 확인합니다.
   재확인되지 않으면 verdict: "drop", 사유: "재확인 실패".
3. 같은 제품이 이름만 다르게 두 번 들어왔으면 하나로 합칩니다.
4. 한 브랜드에서 3개를 넘기지 마세요. 넘치면 리뷰 영상이 많은 순으로 남깁니다.
5. 7개 조건 중 하나라도 어긋나면 떨어뜨립니다. 애매하면 떨어뜨리는 쪽을 고릅니다.
6. price_krw가 조건 없는 판매가가 맞는지 다시 확인합니다.
   카드·멤버십·쿠폰이 붙어야 나오는 값이면 member_price로 옮기고 price_krw를 다시 찾으세요.
7. site_url이 브랜드 자사몰(또는 브랜드스토어)이 아니라 아무나 여는 일반 스마트스토어라면
   drop 하거나, 진짜 자사몰 주소를 다시 찾아 수정하세요.
8. watch_urls 중 하나(특히 토스)가 site_url보다 30% 넘게 싸면 check_by_human에
   "채널 가격 격차"를 적습니다.
9. market_rating/market_reviews는 판정 근거로 쓰지 마세요. 옮겨 적기만 합니다.

## 통과한 제품에만 아래를 만들어 주세요
- aliases: 사람들이 실제로 검색할 만한 별칭 3~5개
- yt_queries: 유튜브 검색어 2~3개. 브랜드명 + 제품 종류 + 규격 조합
- yt_must: 영상 제목에 반드시 들어가야 통과시킬 단어 1~2개
  ★ 이 항목이 가장 중요합니다. 이게 없으면 다른 모델 댓글이 섞여 후기 전체가 오염됩니다.

## 검증 대상 데이터:
{stage1_json}

## 출력 형식 (JSON 배열만 출력)
[
  {{
    "verdict": "keep" 또는 "drop",
    "reason": "판정 사유 한 줄",
    "brand": "...",
    "name": "...",
    "sub": "{category}",
    "price_krw": 39800,
    "list_price": 45000,
    "member_price": null,
    "site_url": "검증된 브랜드 자사몰 주소",
    "watch_urls": {{"naver": "...", "toss": null, "coupang": null}},
    "brand_scale": "...",
    "brand_evidence": "...",
    "market_rating": 4.6,
    "market_reviews": 1148,
    "market_orders": 320,
    "market_url": "...",
    "aliases": ["검색 별칭들"],
    "yt_queries": ["유튜브 검색어들"],
    "yt_must": ["필수 포함 단어들"],
    "sources": ["근거 URL"],
    "check_by_human": ["사람이 반드시 눈으로 볼 항목"]
  }}
]

drop도 반드시 포함해 출력하세요. 왜 떨어졌는지가 다음 주 검색어를 고치는 재료가 됩니다.
"""

async def run_stage_a(category: str = "프라이팬", auto_save_db: bool = True):
    """A단계 · 브랜드 공식 스마트스토어 찾기.

    새 명세서(dadeul-gemini-candidates.md 0장)의 역할 분담을 따릅니다.
      A단계  브랜드 공식 스토어 찾기      ← 여기 (AI + 웹 검색)
      B단계  스토어 제품 목록·상세 수집   코드 (크롤링)
      C단계  후보 조건으로 판정           AI
      D단계  등록 형식으로 변환           AI

    제품·별점·리뷰 수·가격은 여기서 찾지 않습니다. AI에게 숫자를 물으면 지어냅니다.
    """
    if not client:
        raise HTTPException(status_code=500, detail=".env 파일에 ANTHROPIC_API_KEY가 설정되어 있지 않습니다.")

    # 💡 Claude Sonnet 5는 temperature 등 샘플링 파라미터를 기본값 외로 주면 400 에러를 냅니다.
    MODEL_STAGE_A = "claude-sonnet-5"

    # ★ 한 번에 처리할 브랜드 수. 검색 예산과 반드시 맞춰야 합니다.
    #   브랜드 35개를 주면서 검색 10회만 허용하면, 모델은 "예산이 부족하니 아무것도 못 하겠다"며
    #   빈 배열을 반환합니다(실제로 그렇게 실패했습니다). 브랜드당 검색 2회를 잡습니다.
    #   ⚠️ 테스트 단계라 5로 낮춰 둡니다. 결과가 만족스러우면 8~10으로 올리세요.
    BRAND_BATCH_SIZE = 5
    WEB_SEARCH_TOOL = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": BRAND_BATCH_SIZE * 2 + 2,
    }

    # ── brands 테이블에서 이 갈래의 후보 브랜드를 힌트로 넘긴다 ──
    #    이미 사람이 채워둔 목록이 있으면 AI가 브랜드를 "찾느라" 검색을 태우지 않아도 된다.
    brand_hint = ""
    known_brands = []
    already_done = set()
    if supabase:
        try:
            res = (supabase.table("brands")
                   .select("name, tier, store_url")
                   .eq("category", category)
                   .eq("is_active", True)
                   .execute())
            for it in (res.data or []):
                tier = (it.get("tier") or "").strip()
                nm = (it.get("name") or "").strip()
                if not nm:
                    continue
                # 이미 스토어를 찾아둔 브랜드는 다시 검색하지 않는다 (비용 절약)
                if it.get("store_url"):
                    already_done.add(nm.lower())
                    continue
                # 명백한 제외 등급은 애초에 후보가 아니다
                if tier in ("excluded_global", "excluded_large", "excluded_pb"):
                    continue
                known_brands.append(nm)
        except Exception as e:
            print(f"⚠️ brands 조회 실패(무시하고 진행): {e}")

    remaining = len(known_brands)
    batch = known_brands[:BRAND_BATCH_SIZE]

    if batch:
        brand_hint = (
            "이번에 확인할 브랜드는 아래 "
            f"{len(batch)}개입니다. 이 브랜드들만 확인하고, 다른 브랜드를 새로 찾지 마세요.\n"
            + "\n".join(f"- {b}" for b in batch)
        )
        print(f"📋 미처리 브랜드 {remaining}개 중 {len(batch)}개를 이번 배치로 처리합니다. "
              f"(이미 완료 {len(already_done)}개)")
        if remaining > len(batch):
            print(f"   → 남은 {remaining - len(batch)}개는 버튼을 다시 눌러 이어서 처리하세요.")
    else:
        brand_hint = "참고할 브랜드 목록이 아직 없습니다. 검색으로 직접 찾으세요."
        print("📋 brands 테이블에 이 갈래의 미처리 브랜드가 없습니다. AI가 직접 찾습니다.")

    print(f"\n======================================")
    print(f"🏪 [A단계] '{category}' 브랜드 공식 스토어 찾기 시작...")

    prompt_a = PROMPT_A_STORE.format(category=category, brand_hint=brand_hint)
    response_a = client.messages.create(
        model=MODEL_STAGE_A,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt_a}],
        tools=[WEB_SEARCH_TOOL],
    )

    text_a = extract_text(response_a)
    print(f"📝 [A단계 원본 응답]\n{text_a}\n")
    results = clean_json_response(text_a)
    print(f"📊 [A단계 파싱 완료] 총 {len(results)}개 브랜드 스토어 확인됨.")

    if not results:
        print("⚠️ A단계 결과가 0개입니다. 조기 종료합니다.")
        return {"status": "empty", "message": "A단계에서 공식 스토어를 찾지 못했습니다."}

    saved_count = 0
    save_errors = []
    skipped = 0

    if auto_save_db and supabase:
        print("\n💾 [brands 테이블 갱신 시작]...")
        for item in results:
            brand_name = (item.get("brand") or "").strip()
            store_url = (item.get("store_url") or "").strip()

            # 명세서 A단계 규칙 2: 주소를 확인 못 했으면 그 브랜드는 뺀다
            if not brand_name or not store_url:
                skipped += 1
                continue

            payload = {
                "store_url": store_url,
                "store_type": item.get("store_type"),
                "official": item.get("official"),
                "official_evidence": item.get("official_evidence"),
                "scale_evidence": item.get("scale_evidence"),
                "store_sources": item.get("sources", []),
                "store_uncertain": item.get("uncertain", []),
                "store_checked_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                # 이미 있는 브랜드면 갱신, 없으면 새로 넣는다
                existing = (supabase.table("brands")
                            .select("id")
                            .eq("category", category)
                            .ilike("name", brand_name)
                            .execute())

                if existing.data:
                    supabase.table("brands").update(payload).eq("id", existing.data[0]["id"]).execute()
                else:
                    payload.update({
                        "name": brand_name,
                        "category": category,
                        "tier": "candidate",
                        "tier_reason": "A단계에서 공식 스토어 확인됨",
                        "is_active": True,
                    })
                    supabase.table("brands").insert(payload).execute()
                saved_count += 1
            except Exception as e:
                print(f"❌ [저장 실패] {brand_name}: {e}")
                save_errors.append({"brand": brand_name, "error": str(e)})

        print(f"🎉 A단계 완료! {saved_count}개 저장 · 주소 없어 건너뜀 {skipped}개 · 에러 {len(save_errors)}개")

    return {
        "status": "success",
        "stage": "A",
        "category": category,
        "results": results,
        "saved_count": saved_count,
    }


@app.post("/api/v1/pipeline/find-stores")
async def find_stores_endpoint(category: str = "프라이팬", auto_save_db: bool = True):
    """A단계 실행 엔드포인트."""
    return await run_stage_a(category=category, auto_save_db=auto_save_db)
