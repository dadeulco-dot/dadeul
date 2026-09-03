import os
import json
import re
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from supabase import create_client, Client
from dotenv import load_dotenv

# SQLAdmin & SQLAlchemy 관련 임포트 (Float 추가됨)
from sqladmin import Admin, ModelView, expose
from sqlalchemy import create_engine, Column, String, BigInteger, Float, JSON
from sqlalchemy.orm import declarative_base
from markupsafe import Markup

# 최신 google-genai SDK 임포트
from google import genai
from google.genai.types import (
    GenerateContentConfig,
    GoogleSearch,
    HttpOptions,
    Tool,
)

# ----------------------------------------------------
# 1. 환경 변수 및 DB 연결 설정
# ----------------------------------------------------
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=HttpOptions(api_version="v1")
) if GEMINI_API_KEY else None

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()

# ----------------------------------------------------
# 2. SQLAdmin용 SQLAlchemy ORM 모델 정의 (교차 검증 컬럼 추가)
# ----------------------------------------------------
class ProductCandidateAdminModel(Base):
    __tablename__ = "product_candidates"

    id = Column(String, primary_key=True, index=True)
    brand = Column(String, nullable=True)
    name = Column(String, nullable=True)
    category = Column(String, nullable=True)
    price_krw = Column(BigInteger, nullable=True)
    site_url = Column(String, nullable=True)
    
    # 💡 별점 vs 다들 교차검증 컬럼
    market_rating = Column(Float, nullable=True)
    market_reviews = Column(BigInteger, nullable=True)
    dadeul_label = Column(String, nullable=True)
    dadeul_comment = Column(String, nullable=True)
    
    verdict = Column(String, default="keep")
    reject_reason = Column(String, nullable=True)
    status = Column(String, default="PENDING_APPROVAL")
    ai_metadata = Column(JSON, nullable=True)

# ----------------------------------------------------
# 3. FastAPI 앱 및 기본 설정
# ----------------------------------------------------
app = FastAPI(title="다들 (DADEUL) - 구글 실시간 검색 기반 제품 발굴 파이프라인", version="6.0.0")

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
    # 모바일 호환 custom_list.html을 불러오기 위해 templates_dir 연결
    admin = Admin(app, engine, title="다들(DADEUL) 어드민", templates_dir="templates")

    class ProductCandidateAdminView(ModelView, model=ProductCandidateAdminModel):
        name = "후보 제품"
        name_plural = "후보 제품 목록"

        list_template = "custom_list.html"

        # 목록 표시 컬럼
        column_list = [
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.price_krw,
            ProductCandidateAdminModel.market_rating,
            ProductCandidateAdminModel.dadeul_label,
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.site_url,
        ]

        # 상세 보기 표시 컬럼
        column_details_list = [
            ProductCandidateAdminModel.id,
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.price_krw,
            ProductCandidateAdminModel.market_rating,
            ProductCandidateAdminModel.market_reviews,
            ProductCandidateAdminModel.dadeul_label,
            ProductCandidateAdminModel.dadeul_comment,
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.reject_reason,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.site_url,
            ProductCandidateAdminModel.ai_metadata,
        ]

        # 수정/생성 폼 필드
        form_columns = [
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.price_krw,
            ProductCandidateAdminModel.market_rating,
            ProductCandidateAdminModel.market_reviews,
            ProductCandidateAdminModel.dadeul_label,
            ProductCandidateAdminModel.dadeul_comment,
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.reject_reason,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.site_url,
        ]

        column_searchable_list = ["brand", "name"]

        # 안전한 외부 링크 렌더링
        column_formatters = {
            ProductCandidateAdminModel.site_url: lambda m, a: Markup(
                f'<a href="{m.site_url}" target="_blank" class="btn btn-sm btn-outline-primary">🔗 사이트 방문</a>'
            ) if getattr(m, 'site_url', None) else "-"
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
# 6. 파이프라인 로직 및 중복 제거 (Gemini Grounding)
# ----------------------------------------------------
def clean_json_response(raw_text: str):
    clean_text = re.sub(r'^```json\s*', '', raw_text, flags=re.MULTILINE)
    clean_text = re.sub(r'^```\s*', '', clean_text, flags=re.MULTILINE)
    clean_text = clean_text.strip()
    return json.loads(clean_text)

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
다들의 핵심 가치는 **[판매처 별점]과 [실제 장기 사용 후기]를 교차 검증하여 그 격차(갭)를 드러내는 것**입니다.
예: "네이버 별점 4.8이지만 다들 분석 결과는 대체로 불만/의견 갈림" -> 이 격차가 콘텐츠 자산입니다.

## 이번 작업 (카테고리 엄격 제한 ★)
현재 작업 카테고리: 「{category}」

⚠️ 반드시 오직 「{category}」 카테고리에 완벽히 속하는 제품만 수집하세요. 다른 카테고리가 섞이면 안 됩니다.

## 반드시 지킬 것 ★
1. 검색으로 확인한 정량 데이터(별점, 리뷰수, 가격)만 명확히 수집합니다.
2. 판매처 별점과 실제 텍스트 후기 분석 내용 간의 격차(교차 검증 포인트)를 찾아내어 기록하세요.
3. 확인되지 않은 항목은 null로 처리하세요.

## 후보 조건 (7가지)
① 브랜드 규모: 국내 중소 브랜드 (대기업, 상장사 제외)
② 모델 특정: 모델 단위로 특정되는 대표 규격 1개
③ 유튜브 리뷰: 최근 24개월 안 3개 이상
④ 내구재: 3개월 이상 사용하는 물건
⑤ 가격 공개: 동일 주소 확인 가능
⑥ 가격대: 정가 15,000원 ~ 300,000원
⑦ 수요 규모: 누적 리뷰 1,000건 이상 & 구매 건수 300건 이상

## 무조건 제외
지정된 「{category}」 외 타 제품군 전체, 식품, 건기식, 화장품, 병행수입, 리셀, 중고 등

## 출력 형식 (JSON 배열만 출력)
[
  {{
    "brand": "브랜드명",
    "name": "모델명 포함 제품명",
    "sub": "{category}",
    "price_krw": 39800,
    "site_url": "판매처 주소",
    "market_rating": 4.8,
    "market_reviews": 1148,
    "dadeul_label": "의견 갈림 또는 대체로 불만 또는 만족",
    "dadeul_comment": "별점은 높은데 오래 쓰신 분들 사이엔 말이 갈려요",
    "sources": ["근거 URL"]
  }}
]
"""

PROMPT_STAGE_2 = """
아래는 1단계에서 뽑은 「다들」 제품 후보 목록입니다.
교차 검증 관점(별점 vs 다들 평가 격차)을 유지하며 **검증 및 필터링**을 진행해 주세요.

## 검증 규칙
1. 카테고리가 「{category}」와 일치하지 않는 제품은 즉시 drop (사유: 「카테고리 불일치」).
2. 별점과 실제 후기의 격차(교차 검증 포인트)가 명확한 제품을 우대합니다.
3. 브랜드당 최대 3개까지만 유지합니다.
4. 재확인 실패 시 verdict: "drop", 사유: 「재확인 실패」.

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
    "site_url": "...",
    "market_rating": 4.8,
    "market_reviews": 1148,
    "dadeul_label": "의견 갈림",
    "dadeul_comment": "별점은 높은데 오래 쓰신 분들 사이엔 말이 갈려요",
    "aliases": ["검색 별칭들"],
    "yt_queries": ["유튜브 검색어들"],
    "yt_must": ["필수 포함 단어들"],
    "sources": ["근거 URL"]
  }}
]
"""

async def run_pipeline(category: str = "후라이팬", auto_save_db: bool = True):
    if not client:
        raise HTTPException(status_code=500, detail=".env 파일에 GEMINI_API_KEY가 설정되어 있지 않습니다.")

    search_config = GenerateContentConfig(tools=[Tool(google_search=GoogleSearch())])

    print(f"\n🌐 [1단계] '{category}' 구글 실시간 검색 시작...")
    prompt_1 = PROMPT_STAGE_1.format(category=category)
    response_1 = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=prompt_1,
        config=search_config
    )
    stage1_parsed = clean_json_response(response_1.text)
    stage1_filtered = filter_existing_db_products(stage1_parsed)

    print(f"🔍 [2단계] 후보군 구글 실시간 재검증 및 keep/drop 판정 중...")
    prompt_2 = PROMPT_STAGE_2.format(
        category=category, 
        stage1_json=json.dumps(stage1_filtered, ensure_ascii=False)
    )
    response_2 = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=prompt_2,
        config=search_config
    )
    stage2_results = clean_json_response(response_2.text)

    saved_count = 0
    save_errors = []

    if auto_save_db and supabase:
        print("💾 [Supabase DB 저장 시작]...")
        for item in stage2_results:
            is_keep = item.get("verdict") == "keep"
            db_payload = {
                "brand": item.get("brand"),
                "name": item.get("name"),
                "category": item.get("sub", category),
                "price_krw": item.get("price_krw"),
                "site_url": item.get("site_url"),
                
                # 💡 교차검증 데이터 DB 맵핑
                "market_rating": item.get("market_rating"),
                "market_reviews": item.get("market_reviews"),
                "dadeul_label": item.get("dadeul_label"),
                "dadeul_comment": item.get("dadeul_comment"),
                
                "verdict": item.get("verdict", "keep"),
                "reject_reason": item.get("reason"),
                "status": "PENDING_APPROVAL" if is_keep else "REJECTED",
                "ai_metadata": {
                    "aliases": item.get("aliases", []),
                    "yt_queries": item.get("yt_queries", []),
                    "yt_must": item.get("yt_must", []),
                    "sources": item.get("sources", [])
                }
            }

            try:
                res = supabase.table("product_candidates").insert(db_payload).execute()
                if res.data:
                    saved_count += 1
            except Exception as insert_err:
                save_errors.append({"name": item.get("name"), "error": str(insert_err)})

    return {
        "status": "success",
        "category": category,
        "results": stage2_results,
        "saved_count": saved_count
    }

@app.post("/api/v1/pipeline/discover-candidates")
async def discover_candidates_endpoint(category: str = "후라이팬", auto_save_db: bool = True):
    return await run_pipeline(category=category, auto_save_db=auto_save_db)
