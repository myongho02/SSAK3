"""
M6 — Confusion Matrix + Classification Report

eval_runner.py가 생성한 results.json을 읽어 학술 수준의 평가 지표 계산:
- Confusion Matrix (4×4 등급 매트릭스)
- Per-class precision / recall / F1
- Macro / Weighted average
- 시각화 (ASCII + 텍스트 표)

sklearn이 없어도 동작 — 직접 구현.

[사용법]
    python3 eval/eval_runner.py     # 먼저 평가 실행
    python3 eval/eval_report.py     # 그 다음 리포트
"""

import json
from pathlib import Path
from collections import defaultdict, Counter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = PROJECT_ROOT / "eval" / "results.json"

# 등급 순서 (혼동 행렬 행/열 순서)
GRADES = ["신뢰 가능", "주의 필요", "의심 기사", "신뢰 낮음"]


def load_results():
    if not RESULTS_PATH.exists():
        print(f"❌ {RESULTS_PATH} 가 없습니다. 먼저 eval_runner.py를 실행하세요.")
        return None
    with open(RESULTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def confusion_matrix(results):
    """4x4 혼동 행렬 — rows = expected, cols = actual."""
    matrix = {g: {h: 0 for h in GRADES} for g in GRADES}
    for r in results:
        exp = r["expected_grade"]
        act = r["actual_grade"]
        if exp in matrix and act in matrix[exp]:
            matrix[exp][act] += 1
    return matrix


def per_class_metrics(results):
    """등급별 Precision / Recall / F1 계산."""
    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)
    support = defaultdict(int)  # expected 등급별 케이스 수

    for r in results:
        exp = r["expected_grade"]
        act = r["actual_grade"]
        support[exp] += 1
        if exp == act:
            tp[exp] += 1
        else:
            fp[act] += 1   # 잘못 예측된 등급 입장에선 false positive
            fn[exp] += 1   # 실제 등급 입장에선 false negative

    metrics = {}
    for g in GRADES:
        p = tp[g] / (tp[g] + fp[g]) if (tp[g] + fp[g]) > 0 else 0.0
        r = tp[g] / (tp[g] + fn[g]) if (tp[g] + fn[g]) > 0 else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        metrics[g] = {
            "precision": p,
            "recall": r,
            "f1": f1,
            "support": support[g],
        }
    return metrics


def print_confusion_matrix(matrix):
    """ASCII 표로 혼동 행렬 출력."""
    print("\n" + "=" * 80)
    print("Confusion Matrix (rows=Expected, cols=Actual)")
    print("=" * 80)
    # 헤더
    col_width = max(len(g) for g in GRADES) + 2
    header = "Expected\\Actual".ljust(col_width)
    for g in GRADES:
        header += f" {g[:6]:>6}"
    header += "  Total"
    print(header)
    # 행
    for g in GRADES:
        row = g.ljust(col_width)
        total = 0
        for h in GRADES:
            v = matrix[g][h]
            total += v
            mark = "*" if g == h and v > 0 else " "
            row += f" {mark}{v:>5}"
        row += f"  {total:>5}"
        print(row)
    print("\n* = 대각선 (정답)")


def print_classification_report(metrics, n_total):
    """sklearn classification_report 스타일."""
    print("\n" + "=" * 80)
    print("Classification Report")
    print("=" * 80)
    print(f"{'Grade':<12} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Support':>10}")
    print("-" * 80)

    macro_p, macro_r, macro_f1 = 0.0, 0.0, 0.0
    weighted_p, weighted_r, weighted_f1 = 0.0, 0.0, 0.0
    total_support = sum(m["support"] for m in metrics.values())

    valid_classes = 0
    for g in GRADES:
        m = metrics[g]
        if m["support"] == 0:
            continue
        valid_classes += 1
        print(f"{g:<12} {m['precision']:>10.3f} {m['recall']:>10.3f} {m['f1']:>10.3f} {m['support']:>10}")
        macro_p += m["precision"]
        macro_r += m["recall"]
        macro_f1 += m["f1"]
        if total_support > 0:
            weighted_p += m["precision"] * m["support"] / total_support
            weighted_r += m["recall"] * m["support"] / total_support
            weighted_f1 += m["f1"] * m["support"] / total_support

    if valid_classes > 0:
        macro_p /= valid_classes
        macro_r /= valid_classes
        macro_f1 /= valid_classes

    print("-" * 80)
    print(f"{'Macro avg':<12} {macro_p:>10.3f} {macro_r:>10.3f} {macro_f1:>10.3f} {total_support:>10}")
    print(f"{'Weighted avg':<12} {weighted_p:>10.3f} {weighted_r:>10.3f} {weighted_f1:>10.3f} {total_support:>10}")
    print(f"\nTotal samples: {n_total}")


def main():
    data = load_results()
    if not data:
        return

    results = data.get("results", [])
    n = len(results)
    if n == 0:
        print("결과가 비어있습니다.")
        return

    matrix = confusion_matrix(results)
    metrics = per_class_metrics(results)

    print(f"=== SSAK3 평가 리포트 — 총 {n}건 ===\n")

    strict = sum(1 for r in results if r["actual_grade"] == r["expected_grade"])
    loose = sum(1 for r in results if r.get("dist", 99) <= 1)
    print(f"Strict Accuracy: {strict}/{n} = {100*strict/n:.1f}%")
    print(f"Loose Accuracy:  {loose}/{n} = {100*loose/n:.1f}%")

    print_confusion_matrix(matrix)
    print_classification_report(metrics, n)

    # JSON 저장
    out = {
        "n_total": n,
        "strict_accuracy": strict / n,
        "loose_accuracy": loose / n,
        "confusion_matrix": matrix,
        "per_class_metrics": {g: m for g, m in metrics.items()},
    }
    out_path = PROJECT_ROOT / "eval" / "report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
