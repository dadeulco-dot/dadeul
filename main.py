import os
import json
import re
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from supabase import create_client, Client
from dotenv import load_dotenv

# SQLAdmin & SQLAlchemy 관련 임포트
from sqladmin import Admin, ModelView
from sqlalchemy import create_engine, Column, Integer, String, BigInteger, Float, JSON, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker
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

# Render/Supabase PostgreSQL URL 호환 처리 (postgres:// -> postgresql://)
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Gemini Client 생성
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=HttpOptions(api_version="v1")
) if GEMINI_API_KEY else None

# Supabase SDK 클라이언트 (기존 파이프라인용)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# SQLAlchemy 엔진 생성 (SQLAdmin 및 관리자 판넬용)
engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()

# ----------------------------------------------------
# 2. SQLAdmin용 SQLAlchemy ORM 모델 정의
# ----------------------------------------------------
class ProductCandidateAdminModel(Base):
    __tablename__ = "product_candidates"

    # Supabase UUID / String Primary Key 호환을 위해 String(또는 BigInteger) 지정
    id = Column(String, primary_key=True, index=True)
    brand = Column(String, nullable=True)
    name = Column(String, nullable=True)
    category = Column(String, nullable=True)
    price_krw = Column(BigInteger, nullable=True)
    site_url = Column(String, nullable=True)
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
    admin = Admin(app, engine, title="다들(DADEUL) 어드민")

    class ProductCandidateAdminView(ModelView, model=ProductCandidateAdminModel):
        name = "후보 제품"
        name_plural = "후보 제품 목록"

        # 목록 테이블 표시 컬럼
        column_list = [
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.price_krw,
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
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.reject_reason,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.site_url,
            ProductCandidateAdminModel.ai_metadata,
        ]

        # 수정/생성 폼 필드 (id는 자동생성되므로 제외)
        form_columns = [
            ProductCandidateAdminModel.brand,
            ProductCandidateAdminModel.name,
            ProductCandidateAdminModel.category,
            ProductCandidateAdminModel.price_krw,
            ProductCandidateAdminModel.verdict,
            ProductCandidateAdminModel.reject_reason,
            ProductCandidateAdminModel.status,
            ProductCandidateAdminModel.site_url,
        ]

        column_searchable_list = [ProductCandidateAdminModel.brand, ProductCandidateAdminModel.name]
        column_filters = [ProductCandidateAdminModel.verdict, ProductCandidateAdminModel.status, ProductCandidateAdminModel.category]

        # 안전한 사이트 방문 링크 렌더링
        column_formatters = {
            ProductCandidateAdminModel.site_url: lambda m, a: Markup(
                f'<a href="{m.site_url}" target="_blank" class="btn btn-sm btn-outline-primary">🔗 사이트 방문</a>'
            ) if getattr(m, 'site_url', None) else "-"
        }

        can_view_details = True
        can_edit = True
        can_delete = True
        can_create = True

    admin.add_view(ProductCandidateAdminView)

# ----------------------------------------------------
# 5. 사용자 앱 (main.html) 서빙
# ----------------------------------------------------
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_user_app():
    """http://localhost:8000 접속 시 친구가 준 main.html 서빙"""
    if os.path.exists("static/main.html"):
        return FileResponse("static/main.html")
    return {"message": "static/main.html 파일을 찾을 수 없습니다. 폴더 구조를 확인하세요."}

# ----------------------------------------------------
# 6. 기존 파이프라인 로직 (Gemini Grounding)
# ----------------------------------------------------
def clean_json_response(raw_text: str):
    """Grounding 응답에서 마크다운 JSON 태그를 제거하고 파싱합니다."""
    clean_text = re.sub(r'^```json\s*', '', raw_text, flags=re.MULTILINE)
    clean_text = re.sub(r'^```\s*', '', clean_text, flags=re.MULTILINE)
    clean_text = clean_text.strip()
    return json.loads(clean_text)


PROMPT_STAGE_1 = """
당신은 「다들」의 제품 발굴 담당자입니다.

## 다들이 하는 일
흩어진 후기를 모아 제품을 고르고, 사려는 사람이 모이면 브랜드와 직접 가격을 협상합니다.
협상은 1인 사업자가 브랜드 담당자에게 직접 연락해서 진행합니다.

## 이번 작업
「{category}」 갈래에서 협상 후보가 될 만한 제품을 찾아 주세요.
목표: 10~25개. 부족하면 부족한 대로 내고, 개수를 채우려고 억지로 넣지 마세요.

## 반드시 지킬 것 ★
1. 검색으로 확인한 것만 씁니다. 기억이나 추측으로 제품명·브랜드·가격을 만들지 마세요.
2. 확인하지 못한 항목은 반드시 null 로 두세요. 빈칸을 채우려 짐작하지 마세요.
3. 제품마다 근거 URL을 최소 1개 답니다. URL을 못 찾으면 그 제품은 빼세요.
4. 제품이 좋은지 나쁜지 판단하지 마세요. 후기 평가는 다들이 별도 절차로 합니다.
   당신은 "협상 후보가 될 조건을 갖췄는가"만 봅니다.
5. 후기 원문·상세페이지 문구를 복사하지 마세요. 사실(숫자·스펙 값)만 옮깁니다.

## 후보 조건 — 7개를 모두 만족해야 합니다
① 브랜드 규모: 1인이 연락해 협상할 수 있는 국내 중소 브랜드 (대기업, 상장사, 노브랜드 수입품 제외)
② 모델이 특정되는가: 모델 단위로 딱 떨어지는 대표 규격 1개
③ 유튜브 리뷰: 최근 24개월 안 3개 이상
④ 내구재: 최소 3개월 이상 사용하는 물건
⑤ 가격 공개: 매일 동일 주소 확인 가능
⑥ 가격대: 정가 15,000원 ~ 300,000원
⑦ 수요 규모: 누적 리뷰 1,000건 이상 & 구매 건수 300건 이상 (확인 불가 시 null)

★ 별점은 통과 기준이 아닙니다. 숫자만 적으세요. 4.0 미만이면 uncertain에 「별점 낮음」 표기.
★ 리뷰 본문은 읽지 마세요. 숫자(별점, 리뷰수, 구매수)만 기록합니다.

## 무조건 제외
식품, 건기식, 의약외품, 화장품, 유아/아동용, 병행수입, 리셀, 중고, 출시 6개월 미만, 상시 할인 제품

## 출력 형식
설명이나 인삿말 없이 반드시 **JSON 배열만** 출력해 주세요.
[
  {{
    "brand": "브랜드명",
    "name": "모델명까지 포함한 제품명",
    "sub": "{category}",
    "price_krw": 39800,
    "site_url": "가격을 추적할 판매처 주소",
    "brand_scale": "중소",
    "brand_evidence": "판단 근거",
    "yt_review_count": 5,
    "yt_evidence": ["영상 URL"],
    "market_rating": 4.68,
    "market_reviews": 109000,
    "market_orders": 4507,
    "market_url": "숫자를 확인한 판매처 주소",
    "release": "2024-03 또는 null",
    "sources": ["근거 URL"],
    "uncertain": ["확신이 낮은 항목명"]
  }}
]
"""

PROMPT_STAGE_2 = """
아래는 1단계에서 뽑은 「다들」 제품 후보 목록입니다.
이번에는 **떨어뜨리는 쪽에 서서** 다시 검토해 주세요.

## 검증 규칙
1. 각 제품을 다시 검색해 브랜드·모델명·가격이 실제로 존재하는지 확인합니다. 재확인 실패 시 verdict: "drop", 사유: 「재확인 실패」.
2. 중복 제품은 하나로 합칩니다.
3. 한 브랜드에서 3개를 넘기지 마세요. 넘치면 리뷰 영상이 많은 순으로 남깁니다.
4. 7개 조건 중 하나라도 어긋나면 떨어뜨립니다. 애매하면 떨어뜨리는 쪽을 고릅니다.
5. market_reviews, market_orders를 다시 확인하여 1단계 값과 차이가 크면 check_by_human에 「수치 불일치」 작성.
★ market_rating은 높든 낮든 판정 근거로 쓰지 마세요.

## 통과한 제품(keep) 필수 생성 항목
- aliases: 검색 별칭 3~5개
- yt_queries: 브랜드명 + 제품 종류 + 규격 조합 검색어 2~3개
- yt_must: 영상 제목 필수 포함 단어 1~2개 (오염 방지 핵심)

## 검증 대상 데이터:
{stage1_json}

## 출력 형식
설명이나 인삿말 없이 반드시 **JSON 배열만** 출력해 주세요.
[
  {{
    "verdict": "keep" 또는 "drop",
    "reason": "판정 사유 한 줄",
    "brand": "...",
    "name": "...",
    "sub": "...",
    "price_krw": 39800,
    "site_url": "...",
    "market_rating": 4.68,
    "market_reviews": 109000,
    "market_orders": 4507,
    "aliases": ["..."],
    "yt_queries": ["..."],
    "yt_must": ["..."],
    "sources": ["..."],
    "check_by_human": ["사람이 볼 항목"]
  }}
]
"""

@app.post("/api/v1/pipeline/discover-candidates")
async def discover_candidates(category: str = "프라이팬", auto_save_db: bool = True):
    if not client:
        raise HTTPException(status_code=500, detail=".env 파일에 GEMINI_API_KEY가 설정되어 있지 않습니다.")

    try:
        search_config = GenerateContentConfig(
            tools=[Tool(google_search=GoogleSearch())]
        )

        # --- [1단계: 후보 넓게 긁기] ---
        print(f"\n🌐 [1단계] '{category}' 갈래 구글 실시간 검색 시작...")
        prompt_1 = PROMPT_STAGE_1.format(category=category)
        
        response_1 = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt_1,
            config=search_config
        )
        stage1_raw = response_1.text.strip()
        print(f"✅ [1단계 완료] 구글 실시간 검색 기반 데이터 추출 성공")

        # --- [2단계: 의심하고 검증하기] ---
        print(f"🔍 [2단계] 후보군 구글 실시간 재검증 및 keep/drop 판정 중...")
        prompt_2 = PROMPT_STAGE_2.format(stage1_json=stage1_raw)
        
        response_2 = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt_2,
            config=search_config
        )
        stage2_results = clean_json_response(response_2.text)
        print(f"✅ [2단계 완료] 총 {len(stage2_results)}개 정밀 판정 완료\n")

        # --- [3단계: Supabase DB 저장] ---
        saved_count = 0
        save_errors = []

        if auto_save_db and supabase:
            print("💾 [Supabase DB 저장 시작]...")
            for item in stage2_results:
                is_keep = item.get("verdict") == "keep"
                brand_val = item.get("brand")
                name_val = item.get("name")
                category_val = item.get("sub", category)
                
                db_payload = {
                    "brand": brand_val,
                    "name": name_val,
                    "category": category_val,
                    "price_krw": item.get("price_krw"),
                    "site_url": item.get("site_url"),
                    "verdict": item.get("verdict", "keep"),
                    "reject_reason": item.get("reason"),
                    "status": "PENDING_APPROVAL" if is_keep else "REJECTED",
                    "ai_metadata": {
                        "market_rating": item.get("market_rating"),
                        "market_reviews": item.get("market_reviews"),
                        "market_orders": item.get("market_orders"),
                        "aliases": item.get("aliases", []),
                        "yt_queries": item.get("yt_queries", []),
                        "yt_must": item.get("yt_must", []),
                        "human_check_tags": item.get("check_by_human", []),
                        "sources": item.get("sources", [])
                    }
                }

                try:
                    res = supabase.table("product_candidates").insert(db_payload).execute()
                    if res.data:
                        saved_count += 1
                except Exception as insert_err:
                    print(f"⚠️ 저장 실패 [{item.get('name')}]: {str(insert_err)}")
                    save_errors.append({"name": item.get("name"), "error": str(insert_err)})

            print(f"🎉 Supabase DB 저장 완료: {saved_count}개 성공, {len(save_errors)}개 실패\n")

        keeps = [i for i in stage2_results if i.get("verdict") == "keep"]
        drops = [i for i in stage2_results if i.get("verdict") == "drop"]

        return {
            "status": "success",
            "category": category,
            "summary": {
                "total_evaluated": len(stage2_results),
                "keep_count": len(keeps),
                "drop_count": len(drops),
                "db_saved_count": saved_count,
                "db_error_count": len(save_errors)
            },
            "save_errors": save_errors,
            "results": stage2_results
        }

    except Exception as e:
        print(f"❌ 파이프라인 오류 발생: {str(e)}")
        return {
            "status": "error",
            "detail": str(e)
        }
