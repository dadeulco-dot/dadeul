import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, Column, String, Integer
from sqlalchemy.orm import declarative_base
from sqladmin import Admin, ModelView
from markupsafe import markup

# 1. 환경 변수 로드 (.env 및 Render 환경 변수)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# 2. Database & SQLAlchemy 설정
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Base = declarative_base()

# 3. DB 모델 정의 (ProductCandidate)
class ProductCandidate(Base):
    __tablename__ = "product_candidates"

    id = Column(String, primary_key=True)
    brand = Column(String)
    name = Column(String)
    category = Column(String)
    price_krw = Column(Integer)
    verdict = Column(String)
    status = Column(String)
    link = Column(String, nullable=True)  # 상품 사이트 주소 링크

# 4. FastAPI 앱 생성
app = FastAPI()

# 루트(/) 접속 시 어드민 페이지로 바로 리다이렉트
@app.get("/")
def read_root():
    return RedirectResponse(url="/admin")

# 5. SQLAdmin 모델 뷰 정의 (어드민 설정)
class ProductCandidateAdmin(ModelView, model=ProductCandidate):
    name = "후보 제품"
    name_plural = "후보 제품 목록"

    # 목록(List) 테이블에 보여줄 컬럼 (id 숨김)
    column_list = [
        ProductCandidate.brand,
        ProductCandidate.name,
        ProductCandidate.category,
        ProductCandidate.price_krw,
        ProductCandidate.verdict,
        ProductCandidate.status,
        ProductCandidate.link,
    ]

    # 상세보기(View) 페이지에 보여줄 컬럼
    column_details_list = [
        ProductCandidate.id,
        ProductCandidate.brand,
        ProductCandidate.name,
        ProductCandidate.category,
        ProductCandidate.price_krw,
        ProductCandidate.verdict,
        ProductCandidate.status,
        ProductCandidate.link,
    ]

    # 수정(Edit) / 생성(Create) 폼에 표시할 입력 필드
    form_columns = [
        ProductCandidate.brand,
        ProductCandidate.name,
        ProductCandidate.category,
        ProductCandidate.price_krw,
        ProductCandidate.verdict,
        ProductCandidate.status,
        ProductCandidate.link,
    ]

    # link 컬럼을 누르면 외부 사이트가 새 창으로 열리는 버튼으로 스타일링
    column_formatters = {
        ProductCandidate.link: lambda m, a: markup(
            f'<a href="{m.link}" target="_blank" class="btn btn-sm btn-outline-primary">🔗 사이트 방문</a>'
        ) if m.link else "-"
    }

    # 기능 권한 설정
    can_view_details = True  # 상세 정보 보기 (눈 모양 아이콘)
    can_edit = True          # DB 수정 (연필 아이콘)
    can_delete = True        # DB 삭제 (휴지통 아이콘)
    can_create = True        # 새 제품 등록

# 6. SQLAdmin 등록
admin = Admin(app, engine, title="다들(DADEUL) 어드민")
admin.add_view(ProductCandidateAdmin)
