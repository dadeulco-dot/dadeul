import os
import json
import re
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

    # ── 원본 스냅샷 (감사용) ──
    raw_stage1 = Column(JSON, nullable=True)
    raw_stage2 = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# ----------------------------------------------------
# 3. FastAPI 앱 및 기본 설정
# ----------------------------------------------------
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

        list_template = "custom_list.html"

        # ── 리스트: 후보를 빠르게 훑어보기 위한 최소 정보 ──
        column_list = [
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.price_krw,
            ProductCandidateAdminModel.list_price,
            ProductCandidateAdminModel.price_inflated,
            ProductCandidateAdminModel.market_reviews,
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.site_url,
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
            ProductCandidateAdminModel.price_krw: lambda m, a: _fmt_won(m.price_krw),
            ProductCandidateAdminModel.list_price: lambda m, a: _fmt_won(m.list_price),
            ProductCandidateAdminModel.price_inflated: lambda m, a: _fmt_flag(m.price_inflated),
        }

        column_formatters_detail = {
            ProductCandidateAdminModel.site_url: lambda m, a: _fmt_link(m.site_url, "🔗 자사몰"),
            ProductCandidateAdminModel.watch_naver: lambda m, a: _fmt_link(m.watch_naver, "🔗 네이버"),
            ProductCandidateAdminModel.watch_toss: lambda m, a: _fmt_link(m.watch_toss, "🔗 토스"),
            ProductCandidateAdminModel.watch_coupang: lambda m, a: _fmt_link(m.watch_coupang, "🔗 제휴"),
            ProductCandidateAdminModel.market_url: lambda m, a: _fmt_link(m.market_url, "🔗 수치 확인"),
            ProductCandidateAdminModel.price_krw: lambda m, a: _fmt_won(m.price_krw),
            ProductCandidateAdminModel.list_price: lambda m, a: _fmt_won(m.list_price),
            ProductCandidateAdminModel.member_price: lambda m, a: _fmt_won(m.member_price),
            ProductCandidateAdminModel.price_inflated: lambda m, a: _fmt_flag(m.price_inflated),
        }

        can_view_details = True
        can_edit = True
        can_delete = True
        can_create = True

        @expose("/fetch-products", methods=["POST"])
        async def fetch_products_action(self, request: Request):
            form_data = await request.form()
            category = form_data.get("category", "후라이팬")
            
            await run_pipeline(category=category, auto_save_db=True)
            return RedirectResponse(url="/admin/product-candidate-admin-model/list", status_code=303)

    admin.add_view(ProductCandidateAdminView)

# ----------------------------------------------------
# 5. 사용자 앱 (main.html) 서빙
# ----------------------------------------------------
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_user_app():
    if os.path.exists("static/main.html"):
        return FileResponse("static/main.html")
    return {"message": "static/main.html 파일을 찾을 수 없습니다. 폴더 구조를 확인하세요."}

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

PROMPT_STAGE_1 = """
당신은 「다들」의 제품 발굴 담당자입니다.

## 다들이 하는 일
흩어진 후기를 모아 제품을 고르고, 사려는 사람이 모이면 브랜드와 직접 가격을 협상합니다.
협상은 1인 사업자가 브랜드 담당자에게 직접 연락해서 진행합니다.
당신은 이 협상의 후보가 될 만한 제품을 찾는 역할만 합니다.
★ 제품이 좋은지 나쁜지, 별점이 실제 후기와 맞는지 안 맞는지는 여기서 판단하지 않습니다.
   그건 다들이 유튜브 댓글을 모은 뒤 별도의 공개된 규칙(label_of)으로 정합니다.

## 이번 작업 (카테고리 엄격 제한 ★)
현재 작업 카테고리: 「{category}」
반드시 오직 「{category}」에 속하는 제품만 찾으세요.
목표 5~8개. 부족하면 부족한 대로 내고, 개수를 채우려고 억지로 넣지 마세요.

★★★ 출력 규칙 — 다른 무엇보다 우선합니다 ★★★
당신은 이 작업 내내 어떤 텍스트도 출력하지 않습니다. 검색 도구만 조용히 반복해서 사용하세요.
- "~하겠습니다", "확인했습니다", "이제 더 효율적으로" 같은 진행 상황 설명을 단 한 글자도 쓰지 마세요.
- 브랜드 목록, 검증 계획, 중간 결과를 텍스트로 나열하지 마세요. 검색 도구 호출 안에서만 다루세요.
- 검색을 몇 번을 하든, 사람에게 보이는 텍스트 응답은 맨 마지막에 JSON 배열 단 한 번만 출력합니다.
- 이 규칙을 어기면 응답이 중간에 잘려 결과물이 통째로 사라집니다. 서술은 결과를 0개로 만듭니다.

★ 검색 방법 — 순서를 반드시 지키세요. (아래는 검색 도구 사용 순서일 뿐, 텍스트로 쓰라는 뜻이 아닙니다)
1. 먼저 「{category}」를 만드는 국내 중소 브랜드를 아는 대로 최소 5개 이상 나열해보고 검색으로 목록을 넓히세요.
   (예시가 필요하면 "국내 {category} 브랜드", "{category} 중소기업" 같은 검색으로 목록부터 넓게 훑으세요.)
2. 나열한 브랜드마다 대표 모델을 최소 1개씩 검색해서 조건을 확인하세요.
3. 확신 가는 3~5개만 찾고 멈추지 마세요. 나열한 브랜드를 다 확인하기 전엔 후보 목록을 마감하지 마세요.
4. 검색 예산이 넉넉하지 않습니다(약 6회). 브랜드 하나당 검색 1회로 최대한 많은 정보를 확인하고, 같은 브랜드를 여러 번 검색하지 마세요.
5. 이 모든 과정은 검색 도구 호출로만 진행하고, 사람에게는 아무것도 보고하지 않습니다.

## 반드시 지킬 것 ★
1. 검색으로 확인한 것만 씁니다. 기억이나 추측으로 제품명·브랜드·가격을 만들지 마세요.
2. 확인하지 못한 항목은 반드시 null로 두세요. 빈칸을 채우려 짐작하지 마세요.
3. 제품마다 근거 URL을 최소 1개 답니다. URL을 못 찾으면 그 제품은 빼세요.
4. 제품 평가(별점 해석, 다들 라벨 등)를 스스로 만들어내지 마세요. 정량 데이터만 그대로 옮겨 적습니다.
5. 후기 원문·상세페이지 문구를 복사하지 마세요. 사실(숫자·스펙 값)만 옮깁니다.

## 후보 조건 — 7개를 모두 만족해야 합니다

① 브랜드 규모
1인이 연락해 협상 테이블에 앉을 수 있는 국내 중소 브랜드.
- 필요: 자사몰 또는 스마트스토어를 직접 운영하고, 고객센터·문의 창구가 공개돼 있음
- 제외: 대기업·대기업 계열·글로벌 브랜드, 상장사, 홈쇼핑 전속 브랜드
- ★ 국내 브랜드처럼 보여도 모기업을 확인하세요. 예: 테팔은 프랑스 그룹세브 소속이라 제외입니다
- 제외: 브랜드 실체가 불분명한 노브랜드 수입품, 오픈마켓 전용 무명 셀러

② 모델이 특정되는가
제품명이 모델 단위로 딱 떨어져야 합니다. 크기·용량 파생이 있으면 가장 많이 팔리는 규격 하나만 고릅니다.

③ 유튜브 리뷰가 쌓이는가
최근 24개월 안에 이 제품(또는 정확히 같은 모델)을 다룬 리뷰 영상이 3개 이상,
그중 댓글이 달린 영상이 있어야 합니다.

④ 오래 쓰는 물건인가
최소 3개월 이상 쓰면서 장기 사용 후기가 쌓이는 내구재. 소모품·시즌 상품은 제외.

⑤ 가격이 공개돼 있고 추적 가능한가
공개 판매처에서 가격이 노출되고, 매일 같은 주소에서 확인할 수 있어야 합니다.

★ 가격은 세 종류를 구분해서 적어 주세요. 섞으면 안 됩니다.
| 종류 | 무엇 | 필드 |
|---|---|---|
| 표시가 | 판매처가 적어둔 정가 | list_price |
| 판매가 | 조건 없이 지금 누구나 사는 값 (배송비 포함) | price_krw ← 이게 기준 |
| 조건부가 | 카드·멤버십·쿠폰·앱 전용가 | member_price |

★ 표시가가 판매가의 2배를 넘으면 uncertain에 "정가 부풀림 의심"을 적어 주세요.
  다들은 부풀린 정가로 할인율을 크게 보이게 하지 않겠다고 약속했습니다.

⑥ 가격대
정가 15,000원 ~ 300,000원.

⑦ 수요 규모 — 아래를 모두 만족
- 누적 리뷰 1,000건 이상
- 구매 건수 300건 이상 (판매처에 표기된 값. 표기가 없으면 null로 두고 사람이 확인합니다)
- 여러 판매처에 흩어져 있으면 가장 많이 파는 곳 하나를 기준으로 합니다

## 판매처 · 가격 감시 채널
- site_url: 브랜드 자사몰 — 「사러 가기」가 향할 곳(기준가). 자사몰이 없으면 브랜드가 직접 운영하는
  브랜드스토어(brand.naver.com)를 대신 쓸 수 있지만, 아무나 여는 일반 스마트스토어를 자사몰로 대신 쓰지 마세요.
- watch_urls: 가격을 매일 감시할 다른 채널 — {{"naver": "...", "toss": null, "coupang": null}}.
  프로모션 채널에서 더 싸게 팔리고 있으면 나중에 협상가가 무의미해지므로 필요합니다.
  단, 어느 채널에서도 리뷰 본문은 읽거나 옮기지 마세요. 숫자와 주소만 가져옵니다.

★ 별점은 통과 기준이 아닙니다. 숫자만 그대로 적어 주세요. 높다고 뽑거나 낮다고 떨어뜨리지 마세요.
  다만 별점이 4.0 미만이면 uncertain에 "별점 낮음"을 적어 사람이 보게 하세요.
★ 리뷰 본문을 읽거나 옮기지 마세요. 별점·리뷰 수·구매 수 세 숫자만 기록합니다.

## 무조건 제외
- 식품·건강기능식품·의약외품·화장품
- 유아·아동이 직접 쓰는 제품
- 병행수입품, 리셀 상품, 중고
- 출시 6개월 미만 신제품
- 상시 할인 중이라 정가가 의미 없는 제품
- 지정된 「{category}」 외 타 제품군 전체

## 출력 형식 (JSON 배열만 출력)
[
  {{
    "brand": "브랜드명",
    "name": "모델명 포함 제품명",
    "sub": "{category}",
    "price_krw": 39800,
    "list_price": 45000,
    "member_price": null,
    "site_url": "브랜드 자사몰 주소",
    "watch_urls": {{"naver": "...", "toss": null, "coupang": null}},
    "brand_scale": "중소",
    "brand_evidence": "자사몰 운영·고객센터 공개 등 판단 근거",
    "yt_review_count": 5,
    "yt_evidence": ["영상 URL"],
    "market_rating": 4.6,
    "market_reviews": 1148,
    "market_orders": 320,
    "market_url": "숫자를 확인한 판매처 주소",
    "release": "2024-03 또는 null",
    "sources": ["근거 URL"],
    "uncertain": ["확신 낮은 항목명"]
  }}
]
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

async def run_pipeline(category: str = "후라이팬", auto_save_db: bool = True):
    if not client:
        raise HTTPException(status_code=500, detail=".env 파일에 ANTHROPIC_API_KEY가 설정되어 있지 않습니다.")

    # 💡 Claude Sonnet 5는 temperature 등 샘플링 파라미터를 기본값 외로 주면 400 에러를 냅니다.
    #    JSON 강제는 프롬프트 지시("설명 없이 JSON 배열만")와 clean_json_response의
    #    괄호 추출 방어 로직으로 대신합니다.
    WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 6}
    # 💡 1단계(넓게 찾기)는 저렴한 Haiku로, 2단계(검증·판정)만 Sonnet으로.
    #    검색-토큰 누적 때문에 1단계 비용이 가장 크게 늘어나는 구간이라 여기를 먼저 낮춘다.
    MODEL_STAGE1 = "claude-haiku-4-5-20251001"
    MODEL_STAGE2 = "claude-sonnet-5"

    print(f"\n======================================")
    print(f"🌐 [1단계] '{category}' Claude 웹 검색 시작...")
    prompt_1 = PROMPT_STAGE_1.format(category=category)
    response_1 = client.messages.create(
        model=MODEL_STAGE1,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt_1}],
        tools=[WEB_SEARCH_TOOL],
    )

    text_1 = extract_text(response_1)
    print(f"📝 [1단계 원본 응답]\n{text_1}\n")
    stage1_parsed = clean_json_response(text_1)
    print(f"📊 [1단계 파싱 완료] 총 {len(stage1_parsed)}개 제품 추출됨.")

    # 💡 여기서 자동으로 Sonnet 재시도를 걸지 않는다 — 실패는 실패로 명확히 보여주고,
    #    재시도할지는 사람이 로그를 보고 직접 버튼을 다시 눌러 결정하게 한다.
    #    (돈이 나가는 API 호출을 사용자 모르게 한 번 더 트리거하지 않기 위함)
    if not stage1_parsed:
        print("⚠️ 1단계 추출 결과가 0개입니다. 파이프라인을 조기 종료합니다.")
        return {"status": "empty", "message": "1단계에서 조건에 맞는 제품을 찾지 못했습니다."}

    stage1_filtered = filter_existing_db_products(stage1_parsed)

    print(f"\n🔍 [2단계] 후보군 Claude 웹 재검증 및 keep/drop 판정 중...")
    prompt_2 = PROMPT_STAGE_2.format(
        category=category, 
        stage1_json=json.dumps(stage1_filtered, ensure_ascii=False)
    )
    response_2 = client.messages.create(
        model=MODEL_STAGE2,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt_2}],
        tools=[WEB_SEARCH_TOOL],
    )

    text_2 = extract_text(response_2)
    print(f"📝 [2단계 원본 응답]\n{text_2}\n")
    stage2_results = clean_json_response(text_2)
    print(f"📊 [2단계 파싱 완료] 총 {len(stage2_results)}개 제품 검증 완료.")

    # 💡 stage2가 아직 재출력하지 않는 필드(brand_scale, release 등)를 stage1 값으로
    #    폴백시키고, raw_stage1 감사 스냅샷도 남기기 위한 매칭 테이블
    stage1_lookup = {}
    for it in stage1_filtered:
        key = (str(it.get("brand") or "").strip().lower(), str(it.get("name") or "").strip().lower())
        stage1_lookup[key] = it

    saved_count = 0
    save_errors = []

    if auto_save_db and supabase:
        print("\n💾 [Supabase DB 저장 시작]...")
        for item in stage2_results:
            is_keep = item.get("verdict") == "keep"
            key = (str(item.get("brand") or "").strip().lower(), str(item.get("name") or "").strip().lower())
            s1 = stage1_lookup.get(key) or {}

            # 💡 safe_int, safe_float 적용으로 DB 저장 안정성 극대화
            watch = item.get("watch_urls", s1.get("watch_urls")) or {}
            list_price = safe_int(item.get("list_price", s1.get("list_price")))
            price_krw = safe_int(item.get("price_krw", s1.get("price_krw")))
            price_inflated = bool(list_price and price_krw and list_price > 2 * price_krw)

            db_payload = {
                "brand": item.get("brand"),
                "name": item.get("name"),
                "category": item.get("sub", category),
                "verdict": item.get("verdict", "keep"),
                "reject_reason": item.get("reason"),
                "status": "PENDING_APPROVAL" if is_keep else "REJECTED",
                "stage": "stage2_verified",

                "price_krw": price_krw,
                "list_price": list_price,
                "member_price": safe_int(item.get("member_price", s1.get("member_price"))),
                "price_inflated": price_inflated,

                "site_url": item.get("site_url", s1.get("site_url")),
                "watch_naver": watch.get("naver"),
                "watch_toss": watch.get("toss"),
                "watch_coupang": watch.get("coupang"),

                "brand_scale": item.get("brand_scale", s1.get("brand_scale")),
                "brand_evidence": item.get("brand_evidence", s1.get("brand_evidence")),

                "yt_review_count": safe_int(item.get("yt_review_count", s1.get("yt_review_count"))),
                "yt_evidence": item.get("yt_evidence", s1.get("yt_evidence", [])),
                "yt_queries": item.get("yt_queries", []),
                "yt_must": item.get("yt_must", []),
                "aliases": item.get("aliases", []),

                # 참고용 시장 신호일 뿐, 이 값 자체가 다들 라벨이 되지는 않음 (명세서 2-2)
                "market_rating": safe_float(item.get("market_rating", s1.get("market_rating"))),
                "market_reviews": safe_int(item.get("market_reviews", s1.get("market_reviews"))),
                "market_orders": safe_int(item.get("market_orders", s1.get("market_orders"))),
                "market_url": item.get("market_url", s1.get("market_url")),

                "release": item.get("release", s1.get("release")),

                "sources": item.get("sources", s1.get("sources", [])),
                "uncertain": item.get("uncertain", s1.get("uncertain", [])),
                "check_by_human": item.get("check_by_human", []),

                "raw_stage1": s1 or None,
                "raw_stage2": item,
            }

            try:
                res = supabase.table("product_candidates").insert(db_payload).execute()
                if res.data:
                    saved_count += 1
            except Exception as insert_err:
                print(f"❌ [DB 저장 실패] {item.get('name')}: {str(insert_err)}")
                save_errors.append({"name": item.get("name"), "error": str(insert_err)})

        print(f"🎉 파이프라인 완료! 총 {len(stage2_results)}개 중 {saved_count}개 DB 저장 성공 (에러: {len(save_errors)}개)")

    return {
        "status": "success",
        "category": category,
        "results": stage2_results,
        "saved_count": saved_count
    }

@app.post("/api/v1/pipeline/discover-candidates")
async def discover_candidates_endpoint(category: str = "후라이팬", auto_save_db: bool = True):
    return await run_pipeline(category=category, auto_save_db=auto_save_db)
