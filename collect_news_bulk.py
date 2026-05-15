#!/usr/bin/env python3
"""대량 뉴스 URL 수집기 — 다변화된 한국 언론사 RSS + 네이버 페이지네이션

목적:
  학회 발표용 대규모 처리 실험(수천~수만 건)에 사용할
  실제 뉴스 URL을 다양한 매체에서 수집한다.
  각 URL에 분야(정치/경제/사회/IT/국제/문화/생활/종합) 메타데이터를
  부여하여 본 실험 결과를 분야별로 교차 분석할 수 있게 한다.

수집 소스:
  1) 주요 언론사 RSS 피드 (15+ 매체, 분야별 분리)
  2) 네이버 뉴스 섹션 페이지네이션 (정치/경제/사회/생활/세계/IT)

출력 JSON 구조:
  {
    "timestamp": ...,
    "urls": [...],           # 단순 URL 리스트
    "url_categories": {       # URL → 분야 매핑
      "https://...": "정치",
      ...
    },
    "domain_distribution": {...},
    "category_distribution": {...},
  }
"""

import argparse
import json
import time
import sys
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
HEADERS = {"User-Agent": UA}
TIMEOUT = 10

# ========== RSS 피드 목록 ==========
# (이름, RSS URL, 분야) — 분야는 본 실험 후 교차 분석에 사용됨
RSS_FEEDS = [
    # 연합뉴스 (분야별)
    ("연합뉴스-전체",   "https://www.yna.co.kr/rss/news.xml",              "종합"),
    ("연합뉴스-정치",   "https://www.yna.co.kr/rss/politics.xml",          "정치"),
    ("연합뉴스-경제",   "https://www.yna.co.kr/rss/economy.xml",           "경제"),
    ("연합뉴스-사회",   "https://www.yna.co.kr/rss/society.xml",           "사회"),
    ("연합뉴스-국제",   "https://www.yna.co.kr/rss/international.xml",     "국제"),
    ("연합뉴스-문화",   "https://www.yna.co.kr/rss/culture.xml",           "문화"),
    ("연합뉴스-IT",     "https://www.yna.co.kr/rss/industry.xml",          "IT/산업"),

    # 한겨레 (분야별)
    ("한겨레-전체",     "https://www.hani.co.kr/rss/",                     "종합"),
    ("한겨레-정치",     "https://www.hani.co.kr/rss/politics/",            "정치"),
    ("한겨레-경제",     "https://www.hani.co.kr/rss/economy/",             "경제"),
    ("한겨레-사회",     "https://www.hani.co.kr/rss/society/",             "사회"),
    ("한겨레-국제",     "https://www.hani.co.kr/rss/international/",       "국제"),

    # 경향
    ("경향-전체",       "http://www.khan.co.kr/rss/rssdata/total_news.xml",      "종합"),
    ("경향-정치",       "http://www.khan.co.kr/rss/rssdata/politic_news.xml",    "정치"),
    ("경향-경제",       "http://www.khan.co.kr/rss/rssdata/economy_news.xml",    "경제"),
    ("경향-사회",       "http://www.khan.co.kr/rss/rssdata/society_news.xml",    "사회"),

    # 경제지
    ("한국경제-전체",   "https://www.hankyung.com/feed/all-news",          "경제"),
    ("매일경제-전체",   "https://www.mk.co.kr/rss/30000001/",              "경제"),
    ("매일경제-정치",   "https://www.mk.co.kr/rss/30200030/",              "정치"),
    ("매일경제-사회",   "https://www.mk.co.kr/rss/50400012/",              "사회"),
    ("이데일리",        "https://rss.edaily.co.kr/edaily_news.xml",        "경제"),
    ("머니투데이",      "https://rss.mt.co.kr/news_main.xml",              "경제"),

    # 방송
    ("YTN-전체",        "https://www.ytn.co.kr/_comm/rss/rss_0102.xml",    "방송"),
    ("JTBC-전체",       "https://fs.jtbc.co.kr/RSS/newsflash.xml",         "방송"),

    # IT
    ("ZDNet-코리아",    "https://feeds.feedburner.com/zdkorea",            "IT/산업"),
    ("전자신문-전체",   "https://rss.etnews.com/Section901.xml",           "IT/산업"),
]

# 네이버 섹션 — (이름, 섹션ID, 분야)
NAVER_SECTIONS = [
    ("정치", "100", "정치"),
    ("경제", "101", "경제"),
    ("사회", "102", "사회"),
    ("생활", "103", "생활/문화"),
    ("세계", "104", "국제"),
    ("IT",   "105", "IT/산업"),
]

# 화이트리스트 도메인 (api_server.py와 동기화)
ALLOWED_DOMAINS = (
    "naver.com", "yna.co.kr", "kbs.co.kr", "mbc.co.kr", "sbs.co.kr",
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr",
    "khan.co.kr", "hankyung.com", "mk.co.kr", "mt.co.kr", "sedaily.com",
    "edaily.co.kr", "ohmynews.com", "newsis.com", "news1.kr",
    "ytn.co.kr", "jtbc.co.kr", "tvchosun.com", "channela.com",
    "heraldcorp.com", "etnews.com", "zdnet.co.kr", "blog.naver.com",
)


def is_allowed(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False


def extract_links_from_rss(name, rss_url, category):
    """RSS 피드에서 기사 링크를 추출. URL → 분야 매핑도 반환."""
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        content = resp.content

        urls = []
        try:
            root = ET.fromstring(content)
            for item in root.iter():
                tag = item.tag.split('}')[-1]
                if tag == "link":
                    href = item.text or item.get("href")
                    if href and href.startswith("http"):
                        urls.append(href.strip())
        except ET.ParseError:
            soup = BeautifulSoup(content, 'xml')
            for link in soup.find_all('link'):
                href = link.text or link.get('href', '')
                if href and href.startswith("http"):
                    urls.append(href.strip())

        clean = []
        seen = set()
        for u in urls:
            if u in seen or not is_allowed(u):
                continue
            seen.add(u)
            clean.append(u)
        return name, clean, category
    except Exception:
        return name, [], category


def extract_links_from_naver_section(name, section_id, category):
    try:
        url = f"https://news.naver.com/section/{section_id}"
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(resp.text, 'html.parser')

        urls = set()
        for a_tag in soup.select("a[href*='news.naver.com/mnews/article']"):
            href = a_tag.get("href", "")
            if href.startswith("http") and is_allowed(href):
                urls.add(href.strip())
        for a_tag in soup.select("a[href*='/article/']"):
            href = a_tag.get("href", "")
            if "news.naver.com" in href and href.startswith("http") and is_allowed(href):
                urls.add(href.strip())
        return f"네이버-{name}", list(urls), category
    except Exception:
        return f"네이버-{name}", [], category


def collect_all(target_count):
    print(f"\n[수집 시작] 목표 {target_count}건")
    print(f"  RSS 피드: {len(RSS_FEEDS)}개")
    print(f"  네이버 섹션: {len(NAVER_SECTIONS)}개")

    # url → category 매핑 (먼저 등록된 분야 우선 — 분야별 RSS가 종합 RSS보다 정확)
    url_category = {}
    all_urls_ordered = []

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = []
        for name, rss_url, cat in RSS_FEEDS:
            futures.append(ex.submit(extract_links_from_rss, name, rss_url, cat))
        for name, sid, cat in NAVER_SECTIONS:
            futures.append(ex.submit(extract_links_from_naver_section, name, sid, cat))

        # 분야별 RSS를 먼저 처리하고, 종합 RSS는 나중에 처리하기 위해 두 단계로 분리
        # 일단 결과를 받음
        source_results = []
        for fut in as_completed(futures):
            name, urls, cat = fut.result()
            if urls:
                source_results.append((name, urls, cat))
                print(f"  [{name}({cat})] {len(urls)}개")

    # 종합/방송 등 "광범위" 카테고리는 마지막에 등록되도록 정렬
    def priority(cat):
        # 구체적 분야가 종합/방송보다 우선
        if cat in ("종합", "방송"):
            return 1
        return 0

    source_results.sort(key=lambda x: priority(x[2]))

    for name, urls, cat in source_results:
        for u in urls:
            if u not in url_category:
                url_category[u] = cat
                all_urls_ordered.append(u)

    unique_urls = all_urls_ordered
    print(f"\n[고유 URL] 총 {len(unique_urls)}건")

    # 도메인 분포
    domain_count = {}
    for u in unique_urls:
        host = (urlparse(u).hostname or "unknown").replace("www.", "")
        domain_count[host] = domain_count.get(host, 0) + 1
    print("\n[도메인 분포]")
    for d, n in sorted(domain_count.items(), key=lambda x: -x[1]):
        print(f"  {d:>25}: {n}개")

    # 카테고리 분포
    cat_count = {}
    for u in unique_urls:
        c = url_category.get(u, "미상")
        cat_count[c] = cat_count.get(c, 0) + 1
    print("\n[분야 분포]")
    for c, n in sorted(cat_count.items(), key=lambda x: -x[1]):
        print(f"  {c:>10}: {n}개")

    if len(unique_urls) > target_count:
        unique_urls = unique_urls[:target_count]
        print(f"\n[자르기] 목표 {target_count}건만 사용")
        # 자른 후 분포 재계산
        url_category = {u: url_category[u] for u in unique_urls}
        cat_count = {}
        for u in unique_urls:
            c = url_category[u]
            cat_count[c] = cat_count.get(c, 0) + 1
        print("\n[자른 후 분야 분포]")
        for c, n in sorted(cat_count.items(), key=lambda x: -x[1]):
            print(f"  {c:>10}: {n}개")

    return unique_urls, url_category, domain_count, cat_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--out", default="news_urls.json")
    args = parser.parse_args()

    urls, url_cat, dom_dist, cat_dist = collect_all(args.count)

    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target_count": args.count,
        "actual_count": len(urls),
        "domain_distribution": dom_dist,
        "category_distribution": cat_dist,
        "url_categories": url_cat,
        "urls": urls,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n[저장] {args.out} — {len(urls)}건 (카테고리 메타 포함)")


if __name__ == "__main__":
    main()
