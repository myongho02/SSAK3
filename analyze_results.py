#!/usr/bin/env python3
"""대량 실험 결과 분석기 — 분야별 × 등급별 × 오분류 분석

DB(analysis_results, jobs)와 collect_news_bulk.py가 만든 url_categories를
조인하여 학회 발표용 통계를 생성한다.

생성 항목:
  1. 분야별 등급 분포 (교차표)
  2. 도메인별 실패율
  3. 점수 outlier (대형 매체인데 낮은 등급 받은 케이스)
  4. 처리 시간 / worker_id 분포
  5. 캐시 적중 통계
  6. 오분류 추정 케이스 — 학회 발표 "한계 및 개선 과정" 슬라이드용

사용:
    python3 analyze_results.py --urls news_urls_1200.json --out analysis_report.json
"""

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from urllib.parse import urlparse


def query_db(sql):
    """API 컨테이너 안의 sqlite DB에 SQL 실행. 결과는 \t 구분 텍스트."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "api",
         "python3", "-c",
         f"import sqlite3, json; "
         f"conn=sqlite3.connect('/app/data/results.db'); "
         f"conn.row_factory=sqlite3.Row; "
         f"cur=conn.cursor(); cur.execute({sql!r}); "
         f"rows=[dict(r) for r in cur.fetchall()]; "
         f"print(json.dumps(rows, ensure_ascii=False, default=str))"],
        capture_output=True, text=True
    )
    out = result.stdout.strip()
    if not out:
        return []
    try:
        return json.loads(out.split('\n')[-1])
    except Exception as e:
        print(f"[DB 파싱 오류] {e}\n원본: {out[:500]}", file=sys.stderr)
        return []


def domain_of(url):
    try:
        return (urlparse(url).hostname or "unknown").replace("www.", "")
    except Exception:
        return "unknown"


def analyze(urls_file):
    # ── 1) URL → category 매핑 로드 ──
    with open(urls_file, "r", encoding="utf-8") as f:
        url_meta = json.load(f)
    url_category = url_meta.get("url_categories", {})

    # ── 2) DB에서 결과 + 작업 로그 가져오기 ──
    print("[DB 조회 중]")
    results = query_db("SELECT url, title, total_score, content_score, "
                       "provocative_score, source_score, grade, source_name, "
                       "worker_id, processing_time, status, cache_stats, "
                       "analyzed_at FROM analysis_results")
    jobs = query_db("SELECT url, status, error_message, queued_at, finished_at "
                    "FROM jobs")

    print(f"  analysis_results: {len(results)}건")
    print(f"  jobs: {len(jobs)}건")

    # URL → result 매핑
    res_by_url = {r["url"]: r for r in results}
    job_by_url = {j["url"]: j for j in jobs}

    # ── 3) 기본 통계 ──
    total_jobs = len(jobs)
    done_count = sum(1 for j in jobs if j["status"] == "done")
    failed_count = sum(1 for j in jobs if j["status"] == "failed")

    print(f"\n[전체 통계]")
    print(f"  총 작업: {total_jobs}")
    print(f"  완료: {done_count} ({done_count/total_jobs*100:.1f}%)")
    print(f"  실패: {failed_count} ({failed_count/total_jobs*100:.1f}%)")

    # ── 4) 등급 분포 ──
    grade_count = Counter()
    for r in results:
        if r.get("grade"):
            grade_count[r["grade"]] += 1

    print(f"\n[등급 분포]")
    for g, n in sorted(grade_count.items(), key=lambda x: -x[1]):
        print(f"  {g:>12}: {n:>4}건 ({n/done_count*100:5.1f}%)")

    # ── 5) 분야 × 등급 교차표 ──
    cat_grade = defaultdict(lambda: Counter())
    for r in results:
        cat = url_category.get(r["url"], "미상")
        g = r.get("grade") or "미상"
        cat_grade[cat][g] += 1

    print(f"\n[분야 × 등급 교차표]")
    all_grades = sorted(grade_count.keys())
    header = "  " + " " * 12 + " | " + " | ".join(f"{g:>10}" for g in all_grades) + " | {:>6}".format("합계")
    print(header)
    print("  " + "-" * (12 + 3 + len(all_grades) * 13 + 9))
    cat_totals = {}
    for cat in sorted(cat_grade.keys(), key=lambda c: -sum(cat_grade[c].values())):
        row = f"  {cat:>12} | " + " | ".join(f"{cat_grade[cat][g]:>10}" for g in all_grades)
        total = sum(cat_grade[cat].values())
        cat_totals[cat] = total
        print(row + f" | {total:>6}")

    # 분야별 평균 점수
    cat_scores = defaultdict(list)
    for r in results:
        cat = url_category.get(r["url"], "미상")
        if r.get("total_score") is not None:
            cat_scores[cat].append(r["total_score"])

    print(f"\n[분야별 평균 신뢰도 점수]")
    for cat in sorted(cat_scores.keys(), key=lambda c: -sum(cat_scores[c])/len(cat_scores[c]) if cat_scores[c] else 0):
        scores = cat_scores[cat]
        if scores:
            avg = sum(scores) / len(scores)
            print(f"  {cat:>12}: 평균 {avg:5.1f}점 (n={len(scores)})")

    # ── 6) 도메인별 실패율 ──
    dom_total = Counter()
    dom_failed = Counter()
    for j in jobs:
        d = domain_of(j["url"])
        dom_total[d] += 1
        if j["status"] == "failed":
            dom_failed[d] += 1

    print(f"\n[도메인별 처리 결과]")
    print(f"  {'도메인':>30} | {'총건':>5} | {'실패':>5} | {'실패율':>7}")
    for d in sorted(dom_total.keys(), key=lambda x: -dom_total[x]):
        total = dom_total[d]
        failed = dom_failed[d]
        rate = failed / total * 100 if total else 0
        marker = " ★" if rate > 20 else ""
        print(f"  {d:>30} | {total:>5} | {failed:>5} | {rate:>6.1f}%{marker}")

    # ── 7) 처리 시간 통계 ──
    times = [r["processing_time"] for r in results if r.get("processing_time")]
    if times:
        times_sorted = sorted(times)
        n = len(times)
        avg = sum(times) / n
        median = times_sorted[n // 2]
        p90 = times_sorted[int(n * 0.9)]
        p95 = times_sorted[int(n * 0.95)]
        mn, mx = min(times), max(times)
        print(f"\n[처리 시간 분포 (n={n})]")
        print(f"  평균: {avg:.2f}초 | 중앙값: {median:.2f}초")
        print(f"  P90: {p90:.2f}초 | P95: {p95:.2f}초")
        print(f"  최소: {mn:.2f}초 | 최대: {mx:.2f}초")

    # ── 8) Worker 분배 ──
    worker_count = Counter()
    worker_time = defaultdict(list)
    for r in results:
        w = r.get("worker_id") or "unknown"
        worker_count[w] += 1
        if r.get("processing_time"):
            worker_time[w].append(r["processing_time"])

    print(f"\n[Worker 분배]")
    total_w = sum(worker_count.values())
    for w in sorted(worker_count.keys()):
        cnt = worker_count[w]
        avg_t = sum(worker_time[w]) / len(worker_time[w]) if worker_time[w] else 0
        print(f"  {w:>15}: {cnt:>4}건 ({cnt/total_w*100:5.1f}%) | 평균 {avg_t:.2f}초")

    # ── 9) 캐시 통계 (worker.py의 hit_rate_pct 키 사용) ──
    cache_hits = {"nli": [], "sentiment": [], "source": []}
    for r in results:
        cs = r.get("cache_stats")
        if not cs:
            continue
        try:
            d = json.loads(cs) if isinstance(cs, str) else cs
            for k in ("nli", "sentiment", "source"):
                if k not in d:
                    continue
                # worker.py가 저장하는 key는 'hit_rate_pct' (이미 % 단위)
                pct = d[k].get("hit_rate_pct")
                if pct is None:
                    # fallback: hits / (hits + misses)
                    hits = d[k].get("hits", 0)
                    misses = d[k].get("misses", 0)
                    total = hits + misses
                    pct = (hits / total * 100) if total > 0 else 0.0
                cache_hits[k].append(pct)
        except Exception:
            pass

    print(f"\n[캐시 적중률]")
    for k, vals in cache_hits.items():
        if vals:
            avg = sum(vals) / len(vals)
            mx = max(vals)
            print(f"  {k:>10}: 평균 {avg:5.1f}% | 최대 {mx:5.1f}% (n={len(vals)})")

    # ── 10) 오분류 추정: 대형 통신사인데 낮은 등급 ──
    # 통신사/주류 매체에서 "의심 기사" 또는 "신뢰 낮음" 등급 받은 케이스를 추출
    suspect_grades = ("의심 기사", "신뢰 낮음")
    trusted_sources = ("yna.co.kr", "kbs.co.kr", "mbc.co.kr", "sbs.co.kr",
                       "ytn.co.kr", "jtbc.co.kr")
    misclass_candidates = []
    for r in results:
        d = domain_of(r["url"])
        if any(d.endswith(t) for t in trusted_sources):
            if r.get("grade") in suspect_grades:
                misclass_candidates.append({
                    "url": r["url"],
                    "title": (r.get("title") or "")[:80],
                    "domain": d,
                    "grade": r["grade"],
                    "total_score": r.get("total_score"),
                    "content_score": r.get("content_score"),
                    "provocative_score": r.get("provocative_score"),
                    "source_score": r.get("source_score"),
                })

    print(f"\n[오분류 추정 — 통신사인데 낮은 등급] {len(misclass_candidates)}건")
    for m in misclass_candidates[:10]:
        print(f"  [{m['domain']}/{m['grade']}/{m['total_score']:.1f}점] {m['title']}")

    # ── 11) 짧은 본문 / 추출 실패로 의심 ──
    short_body = []
    body_lens = []
    body_results = query_db("SELECT url, title, LENGTH(body) AS body_len, grade, "
                            "total_score FROM analysis_results")
    for r in body_results:
        body_lens.append(r["body_len"] or 0)
        if (r["body_len"] or 0) < 200:
            short_body.append(r)

    if body_lens:
        body_sorted = sorted(body_lens)
        n = len(body_sorted)
        print(f"\n[본문 길이 분포]")
        print(f"  평균: {sum(body_lens)/n:.0f}자 | 중앙값: {body_sorted[n//2]}자")
        print(f"  최소: {min(body_lens)}자 | 최대: {max(body_lens)}자")
        print(f"  200자 미만 (추출 실패 추정): {len(short_body)}건")

    # 결과 객체 반환
    return {
        "total_jobs": total_jobs,
        "done": done_count,
        "failed": failed_count,
        "failure_rate": round(failed_count / total_jobs * 100, 2) if total_jobs else 0,
        "grade_distribution": dict(grade_count),
        "category_grade_crosstab": {k: dict(v) for k, v in cat_grade.items()},
        "category_avg_scores": {k: round(sum(v)/len(v), 2) for k, v in cat_scores.items() if v},
        "domain_failure_rates": {d: {"total": dom_total[d], "failed": dom_failed[d],
                                     "rate": round(dom_failed[d]/dom_total[d]*100, 2) if dom_total[d] else 0}
                                 for d in dom_total},
        "processing_time": {
            "avg": round(sum(times)/len(times), 2) if times else None,
            "median": round(sorted(times)[len(times)//2], 2) if times else None,
            "p90": round(sorted(times)[int(len(times)*0.9)], 2) if times else None,
            "p95": round(sorted(times)[int(len(times)*0.95)], 2) if times else None,
            "min": round(min(times), 2) if times else None,
            "max": round(max(times), 2) if times else None,
        } if times else None,
        "worker_distribution": {w: {"count": worker_count[w],
                                    "ratio": round(worker_count[w]/total_w*100, 2),
                                    "avg_time": round(sum(worker_time[w])/len(worker_time[w]), 2) if worker_time[w] else None}
                                for w in worker_count},
        "cache_hits": {k: {"avg_pct": round(sum(v)/len(v), 2),
                           "max_pct": round(max(v), 2),
                           "n": len(v)}
                       for k, v in cache_hits.items() if v},
        "misclassification_candidates": misclass_candidates[:30],
        "body_length": {
            "avg": round(sum(body_lens)/len(body_lens), 1) if body_lens else None,
            "median": sorted(body_lens)[len(body_lens)//2] if body_lens else None,
            "short_body_count": len(short_body),
        } if body_lens else None,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls", required=True, help="news_urls_*.json (카테고리 메타)")
    p.add_argument("--out", default="analysis_report.json")
    args = p.parse_args()

    report = analyze(args.urls)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[저장] {args.out}")


if __name__ == "__main__":
    main()
