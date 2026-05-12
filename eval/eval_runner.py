"""
SSAK3 정확도 평가 실행기 (K1)

eval_dataset.py의 케이스를 Worker 컨테이너의 분석 함수에 직접 전달하여
실제 분류 결과를 측정한다.

[측정 지표]
- Accuracy: 예상 등급과 실제 등급이 일치하는 비율
- Loose Accuracy: 예상 등급과 실제 등급이 1단계 이내 차이인 비율 (인접 등급 인정)
- 카테고리별 정확도 (normal/sensational/mismatch/unknown_source)
- 각 케이스의 본문일치도/자극성/출처/종합 점수 표

[사용법]
    # SSAK3 가동 상태에서 (docker compose up -d)
    python3 eval/eval_runner.py

결과는 stdout에 표 형태 + eval/results.json에 raw 데이터로 저장.
"""

import json
import time
import sys
from pathlib import Path

# eval_dataset 모듈 로딩
sys.path.insert(0, str(Path(__file__).parent))
from eval_dataset import all_cases as base_cases, GRADE_RANK, grade_distance

# M2: 사용자 피드백 케이스도 자동 포함 (있을 때만)
try:
    from feedback_cases import FEEDBACK_CASES
except ImportError:
    FEEDBACK_CASES = []


def all_cases():
    """기본 평가 케이스 + 피드백 케이스를 합쳐 반환."""
    return list(base_cases()) + [
        {**c, "category": "feedback"} for c in FEEDBACK_CASES
    ]


def analyze_via_worker(title, body, source_name):
    """Worker의 분석 함수를 docker exec로 직접 호출.

    /analyze API를 거치면 크롤링이 필요한데, 평가용 합성 데이터는
    제목/본문/출처를 직접 주입해야 하므로 worker.py 함수를 직접 호출.
    """
    import subprocess
    # 컨테이너 내부에서 분석 함수 4종을 호출하고 결과를 JSON으로 출력
    py_code = f"""
import sys, json
sys.path.insert(0, '/app')
from worker import (
    analyze_content_similarity,
    analyze_provocative,
    analyze_source,
    calculate_total_score,
    get_grade,
)
title = {json.dumps(title)}
body = {json.dumps(body)}
source_name = {json.dumps(source_name)}

source_score, source_class = analyze_source('', source_name)
content_score, content_details = analyze_content_similarity(title, body)
provocative_score, provocative_details = analyze_provocative(title, body, source_score=source_score)
total = calculate_total_score(content_score, provocative_score, source_score)
grade = get_grade(total)

print(json.dumps({{
    'content_score': content_score,
    'provocative_score': provocative_score,
    'source_score': source_score,
    'source_class': source_class,
    'total_score': total,
    'grade': grade,
    'content_details': content_details,
    'provocative_details': provocative_details,
}}, ensure_ascii=False))
"""
    # docker-compose 프로젝트 경로 자동 감지 (worktree vs 메인)
    import os
    project_dir = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "worker-1", "python3", "-c", py_code],
        capture_output=True, text=True, cwd=str(project_dir), timeout=120,
    )
    if result.returncode != 0:
        return None, result.stderr
    # stdout 마지막 줄이 JSON
    lines = [l for l in result.stdout.strip().split("\n") if l.startswith("{")]
    if not lines:
        return None, f"JSON output 없음: {result.stdout}"
    try:
        return json.loads(lines[-1]), None
    except Exception as e:
        return None, f"JSON parse 실패: {e}\n{lines[-1][:200]}"


def main():
    cases = all_cases()
    print(f"=== SSAK3 정확도 평가 ({len(cases)}건) ===\n")

    results = []
    for case in cases:
        sys.stdout.write(f"[{case['id']}] {case['category']:<15} {case['title'][:40]:40} ... ")
        sys.stdout.flush()
        t0 = time.time()
        analysis, err = analyze_via_worker(case["title"], case["body"], case["source_name"])
        elapsed = time.time() - t0
        if err:
            print(f"❌ {err[:80]}")
            results.append({"case": case, "error": err[:200]})
            continue
        actual_grade = analysis["grade"]
        expected_grade = case["expected_grade"]
        dist = grade_distance(expected_grade, actual_grade)
        marker = "✅" if dist == 0 else ("△" if dist == 1 else "❌")
        print(f"{marker} 예상={expected_grade:6} → 실제={actual_grade:6} ({analysis['total_score']:.1f}점) [{elapsed:.1f}s]")
        results.append({"case": case, "analysis": analysis, "dist": dist})

    # ========== 정확도 집계 ==========
    print("\n" + "=" * 80)
    print("정확도 요약")
    print("=" * 80)

    valid = [r for r in results if "analysis" in r]
    if not valid:
        print("측정 가능한 케이스가 없습니다.")
        return

    exact = sum(1 for r in valid if r["dist"] == 0)
    loose = sum(1 for r in valid if r["dist"] <= 1)
    n = len(valid)
    print(f"  Strict Accuracy:  {exact}/{n} = {100*exact/n:.1f}% (예상 등급 정확 일치)")
    print(f"  Loose Accuracy:   {loose}/{n} = {100*loose/n:.1f}% (인접 등급 1칸 이내 인정)")

    # 카테고리별
    print("\n  카테고리별 Strict Accuracy:")
    categories = sorted({r["case"]["category"] for r in valid})
    for cat in categories:
        cat_results = [r for r in valid if r["case"]["category"] == cat]
        cat_exact = sum(1 for r in cat_results if r["dist"] == 0)
        cat_n = len(cat_results)
        print(f"    {cat:<18} {cat_exact}/{cat_n} = {100*cat_exact/cat_n:.1f}%")

    # ========== 케이스별 상세 표 ==========
    print("\n" + "=" * 80)
    print("케이스별 상세 점수")
    print("=" * 80)
    print(f"{'ID':4} {'카테고리':15} {'예상':10} {'실제':10} {'본문':6} {'자극':6} {'출처':6} {'종합':6} {'결과':4}")
    for r in valid:
        case = r["case"]
        a = r["analysis"]
        marker = "✅" if r["dist"] == 0 else ("△" if r["dist"] == 1 else "❌")
        print(f"{case['id']:4} {case['category']:15} "
              f"{case['expected_grade']:10} {a['grade']:10} "
              f"{a['content_score']:6.1f} {a['provocative_score']:6.1f} "
              f"{a['source_score']:6.1f} {a['total_score']:6.1f} {marker}")

    # ========== Raw 저장 ==========
    out_path = Path(__file__).parent / "results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "total": n,
            "strict_accuracy": exact / n,
            "loose_accuracy": loose / n,
            "results": [
                {
                    "id": r["case"]["id"],
                    "category": r["case"]["category"],
                    "title": r["case"]["title"],
                    "expected_grade": r["case"]["expected_grade"],
                    "actual_grade": r["analysis"]["grade"],
                    "scores": {
                        "content": r["analysis"]["content_score"],
                        "provocative": r["analysis"]["provocative_score"],
                        "source": r["analysis"]["source_score"],
                        "total": r["analysis"]["total_score"],
                    },
                    "dist": r["dist"],
                }
                for r in valid
            ]
        }, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
