"""
M2 — 사용자 피드백을 회귀 평가 케이스로 자동 변환

작동:
1. DB의 feedback 테이블에서 사용자가 신고한 오류 케이스 수집
2. 분석 결과(analysis_results)와 조인하여 제목/본문/예상 등급 추출
3. eval/feedback_cases.py 파일로 저장 (eval_dataset 형식)
4. eval_runner는 기존 케이스 + 피드백 케이스를 합쳐 회귀 평가

[예상 등급 추론]
사용자가 rating + category로 피드백 → 시스템 등급과 사용자 의도 등급 매핑:
- category="accurate" → 시스템 등급 = 예상 등급 (positive 케이스)
- category="false_positive" (정상인데 의심) → 예상 등급 = "신뢰 가능"
- category="false_negative" (자극인데 신뢰) → 예상 등급 = "의심 기사"
- 그 외 카테고리 + rating ≤ 2 → 예상 등급 = 시스템 등급 ±1 (자동 추론)
"""

import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 카테고리 → 예상 등급 매핑
CATEGORY_TO_EXPECTED = {
    "accurate": None,  # 시스템 등급 그대로 정답으로 인정
    "false_positive": "신뢰 가능",  # 정상인데 의심으로 분류된 케이스 → 정답은 신뢰
    "false_negative": "의심 기사",  # 자극인데 신뢰로 분류 → 정답은 의심
    "keyword_miss": None,  # 키워드 누락은 점수 영향만, 등급 라벨 명확치 않음
    "source_unknown": None,
    "provocative_miss": "의심 기사",  # 자극성 누락 → 실제론 자극 분류돼야 함
    "ux_issue": None,  # UX는 평가 케이스에서 제외
    "other": None,
}


def fetch_feedback_with_results():
    """SQLite (API 컨테이너 내부) → feedback ⋈ analysis_results JOIN."""
    py_code = """
import sqlite3, json
conn = sqlite3.connect('/app/data/results.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
cursor.execute('''
    SELECT
        f.id AS feedback_id,
        f.rating, f.category, f.comment, f.created_at,
        r.id AS result_id, r.url, r.title, r.body,
        r.content_score, r.provocative_score, r.source_score,
        r.total_score, r.grade, r.source_name
    FROM feedback f
    JOIN analysis_results r ON f.result_id = r.id
    ORDER BY f.created_at DESC
''')
rows = [dict(r) for r in cursor.fetchall()]
conn.close()
print(json.dumps(rows, ensure_ascii=False))
"""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python3", "-c", py_code],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
    )
    if result.returncode != 0:
        print(f"[!] DB 조회 실패: {result.stderr}", file=sys.stderr)
        return []
    # stdout 마지막 JSON 라인
    lines = [l for l in result.stdout.strip().split("\n") if l.startswith("[")]
    return json.loads(lines[-1]) if lines else []


def feedback_to_eval_case(fb):
    """피드백 1건 → 평가 케이스 dict 변환. None 반환 시 변환 불가."""
    category = fb.get("category") or ""
    rating = fb.get("rating", 3)
    system_grade = fb.get("grade", "주의 필요")

    # 예상 등급 결정
    expected = CATEGORY_TO_EXPECTED.get(category)
    if expected is None and rating in (4, 5) and category in ("", "accurate"):
        # 사용자가 만족(4~5)했고 카테고리 없으면 → 시스템 등급 = 정답
        expected = system_grade
    if expected is None and rating in (1, 2):
        # 사용자가 강한 불만족(1~2)이지만 카테고리 미선택 → 시스템 등급 ±1
        # 보수적으로: 시스템이 신뢰 가능이면 의심, 의심이면 신뢰로 (반대)
        grade_flip = {
            "신뢰 가능": "주의 필요",
            "주의 필요": "의심 기사",
            "의심 기사": "주의 필요",
            "신뢰 낮음": "의심 기사",
        }
        expected = grade_flip.get(system_grade, system_grade)
    if expected is None:
        return None  # 라벨 명확치 않음 → 평가 케이스에서 제외

    # 본문 일부만 — DB에서 처음 500자만 저장됐을 수 있음
    body = (fb.get("body") or "")[:1500]
    return {
        "id": f"FB{fb['feedback_id']}",
        "category": "feedback",
        "expected_grade": expected,
        "title": fb["title"] or "",
        "body": body,
        "source_name": (fb.get("source_name") or "").split("|")[0],
        "user_rating": rating,
        "user_category": category,
        "user_comment": (fb.get("comment") or "")[:200],
    }


def main():
    print("=== M2: 사용자 피드백 → 회귀 평가 케이스 변환 ===\n")
    rows = fetch_feedback_with_results()
    print(f"수집된 피드백: {len(rows)}건")

    cases = []
    for fb in rows:
        case = feedback_to_eval_case(fb)
        if case:
            cases.append(case)

    print(f"변환된 평가 케이스: {len(cases)}건 (라벨 불명확한 케이스 제외)\n")

    # 카테고리 분포 출력
    from collections import Counter
    cat_counts = Counter(c["expected_grade"] for c in cases)
    for grade, n in cat_counts.most_common():
        print(f"  예상 등급 [{grade}]: {n}건")
    print()

    # 파일 저장 (eval_dataset과 호환되는 Python 모듈 형식)
    out_path = PROJECT_ROOT / "eval" / "feedback_cases.py"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('"""\n')
        f.write(f'자동 생성된 피드백 평가 케이스 (M2)\n')
        f.write(f'생성 시각: {datetime.now().isoformat(timespec="seconds")}\n')
        f.write(f'케이스 수: {len(cases)}\n')
        f.write('이 파일은 feedback_to_cases.py가 자동 생성합니다 — 직접 편집 금지\n')
        f.write('"""\n\n')
        f.write('FEEDBACK_CASES = ')
        json.dump(cases, f, ensure_ascii=False, indent=2)
        f.write('\n')

    print(f"저장 완료: {out_path}")
    print(f"\n다음 eval_runner.py 실행 시 자동으로 회귀 평가에 포함됨.")


if __name__ == "__main__":
    main()
