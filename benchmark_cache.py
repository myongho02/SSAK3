#!/usr/bin/env python3
"""캐시 효과 정량 측정 벤치마크 — Cold vs Warm 비교

Round 1 (Cold): docker compose restart로 LRU 캐시 완전 초기화 후 1,483건 처리
Round 2 (Warm): DB만 reset (worker LRU 캐시는 메모리에 살아있음) 1,483건 재처리
→ T_cold / T_warm = 캐시 가속 배수

사용:
    python3 benchmark_cache.py --urls news_urls_5k.json --workers 3
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


def dc(*args):
    return subprocess.run(["docker", "compose", *args], capture_output=True, text=True)


def get_running():
    r = dc("ps", "--services", "--filter", "status=running")
    return [s.strip() for s in r.stdout.splitlines() if s.strip() in ALL_WORKERS]


def stop_workers():
    print("  [Worker 정지]")
    dc("stop", "--timeout", "30", *ALL_WORKERS)
    deadline = time.time() + 20
    while time.time() < deadline:
        if not get_running():
            return
        time.sleep(1)
    dc("kill", *ALL_WORKERS)


def restart_workers(n):
    """worker를 완전 재시작 (메모리 캐시 초기화 보장)."""
    targets = ALL_WORKERS[:n]
    print(f"  [Worker 완전 재시작 × {n}] {targets}")
    dc("restart", *targets)
    deadline = time.time() + 60
    while time.time() < deadline:
        if set(get_running()) == set(targets):
            time.sleep(10)  # 모델 로딩
            return
        time.sleep(2)
    time.sleep(10)


def start_workers(n):
    """worker N개만 시작 (메모리 캐시 유지)."""
    targets = ALL_WORKERS[:n]
    print(f"  [Worker 시작 × {n}] {targets}")
    dc("start", *targets)
    deadline = time.time() + 30
    while time.time() < deadline:
        if set(get_running()) == set(targets):
            time.sleep(8)
            return
        time.sleep(1)
    time.sleep(8)


def reset_db():
    dc("exec", "-T", "api", "python3", "-c",
       "import sqlite3; "
       "conn=sqlite3.connect('/app/data/results.db'); "
       "conn.execute('DELETE FROM analysis_results'); "
       "conn.execute('DELETE FROM jobs'); "
       "conn.commit(); conn.close()")


def purge_queue():
    dc("exec", "-T", "rabbitmq", "rabbitmqctl", "purge_queue", "news_queue")


def get_cache_stats():
    """worker가 들고 있는 LRU 캐시 통계를 직접 조회 (API 통해서가 아니라 DB의 가장 최근 cache_stats)."""
    r = dc("exec", "-T", "api", "python3", "-c",
           "import sqlite3, json; "
           "conn=sqlite3.connect('/app/data/results.db'); "
           "cur=conn.cursor(); "
           "cur.execute('SELECT cache_stats FROM analysis_results ORDER BY id DESC LIMIT 1'); "
           "row=cur.fetchone(); "
           "print(row[0] if row and row[0] else '{}')")
    try:
        return json.loads(r.stdout.strip().split('\n')[-1])
    except Exception:
        return {}


def send_chunked(urls):
    queued = 0
    for i in range(0, len(urls), MAX_BULK):
        chunk = urls[i:i + MAX_BULK]
        try:
            r = requests.post(f"{API_URL}/analyze/bulk", json={"urls": chunk}, timeout=60)
            queued += r.json().get("count", 0)
        except Exception as e:
            print(f"  [전송 오류] {e}")
    return queued


def wait_complete(target, timeout=3600):
    start = time.time()
    last = 0
    while True:
        elapsed = time.time() - start
        try:
            s = requests.get(f"{API_URL}/jobs/summary", timeout=5).json()
        except Exception:
            s = {}
        done = s.get("done", 0) + s.get("failed", 0)
        if elapsed - last >= 20 or done >= target:
            rate = done / elapsed if elapsed > 0 else 0
            print(f"    [{elapsed:6.0f}s] {done}/{target} ({done/target*100:5.1f}%) | "
                  f"{rate:5.2f}/s | pending={s.get('pending',0)} failed={s.get('failed',0)}")
            last = elapsed
        if done >= target or elapsed > timeout:
            return elapsed, s
        time.sleep(3)


def run_round(urls, workers, fresh_cache, label):
    print(f"\n{'='*60}")
    print(f"[{label}] {len(urls)}건 (worker={workers}, fresh_cache={fresh_cache})")
    print(f"{'='*60}")

    stop_workers()
    reset_db()
    purge_queue()

    if fresh_cache:
        # 완전 재시작으로 LRU 캐시 초기화
        restart_workers(workers)
    else:
        # 그냥 start (캐시 메모리 유지)
        start_workers(workers)

    print(f"  [전송] {len(urls)}건")
    t0 = time.time()
    queued = send_chunked(urls)
    send_dt = time.time() - t0
    print(f"  [전송 완료] {queued}건 ({send_dt:.1f}초)")

    elapsed, summary = wait_complete(queued)
    total_time = time.time() - t0
    rate = queued / elapsed if elapsed > 0 else 0

    # 캐시 통계
    cs = get_cache_stats()

    print(f"\n  [{label} 결과]")
    print(f"    처리 시간: {elapsed:.1f}초 | 처리량: {rate:.2f} 건/s | 완료/실패: {summary.get('done',0)}/{summary.get('failed',0)}")
    if cs:
        for k in ("nli", "sentiment", "source"):
            if k in cs:
                print(f"    {k} 캐시: {cs[k].get('hit_rate_pct', '?')}% (hits={cs[k].get('hits')}, misses={cs[k].get('misses')})")

    return {
        "label": label,
        "fresh_cache": fresh_cache,
        "queued": queued,
        "done": summary.get("done", 0),
        "failed": summary.get("failed", 0),
        "send_time": round(send_dt, 1),
        "process_time": round(elapsed, 1),
        "throughput_per_sec": round(rate, 2),
        "cache_stats_final": cs,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls", required=True)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--out", default="benchmark_cache.json")
    args = p.parse_args()

    with open(args.urls, "r", encoding="utf-8") as f:
        data = json.load(f)
    urls = data["urls"]
    print(f"[로드] {len(urls)}건")

    try:
        requests.get(f"{API_URL}/", timeout=5).json()
    except Exception:
        sys.exit("[오류] API 연결 실패")

    # Round 1: Cold (캐시 완전 초기화)
    r1 = run_round(urls, args.workers, fresh_cache=True, label="Round 1 (Cold Cache)")

    # Round 2: Warm (캐시 유지, DB만 reset)
    r2 = run_round(urls, args.workers, fresh_cache=False, label="Round 2 (Warm Cache)")

    stop_workers()

    # 비교
    print(f"\n{'='*70}")
    print(f"Cold vs Warm Cache 비교")
    print(f"{'='*70}")
    print(f"{'단계':<25} {'처리시간':>10} {'처리량':>12} {'완료':>6}")
    print("-" * 70)
    for r in (r1, r2):
        print(f"{r['label']:<25} {r['process_time']:>8.1f}초 "
              f"{r['throughput_per_sec']:>8.2f} 건/s {r['done']:>6}")
    speedup = r1['process_time'] / r2['process_time'] if r2['process_time'] > 0 else 0
    print(f"\n  → 캐시 가속: {speedup:.2f}배 (Cold {r1['process_time']:.0f}초 → Warm {r2['process_time']:.0f}초)")

    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url_count": len(urls),
        "workers": args.workers,
        "round_1_cold": r1,
        "round_2_warm": r2,
        "cache_speedup": round(speedup, 2),
        "total_messages_processed": r1['done'] + r2['done'],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n[저장] {args.out}")


if __name__ == "__main__":
    main()
