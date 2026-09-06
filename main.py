# 💡 [여기에 추가] 문자가 섞인 가격/리뷰수를 안전하게 숫자로 바꿔주는 방어 함수
def safe_int(val):
    if isinstance(val, int): return val
    if not val: return None
    try: return int(re.sub(r'[^\d-]', '', str(val)))
    except: return None

def safe_float(val):
    if isinstance(val, (float, int)): return float(val)
    if not val: return None
    try: return float(re.sub(r'[^\d.-]', '', str(val)))
    except: return None


async def run_pipeline(category: str = "후라이팬", auto_save_db: bool = True):
    # ...(중간 검색 로직 동일)...

    if auto_save_db and supabase:
        print("\n💾 [Supabase DB 저장 시작]...")
        for item in stage2_results:
            is_keep = item.get("verdict") == "keep"
            
            # 💡 [핵심 수정] safe_int, safe_float로 감싸서 DB 에러 원천 차단
            db_payload = {
                "brand": item.get("brand"),
                "name": item.get("name"),
                "category": item.get("sub", category),
                "price_krw": safe_int(item.get("price_krw")),
                "site_url": item.get("site_url"),
                "market_rating": safe_float(item.get("market_rating")),
                "market_reviews": safe_int(item.get("market_reviews")),
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
                # 에러 로그가 터미널에 상세히 찍히도록 개선
                print(f"❌ [DB 저장 실패] {item.get('name')}: {str(insert_err)}")
                save_errors.append({"name": item.get("name"), "error": str(insert_err)})

        print(f"🎉 파이프라인 완료! 총 {len(stage2_results)}개 중 {saved_count}개 DB 저장 성공 (에러: {len(save_errors)}개)")
