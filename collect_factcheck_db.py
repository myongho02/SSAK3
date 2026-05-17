#!/usr/bin/env python3
"""한국 팩트체크 사이트 거짓 판정 사례 수집기.

목적:
  외부 검증 자료(거짓 판정 사례)를 사전 수집하여 임베딩 DB로 구축.
  worker.py가 입력 기사를 분석할 때 이 DB와 의미 유사도를 비교해
  '이미 거짓 판정된 사례와 유사한가?' 보조 신호를 생성한다.

수집 대상:
  뉴스톱(newstopkorea.com)의 팩트체크 섹션 (전체 4,000건+)
  - 페이지네이션으로 N 페이지 순회
  - 각 기사 본문에서 "거짓 / 허위 / 사실이 아닌" 등 판정 키워드 포함된 케이스만 필터

사용:
    python3 collect_factcheck_db.py --pages 30 --out factcheck_db.json
"""

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {"User-Agent": UA}
TIMEOUT = 12
LIST_URL = "https://www.newstopkorea.com/news/articleList.html?sc_section_code=S1N45&page={page}"
VIEW_URL = "https://www.newstopkorea.com/news/articleView.html?idxno={idxno}"

# 거짓 판정을 의미하는 키워드 (본문에서 검색)
FALSE_KEYWORDS = [
    "거짓", "허위", "사실이 아니", "사실과 다르", "확인되지 않", "근거 없",
    "잘못된", "오해", "사실무근", "왜곡", "조작", "가짜뉴스", "가짜 뉴스",
    "낭설", "유언비어", "헛소문", "팩트체크: 거짓", "팩트체크: 허위",
]


def fetch_article_list(page):
    """뉴스톱 팩트체크 섹션 페이지에서 기사 idxno 추출.
    [주의] 뉴스톱 페이지네이션이 JS 기반이라 실제로는 page 매개변수가 무시됨.
    대신 fetch_idxno_range()를 사용해 idxno를 직접 차감하며 수집."""
    url = LIST_URL.format(page=page)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(resp.text, "html.parser")
        idxnos = []
        for a in soup.select("a"):
            href = a.get("href", "")
            m = re.search(r"idxno=(\d+)", href)
            if m:
                idxno = int(m.group(1))
                if idxno not in idxnos:
                    idxnos.append(idxno)
        return idxnos
    except Exception:
        return []


def fetch_idxno_range(start_idxno, count):
    """idxno=start_idxno 부터 -1씩 내려가며 count 개 idxno 리스트 생성.
    페이지네이션 없이 직접 ID로 옛 기사에 접근."""
    return list(range(start_idxno, start_idxno - count, -1))


def fetch_article(idxno):
    """기사 본문에서 제목 + 본문 추출 + 팩트체크 + 거짓 키워드 매칭 검사."""
    url = VIEW_URL.format(idxno=idxno)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        # 제목 (meta og:title 우선 — 가장 안정적)
        title = ""
        og = soup.select_one("meta[property='og:title']")
        if og:
            title = og.get("content", "").strip()
        if not title:
            t = soup.select_one("h3.heading, h1.title, .article-head-title")
            if t:
                title = t.get_text(strip=True)

        # 팩트체크 섹션 여부 — 제목에 [팩트체크] 또는 [주간팩트체크] 라벨
        title_lower = title
        is_factcheck = bool(re.search(r"\[(주간)?팩트체크\]|\[Fact[- ]?Check\]", title_lower, re.I))
        if not is_factcheck:
            return None

        # 본문 영역
        body_tag = soup.select_one("#article-view-content-div, .article-view-content, .article-body")
        if not body_tag:
            body_tag = soup.select_one("article, main")
        body_text = body_tag.get_text(separator=" ", strip=True) if body_tag else ""
        body_text = re.sub(r"\s+", " ", body_text)[:2000]

        # 제목에서 "[팩트체크]" 등 라벨 제거
        title = re.sub(r"^\s*\[[^\]]+\]\s*", "", title).strip()

        if not title or len(body_text) < 100:
            return None

        # 거짓 판정 키워드 매칭 (본문에서 검색)
        matched = [k for k in FALSE_KEYWORDS if k in body_text]

        return {
            "idxno": idxno,
            "url": url,
            "title": title,
            "body_excerpt": body_text[:600],
            "matched_keywords": matched[:8],
            "has_false_signal": len(matched) > 0,
        }
    except Exception:
        return None


def collect(start_idxno=44989, scan_count=500, max_workers=10):
    """idxno=start_idxno 부터 -1씩 내려가며 scan_count 개 시도.
    팩트체크 라벨 + 본문 길이 조건 충족하는 케이스만 수집."""
    idxnos = fetch_idxno_range(start_idxno, scan_count)
    print(f"[수집 시작] idxno {start_idxno} ~ {start_idxno - scan_count + 1} (총 {scan_count}개 스캔)")

    cases = []
    false_cases = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_article, idx): idx for idx in idxnos}
        done = 0
        for fut in as_completed(futures):
            done += 1
            result = fut.result()
            if result:
                cases.append(result)
                if result["has_false_signal"]:
                    false_cases.append(result)
            if done % 50 == 0:
                print(f"  진행 {done}/{scan_count} | 팩트체크 {len(cases)}건 | 거짓 신호 {len(false_cases)}건")

    print(f"\n[수집 완료]")
    print(f"  팩트체크 라벨 매칭: {len(cases)}건")
    print(f"  거짓 신호 포함: {len(false_cases)}건")

    # 매칭 키워드 통계
    from collections import Counter
    kw_count = Counter()
    for c in false_cases:
        for k in c["matched_keywords"]:
            kw_count[k] += 1
    print("\n[매칭 키워드 분포]")
    for k, n in kw_count.most_common(10):
        print(f"  {k:>15}: {n}건")

    return cases  # 모든 팩트체크 케이스 반환 (거짓 신호 유무는 메타로)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, default=44989,
                   help="시작 idxno (최신 기사)")
    p.add_argument("--scan", type=int, default=500,
                   help="스캔할 idxno 개수 (start ~ start-scan+1)")
    p.add_argument("--out", default="factcheck_db.json")
    args = p.parse_args()

    cases = collect(start_idxno=args.start, scan_count=args.scan)

    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "newstopkorea.com 팩트체크 섹션",
        "total": len(cases),
        "false_signal_count": sum(1 for c in cases if c["has_false_signal"]),
        "cases": cases,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n[저장] {args.out} — 팩트체크 {len(cases)}건")
    print("\n[샘플 5건]")
    for c in cases[:5]:
        flag = "⚠️ 거짓신호" if c["has_false_signal"] else "✓ 일반"
        print(f"  [{c['idxno']}] {flag} {c['title'][:55]}")


if __name__ == "__main__":
    main()
