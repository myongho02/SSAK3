#!/usr/bin/env python3
"""발표 시연용 데모 데이터셋 시드 스크립트.

발표 당일 시연 전 한 번 실행하면 다음을 수행:
1. 정상 기사 URL 5건(주요 언론사) 분석 → 신뢰 가능 등급 기대
2. 자극적 표현 의심 기사 URL 5건 분석 → 주의/의심 등급 기대
3. 결과를 DB에 캐시하여 발표 시연 시 즉시 응답 (캐시 적중률 ↑ 효과)

사용법:
    python3 demo_seed.py
    # 또는 발표 직전 한 번 실행 후 결과 캐시 상태로 시연
"""

import requests
import time
import sys

API_URL = "http://localhost:5001"

# ── 정상 기사 (주요 언론사, 객관 보도 톤) ──
# 발표 시연 시 "신뢰 가능" 등급(80+)이 기대되는 케이스
# 검증일: 2026-05-15. 학교 발표 시연용 URL 셋 (main 시스템에서 실제 작동 확인).
# 발표 전날 URL 만료 가능성이 있으므로 collect_news_bulk.py 로 신선 URL 보충 권장.
NORMAL_ARTICLES = [
    # 연합뉴스 정치 (신뢰가능 86.8점 — 검증)
    "https://www.yna.co.kr/view/AKR20260515145500063",
    # 연합뉴스 정치 (신뢰가능 85.6점 — 검증)
    "https://www.yna.co.kr/view/AKR20260515144100062",
]

# ── 자극성 표현 의심 기사 ──
# 자극적 단어나 인용 제목으로 자극성 점수가 낮은 케이스 (의심 등급 기대)
SUSPECT_ARTICLES = [
    # 매경 사회 (의심 42.0점 — 자극적 단어 "거짓말/속여/챙긴")
    "https://www.mk.co.kr/news/society/12046305",
    # 매경 사회 (의심 47.3점 — 자극적 단어 "노려/해킹")
    "https://www.mk.co.kr/news/society/12046474",
    # 매경 정치 (의심 45.6점 — [속보] + 미사일/긴급)
    "https://www.mk.co.kr/news/politics/12020887",
    # 경향 사회 (의심 46.4점 — 의문형 자극 "진짜 파업하나")
    "https://www.khan.co.kr/article/202605132042005/?utm_source=khan_rss&utm_medium=r",
    # 경향 사회 (의심 46.3점 — 인용형 자극 + 위증/구형)
    "https://www.khan.co.kr/article/202605131458001/?utm_source=khan_rss&utm_medium=r",
]


def analyze_url(url):
    """URL을 분석 요청 후 완료까지 폴링한다.
    Returns: (성공 여부, 결과 dict 또는 에러 메시지, 소요 시간)
    """
    t0 = time.time()
    try:
        r = requests.post(f"{API_URL}/analyze", json={"url": url}, timeout=10).json()
    except Exception as e:
        return False, f"요청 실패: {e}", 0

    if "result" in r:
        # 이미 분석된 결과 즉시 반환
        return True, r["result"], time.time() - t0

    job_id = r.get("job_id")
    if not job_id:
        return False, f"job 생성 실패: {r}", 0

    # 완료 대기 (최대 60초)
    while time.time() - t0 < 60:
        try:
            jobs = requests.get(f"{API_URL}/jobs", timeout=5).json()
            job = next((j for j in jobs if j["id"] == job_id), None)
            if job and job["status"] == "done":
                if job.get("result_id"):
                    res = requests.get(f"{API_URL}/results/{job['result_id']}", timeout=5).json()
                    return True, res, time.time() - t0
                return True, {"status": "done"}, time.time() - t0
            if job and job["status"] == "failed":
                return False, job.get("error_message", "분석 실패"), time.time() - t0
        except Exception:
            pass
        time.sleep(0.5)

    return False, "타임아웃", time.time() - t0


def seed_set(name, urls, expected_grade):
    """URL 셋을 분석하고 결과를 출력한다."""
    print(f"\n{'='*60}")
    print(f"[{name}] {len(urls)}건 — 예상 등급: {expected_grade}")
    print(f"{'='*60}")

    if not urls:
        print("  (비어 있음 — demo_seed.py 안의 SUSPECT_ARTICLES 리스트를 채워주세요)")
        return

    for idx, url in enumerate(urls, 1):
        print(f"\n[{idx}/{len(urls)}] {url[:70]}")
        ok, res, elapsed = analyze_url(url)
        if ok and isinstance(res, dict) and 'total_score' in res:
            grade = res.get('grade', '-')
            score = res.get('total_score', 0)
            title = (res.get('title') or '')[:50]
            content = res.get('content_score', 0)
            prov = res.get('provocative_score', 0)
            src = res.get('source_score', 0)
            mark = "✓" if grade == expected_grade else "△"
            print(f"  {mark} {score:.1f}점 [{grade}] — {title}")
            print(f"     본문일치: {content:.1f} | 자극성: {prov:.1f} | 출처: {src:.1f} | {elapsed:.1f}초")
        else:
            print(f"  ✗ 실패: {res}")


def main():
    # API 서버 헬스 체크
    try:
        r = requests.get(API_URL, timeout=5)
        if r.status_code != 200:
            print(f"[오류] API 서버 응답 비정상: {r.status_code}")
            sys.exit(1)
    except Exception as e:
        print(f"[오류] API 서버에 접근 불가 — docker compose가 떠있는지 확인하세요: {e}")
        sys.exit(1)

    print("발표 시연용 데모 데이터셋 시드 시작...")

    seed_set("정상 기사 (주요 언론사)", NORMAL_ARTICLES, "신뢰 가능")
    seed_set("자극성 의심 기사", SUSPECT_ARTICLES, "주의 필요")

    print(f"\n{'='*60}")
    print("[완료] 시연 데이터 캐싱됨. 발표 시 같은 URL 재분석 시 즉시 응답.")
    print(f"{'='*60}")
    print("\n참고:")
    print("  - 캐시 적중 시 처리 시간이 1.5초 → 0.22초로 단축됩니다.")
    print("  - 결과는 대시보드(http://localhost:8501)에서 확인 가능합니다.")


if __name__ == "__main__":
    main()
