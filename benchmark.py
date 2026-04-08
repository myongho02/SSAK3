#!/usr/bin/env python3
"""Worker 스케일링 성능 벤치마크 스크립트

사용법:
    docker compose up -d rabbitmq api dashboard
    python3 benchmark.py

동작 흐름:
    1. 네이버 뉴스 최신 기사 URL 20개 수집
    2. Worker 1/3/5개 각각에 대해:
       - DB 초기화 (이전 데이터 삭제)
       - Worker N개 시작 → 20개 URL 전송 → 전부 처리될 때까지 대기 → 시간 측정
       - Worker 정지
    3. 결과를 JSON 파일로 저장 (대시보드에서 읽어서 시각화)
"""

import subprocess
import time
import json
import sys
import os
import sqlite3
import requests
from bs4 import BeautifulSoup

# ========== 설정 ==========
# 스크립트 위치 기준으로 프로젝트 루트를 자동 결정
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
API_URL = "http://localhost:5001"                 # 호스트에서 접근하는 API 주소
DB_PATH = None                                     # Docker 볼륨 내 DB 경로 (아래에서 자동 탐색)
RESULT_FILE = "benchmark_results.json"             # 결과 저장 파일
NUM_ARTICLES = 20                                  # 테스트 기사 수
WORKER_COUNTS = [1, 3, 5]                          # 테스트할 Worker 수
POLL_INTERVAL = 2                                  # DB 폴링 간격 (초)
TIMEOUT = 600                                      # 최대 대기 시간 (초)


def find_db_path():
    """Docker 볼륨 내 results.db 경로를 찾는다.
    docker compose exec로 컨테이너 안에서 DB를 조작한다.
    """
    # 호스트에서는 Docker 볼륨에 직접 접근할 수 없으므로
    # API 컨테이너를 통해 DB를 조작한다
    pass


def collect_naver_news_urls(count=20):
    """네이버 뉴스 메인에서 최신 기사 URL을 수집한다.

    네이버 뉴스 랭킹 페이지에서 각 섹션(정치, 경제, 사회, 생활, IT)의
    기사 링크를 추출하여 중복 제거 후 반환한다.
    """
    print(f"\n[1/4] 네이버 뉴스 최신 기사 URL {count}개 수집 중...")

    urls = set()
    headers = {"User-Agent": "Mozilla/5.0"}

    # ── 섹션별 뉴스 수집 (정치/경제/사회/생활문화/IT과학) ──
    sections = [
        ("정치", "https://news.naver.com/section/100"),
        ("경제", "https://news.naver.com/section/101"),
        ("사회", "https://news.naver.com/section/102"),
        ("생활", "https://news.naver.com/section/103"),
        ("IT",   "https://news.naver.com/section/105"),
    ]

    for name, section_url in sections:
        if len(urls) >= count:
            break
        try:
            resp = requests.get(section_url, headers=headers, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')

            # 네이버 뉴스 섹션 페이지의 기사 링크 패턴
            for a_tag in soup.select("a[href*='news.naver.com/mnews/article']"):
                href = a_tag.get("href", "")
                if href.startswith("http") and href not in urls:
                    urls.add(href)
                    if len(urls) >= count:
                        break

            # 패턴이 안 맞으면 다른 선택자도 시도
            if len(urls) < count:
                for a_tag in soup.select("a[href*='/article/']"):
                    href = a_tag.get("href", "")
                    if "news.naver.com" in href and href.startswith("http"):
                        urls.add(href)
                        if len(urls) >= count:
                            break

            print(f"  [{name}] {len(urls)}개 수집됨")
        except Exception as e:
            print(f"  [{name}] 수집 실패: {e}")

    url_list = list(urls)[:count]
    print(f"  총 {len(url_list)}개 URL 수집 완료")
    return url_list


def reset_db():
    """Docker 컨테이너 내부의 DB를 초기화한다.
    API 컨테이너에서 sqlite3 명령으로 테이블을 비운다.
    """
    print("  DB 초기화 중...")
    subprocess.run(
        ["docker", "compose", "exec", "-T", "api",
         "python3", "-c",
         "import sqlite3, os; "
         "db='/app/data/results.db'; "
         "conn=sqlite3.connect(db); "
         "conn.execute('DELETE FROM analysis_results'); "
         "conn.commit(); conn.close(); "
         "print('DB cleared')"],
        capture_output=True, text=True,
        cwd=PROJECT_ROOT
    )


def get_done_count():
    """API를 통해 현재 분석 완료된 기사 수를 조회한다."""
    try:
        resp = requests.get(f"{API_URL}/results", timeout=5)
        if resp.status_code == 200:
            results = resp.json()
            if isinstance(results, list):
                return len(results)
    except Exception:
        pass
    return 0


def start_workers(n):
    """Worker를 N개로 스케일링하여 시작한다."""
    print(f"  Worker {n}개 시작 중...")
    subprocess.run(
        ["docker", "compose", "up", "-d", "--scale", f"worker={n}", "--no-recreate", "worker"],
        capture_output=True, text=True,
        cwd=PROJECT_ROOT
    )
    # Worker가 모델 로딩을 완료할 때까지 대기
    time.sleep(5)


def stop_workers():
    """모든 Worker를 정지한다."""
    print("  Worker 정지 중...")
    subprocess.run(
        ["docker", "compose", "stop", "worker"],
        capture_output=True, text=True,
        cwd=PROJECT_ROOT
    )
    # 완전히 정지될 때까지 잠시 대기
    time.sleep(2)


def send_bulk_urls(urls):
    """API의 /analyze/bulk 엔드포인트로 URL 리스트를 전송한다."""
    resp = requests.post(
        f"{API_URL}/analyze/bulk",
        json={"urls": urls},
        timeout=30
    )
    return resp.json()


def wait_for_completion(target_count, timeout=TIMEOUT):
    """모든 기사의 분석이 완료될 때까지 폴링으로 대기한다.

    Args:
        target_count: 완료 목표 건수
        timeout: 최대 대기 시간 (초)
    Returns:
        실제 소요 시간 (초)
    """
    start_time = time.time()
    while True:
        elapsed = time.time() - start_time
        done = get_done_count()
        # 진행 상황 출력 (같은 줄에 덮어쓰기)
        sys.stdout.write(f"\r  진행: {done}/{target_count} ({elapsed:.0f}초 경과)")
        sys.stdout.flush()

        if done >= target_count:
            print()  # 줄바꿈
            return elapsed

        if elapsed > timeout:
            print(f"\n  [경고] 타임아웃 ({timeout}초) — {done}/{target_count}건 완료")
            return elapsed

        time.sleep(POLL_INTERVAL)


def run_benchmark(urls, worker_count):
    """특정 Worker 수로 벤치마크를 실행한다.

    Args:
        urls: 분석할 URL 리스트
        worker_count: Worker 수
    Returns:
        dict: {workers, total_time, avg_time, articles}
    """
    print(f"\n{'='*50}")
    print(f"[벤치마크] Worker {worker_count}개 — {len(urls)}개 기사")
    print(f"{'='*50}")

    # 1) DB 초기화
    reset_db()

    # 2) Worker 시작
    stop_workers()
    start_workers(worker_count)

    # 3) URL 전송
    print(f"  {len(urls)}개 URL 전송 중...")
    result = send_bulk_urls(urls)
    queued = result.get("count", 0)
    print(f"  큐에 {queued}개 전송 완료")

    # 4) 완료 대기 및 시간 측정
    total_time = wait_for_completion(len(urls))

    # 5) Worker 정지
    stop_workers()

    avg_time = total_time / len(urls) if urls else 0
    # 1000개 처리 예상 시간 (분 단위)
    est_1000 = (avg_time * 1000) / 60

    print(f"  결과: 총 {total_time:.1f}초 | 기사당 {avg_time:.1f}초 | 1000개 예상 {est_1000:.1f}분")

    return {
        "workers": worker_count,
        "total_time": round(total_time, 1),
        "avg_time": round(avg_time, 1),
        "articles": len(urls),
        "est_1000_min": round(est_1000, 1)
    }


def print_summary(results):
    """벤치마크 결과를 표 형태로 출력한다."""
    print(f"\n{'='*60}")
    print("Worker 스케일링 성능 비교 결과")
    print(f"{'='*60}")
    print(f"{'Worker 수':>10} | {'총 처리 시간':>12} | {'기사당 평균':>10} | {'1000개 예상':>12}")
    print(f"{'-'*10}-+-{'-'*12}-+-{'-'*10}-+-{'-'*12}")
    for r in results:
        print(f"{r['workers']:>8}개 | {r['total_time']:>10.1f}초 | {r['avg_time']:>8.1f}초 | {r['est_1000_min']:>9.1f}분")
    print(f"{'='*60}")


def main():
    """벤치마크 메인 함수"""
    print("=" * 60)
    print("SSAK3 Worker 스케일링 성능 벤치마크")
    print("=" * 60)

    # ── 사전 체크: Docker 서비스 실행 확인 ──
    try:
        resp = requests.get(f"{API_URL}/", timeout=5)
        print(f"API 서버 상태: {resp.json().get('status', 'unknown')}")
    except Exception:
        print("[오류] API 서버에 연결할 수 없습니다.")
        print("먼저 실행해주세요: docker compose up -d rabbitmq api dashboard")
        sys.exit(1)

    # 1) 뉴스 URL 수집
    urls = collect_naver_news_urls(NUM_ARTICLES)
    if len(urls) < NUM_ARTICLES:
        print(f"[경고] {NUM_ARTICLES}개 목표 중 {len(urls)}개만 수집됨")

    # 2) 각 Worker 수별 벤치마크 실행
    results = []
    for wc in WORKER_COUNTS:
        result = run_benchmark(urls, wc)
        results.append(result)

    # 3) 결과 요약 출력
    print_summary(results)

    # 4) 결과를 JSON 파일로 저장 (대시보드에서 시각화용)
    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "num_articles": len(urls),
        "urls": urls,
        "results": results
    }

    output_path = os.path.join(PROJECT_ROOT, RESULT_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {output_path}")

    # Docker 볼륨에도 복사 (대시보드 컨테이너에서 접근용)
    subprocess.run(
        ["docker", "compose", "cp",
         output_path, "dashboard:/app/data/benchmark_results.json"],
        capture_output=True, text=True,
        cwd=PROJECT_ROOT
    )
    print("대시보드 컨테이너에 결과 복사 완료")
    print("\n대시보드 사이드바에서 '성능 측정' 메뉴를 확인하세요.")


if __name__ == "__main__":
    main()
