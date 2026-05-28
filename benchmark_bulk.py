#!/usr/bin/env python3
"""대량 처리량 벤치마크 v2 — worker stop 검증 + 각 단계 결과 백업

v2 개선사항:
- stop_workers: timeout 30초 + docker ps 검증으로 완전 종료 확인
- 각 단계 사이에 docker compose restart로 fresh state (메모리 캐시 초기화)
- 각 worker 단계 종료 시 DB 결과를 별도 JSON에 백업 (덮어쓰기 전)
- worker별 분배·처리 시간을 단계별로 기록

사용:
    python3 benchmark_bulk.py --urls news_urls_1200.json --count 1000 --workers 1,2,3
"""

import argparse
import json
import subprocess
import sys
import time
import requests
from pathlib import Path

API_URL = "http://localhost:5001"
MAX_BULK = 100
ALL_WORKERS = ["worker-1", "worker-2", "worker-3"]


def docker_compose(*args, capture=True):
    return subprocess.run(["docker", "compose", *args],
                          capture_output=capture, text=True)


def query_db_json(sql):
    """API 컨테이너 sqlite DB에 SQL 실행 후 JSON 반환."""
    result = docker_compose("exec", "-T", "api", "python3", "-c",
                            f"import sqlite3, json; "
                            f"conn=sqlite3.connect('/app/data/results.db'); "
                            f"conn.row_factory=sqlite3.Row; "
                            f"cur=conn.cursor(); cur.execute({sql!r}); "
                            f"rows=[dict(r) for r in cur.fetchall()]; "
                            f"print(json.dumps(rows, ensure_ascii=False, default=str))")
    out = result.stdout.strip()
    if not out:
        return []
    try:
        return json.loads(out.split('\n')[-1])
    except Exception as e:
        print(f"  [DB 파싱 오류] {e}", file=sys.stderr)
        return []


def reset_db():
    print("  [DB 초기화]")
    docker_compose("exec", "-T", "api", "python3", "-c",
                   "import sqlite3; "
                   "conn=sqlite3.connect('/app/data/results.db'); "
                   "conn.execute('DELETE FROM analysis_results'); "
                   "conn.execute('DELETE FROM jobs'); "
                   "conn.commit(); conn.close(); print('cleared')")


def purge_queue():
    print("  [큐 초기화]")
    docker_compose("exec", "-T", "rabbitmq", "rabbitmqctl", "purge_queue", "news_queue")


def get_running_workers():
    """현재 떠 있는 worker 컨테이너 이름 리스트."""
    r = docker_compose("ps", "--services", "--filter", "status=running")
    if r.returncode != 0:
        return []
    services = [s.strip() for s in r.stdout.splitlines() if s.strip()]
    return [s for s in services if s in ALL_WORKERS]


def stop_all_workers_verified():
    """모든 worker를 정지하고 정말 멈췄는지 검증한다."""
    print("  [모든 Worker 정지 중 (timeout 30)]")
    docker_compose("stop", "--timeout", "30", *ALL_WORKERS)
    # 검증 — 최대 20초 대기
    deadline = time.time() + 20
    while time.time() < deadline:
        running = get_running_workers()
        if not running:
            print(f"  [Worker 모두 정지 확인]")
            return True
        time.sleep(1)
    running = get_running_workers()
    print(f"  [경고] Worker 정지 미완료: {running}")
    # 강제 kill
    if running:
        print(f"  [강제 kill]")
        docker_compose("kill", *running)
        time.sleep(2)
    return False


def start_workers_verified(n):
    """worker-1 ~ worker-N 만 띄우고 그 N개가 정말 떠 있는지 검증."""
    targets = ALL_WORKERS[:n]
    print(f"  [Worker 시작 × {n}] {', '.join(targets)}")
    docker_compose("start", *targets)
    # 모델 로딩 대기 (워커가 RabbitMQ에 consume 등록할 때까지)
    deadline = time.time() + 30
    while time.time() < deadline:
        running = get_running_workers()
        if set(running) == set(targets):
            print(f"  [Worker {n}개 가동 확인]")
            # 모델 로드 + consume 등록까지 추가 대기
            time.sleep(8)
            return True
        time.sleep(1)
    print(f"  [경고] 예상한 {targets} ≠ 실제 {get_running_workers()}")
    time.sleep(8)
    return False


def send_chunked(urls, chunk_size=MAX_BULK):
    queued_total = 0
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i:i + chunk_size]
        try:
            resp = requests.post(f"{API_URL}/analyze/bulk",
                                 json={"urls": chunk}, timeout=60)
            data = resp.json()
            queued_total += data.get("count", 0)
        except Exception as e:
            print(f"  [전송 오류] {e}")
    return queued_total


def get_summary():
    try:
        return requests.get(f"{API_URL}/jobs/summary", timeout=5).json()
    except Exception:
        return {}


def wait_for_completion(target, timeout=3600):
    start = time.time()
    last_print = 0
    while True:
        elapsed = time.time() - start
        s = get_summary()
        done = s.get("done", 0) + s.get("failed", 0)
        if elapsed - last_print >= 30 or done >= target:
            pct = (done / target * 100) if target else 0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (target - done) / rate if rate > 0 else 0
            print(f"    [{elapsed:6.0f}s] {done}/{target} ({pct:5.1f}%) | "
                  f"속도 {rate:5.2f}/s | ETA {eta:6.0f}s | "
                  f"pending={s.get('pending',0)} processing={s.get('processing',0)} "
                  f"failed={s.get('failed',0)}")
            last_print = elapsed
        if done >= target:
            return elapsed, s
        if elapsed > timeout:
            print(f"  [타임아웃 {timeout}초]")
            return elapsed, s
        time.sleep(5)


def snapshot_stage_results(stage_name, output_dir):
    """현재 DB 상태를 단계별 JSON 파일로 백업 (덮어쓰기 전)."""
    print(f"  [단계 결과 백업 → {stage_name}]")
    results = query_db_json("SELECT url, title, total_score, grade, source_name, "
                            "worker_id, processing_time, status, cache_stats "
                            "FROM analysis_results")
    jobs = query_db_json("SELECT url, status, error_message FROM jobs")
    path = output_dir / f"stage_{stage_name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"results": results, "jobs": jobs}, f, ensure_ascii=False, indent=2)
    return path


def run_one(urls, worker_count, output_dir, stage_idx):
    print(f"\n{'='*60}")
    print(f"[단계 {stage_idx}] Worker {worker_count}개 × {len(urls)}건")
    print(f"{'='*60}")

    # 1) 모든 worker 완전 정지
    stop_all_workers_verified()

    # 2) DB + 큐 초기화
    reset_db()
    purge_queue()

    # 3) 지정한 worker만 시작
    start_workers_verified(worker_count)

    # 4) URL 전송
    print(f"  [URL 전송] {len(urls)}건 분할 전송")
    t0 = time.time()
    queued = send_chunked(urls)
    send_dt = time.time() - t0
    print(f"  [전송 완료] {queued}건 큐 적재 ({send_dt:.1f}초)")
    if queued == 0:
        return None

    # 5) 완료 대기
    elapsed, summary = wait_for_completion(queued)
    total_time = time.time() - t0

    # 6) 단계 결과 백업 (DB 덮어쓰기 전!)
    snapshot_stage_results(f"w{worker_count}", output_dir)

    # 7) 워커별 분배 통계
    worker_dist = query_db_json("SELECT worker_id, COUNT(*) as n, "
                                "AVG(processing_time) as avg_t "
                                "FROM analysis_results "
                                "WHERE worker_id IS NOT NULL "
                                "GROUP BY worker_id")
    print(f"  [Worker 분배]")
    for w in worker_dist:
        print(f"    {w['worker_id']}: {w['n']}건 (평균 {w['avg_t']:.2f}초)")

    # 8) 정지
    stop_all_workers_verified()

    rate = queued / elapsed if elapsed > 0 else 0
    avg = elapsed / queued if queued else 0

    return {
        "workers": worker_count,
        "urls_sent": len(urls),
        "queued": queued,
        "done": summary.get("done", 0),
        "failed": summary.get("failed", 0),
        "send_time": round(send_dt, 1),
        "process_time": round(elapsed, 1),
        "total_time": round(total_time, 1),
        "throughput_per_sec": round(rate, 2),
        "avg_per_article": round(avg, 2),
        "est_10000_min": round(avg * 10000 / 60, 1),
        "worker_distribution": [{"worker_id": w["worker_id"],
                                 "count": w["n"],
                                 "avg_time": round(w["avg_t"], 2) if w["avg_t"] else None}
                                for w in worker_dist],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls", required=True)
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--workers", default="1,2,3")
    p.add_argument("--out", default="benchmark_bulk_results.json")
    p.add_argument("--outdir", default="bench_stages",
                   help="단계별 백업 JSON 저장 디렉토리")
    args = p.parse_args()

    output_dir = Path(args.outdir)
    output_dir.mkdir(exist_ok=True)

    with open(args.urls, "r", encoding="utf-8") as f:
        data = json.load(f)
    urls = data["urls"][:args.count]
    print(f"\n[로드] {args.urls} 에서 {len(urls)}건 사용")

    worker_list = [int(x) for x in args.workers.split(",")]

    try:
        r = requests.get(f"{API_URL}/", timeout=5)
        print(f"[API] {r.json().get('status', 'unknown')}")
    except Exception:
        print("[오류] API 서버 연결 실패")
        sys.exit(1)

    results = []
    for i, wc in enumerate(worker_list, 1):
        r = run_one(urls, wc, output_dir, i)
        if r:
            results.append(r)

    # 요약
    print(f"\n{'='*70}")
    print(f"종합 결과 ({len(urls)}건)")
    print(f"{'='*70}")
    print(f"{'Worker':>7} | {'처리시간':>9} | {'처리량':>10} | {'실패':>5} | {'10k예상':>10}")
    print("-" * 70)
    for r in results:
        print(f"{r['workers']:>5}개 | {r['process_time']:>7.1f}초 | "
              f"{r['throughput_per_sec']:>6.2f} 건/s | "
              f"{r['failed']:>4} | {r['est_10000_min']:>7.1f}분")

    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url_count": len(urls),
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n[저장] {args.out}")
    print(f"[단계별 백업] {output_dir}/stage_w*.json")


if __name__ == "__main__":
    main()
