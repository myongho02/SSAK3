import pika
import json
import sqlite3
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from newspaper import Article
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from transformers import pipeline

# ========== AI 모델 로딩 (Worker 시작 시 1번만 로딩) ==========
# snunlp/KR-FinBert-SC: 한국어 감성분석 특화 모델 (긍정/부정/중립 분류)
# 금융 뉴스 학습 기반이라 뉴스 기사의 감성 판별에 적합
# truncation=True: 512 토큰 초과 시 자동 잘림 (BERT 최대 길이 제한)
print("[Worker] AI 모델 로딩 중... (처음에 1~2분 걸릴 수 있음)")
sentiment_analyzer = pipeline(
    "sentiment-analysis",
    model="snunlp/KR-FinBert-SC",
    truncation=True,
    max_length=512
)
print("[Worker] AI 모델 로딩 완료!")

# ========== DB 초기화 ==========
DB_PATH = "/app/data/results.db"

def init_db():
    """결과 저장용 테이블 만들기 (없으면 새로 생성)"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # timeout=30: 다른 Worker가 DB를 잠그고 있을 때 최대 30초까지 대기
    # SQLite는 파일 기반 DB라서 동시에 1개만 쓰기 가능 → timeout으로 대기
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()

    # WAL(Write-Ahead Logging) 모드 활성화
    # 기본 모드(DELETE)는 쓰기 시 DB 전체를 잠그지만,
    # WAL 모드는 읽기와 쓰기를 동시에 허용하여 Worker 여러 개가 경합할 때 성능 향상
    cursor.execute("PRAGMA journal_mode=WAL")

    # status 컬럼: 분석 성공('done') / 크롤링 실패('failed') 등 처리 상태 기록
    # 기본값 'done'으로 설정하여 기존 데이터와 호환 유지
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS analysis_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            title TEXT,
            body TEXT,
            content_score REAL,
            provocative_score REAL,
            source_score REAL,
            total_score REAL,
            grade TEXT,
            status TEXT DEFAULT 'done',
            analyzed_at TEXT,
            matched_keywords TEXT,
            detected_provocative TEXT,
            ai_sentiment TEXT,
            source_name TEXT
        )
    ''')

    # ── 기존 DB에 새 컬럼이 없으면 추가 (마이그레이션) ──
    # ALTER TABLE은 컬럼이 이미 있으면 에러가 나므로 try/except로 무시
    for col in ['matched_keywords', 'detected_provocative', 'ai_sentiment', 'source_name']:
        try:
            cursor.execute(f"ALTER TABLE analysis_results ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # 이미 존재하는 컬럼 — 무시

    conn.commit()
    conn.close()

# ========== 뉴스 크롤링 ==========

def crawl_naver_news(url):
    """네이버 뉴스 전용 크롤러 — BS4로 기사 본문 + 원본 언론사명 추출

    newspaper3k는 네이버 뉴스의 JS 렌더링 본문을 못 가져오므로,
    requests + BeautifulSoup으로 직접 추출한다.

    Returns: (제목, 본문, 원본 언론사명)
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, 'html.parser')

    # 제목 추출: og:title 메타 태그가 가장 안정적 (페이지 구조 변경에 강함)
    title_tag = soup.find("meta", property="og:title")
    title = title_tag["content"] if title_tag else ""

    # 본문 추출: #dic_area가 네이버 뉴스 본문 영역 (모바일/PC 공통)
    body_tag = soup.select_one("#dic_area") or soup.select_one("#articleBodyContents")
    if body_tag:
        # 스크립트/스타일 태그 제거 후 순수 텍스트만 추출
        for tag in body_tag.find_all(["script", "style"]):
            tag.decompose()
        body = body_tag.get_text(separator="\n", strip=True)
    else:
        body = ""

    # ── 원본 언론사명 추출 ──
    # 네이버 뉴스는 og:article:author 메타 태그에 "연합뉴스 | 네이버" 형식으로 저장
    # " | 네이버" 부분을 제거하면 원본 언론사명을 얻을 수 있음
    source_name = ""
    author_tag = soup.find("meta", property="og:article:author")
    if author_tag:
        # "연합뉴스 | 네이버" → "연합뉴스"
        source_name = author_tag.get("content", "").split("|")[0].strip()

    # og:article:author가 없으면 언론사 로고의 alt 텍스트에서 추출
    if not source_name:
        logo_tag = soup.select_one(".media_end_head_top_logo img")
        if logo_tag:
            source_name = logo_tag.get("alt", "").strip()

    return title, body, source_name

def crawl_article(url):
    """URL에 따라 적절한 크롤러를 선택하여 기사를 가져온다.

    - 네이버 뉴스: BS4 직접 파싱 (JS 렌더링 우회) + 원본 언론사명 추출
    - 기타 사이트: newspaper3k 범용 크롤러 사용 + URL에서 도메인 추출

    Returns: (제목, 본문, 원본 언론사명)
    """
    if "news.naver.com" in url:
        return crawl_naver_news(url)

    # 네이버 외 사이트는 newspaper3k로 크롤링
    article = Article(url, language='ko')
    article.download()
    article.parse()
    # newspaper3k의 source_url에서 도메인명을 언론사명으로 사용
    # 예: "https://www.hani.co.kr/..." → "hani.co.kr"
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.replace("www.", "")
    return article.title, article.text, domain

# ========== 신뢰도 분석 함수들 ==========

def extract_lead_sentences(body, n=3):
    """본문에서 첫 N문장(리드)을 추출한다.
    뉴스 기사는 역피라미드 구조로, 첫 1~3문장에 핵심 내용이 집중되어 있다.
    마침표(.), 느낌표(!), 물음표(?) 기준으로 문장을 분리한다.
    """
    # 마침표/느낌표/물음표 뒤에 공백 또는 줄바꿈이 오는 지점에서 분리
    sentences = re.split(r'(?<=[.!?])\s+', body.strip())
    # 빈 문장 제거 후 첫 N문장 반환
    sentences = [s for s in sentences if len(s.strip()) > 5]
    return ' '.join(sentences[:n])

def extract_title_keywords(title):
    """제목에서 핵심 명사(키워드)를 추출한다.
    한국어 형태소 분석기 없이도 동작하도록, 다음 규칙을 적용한다:
    1. 조사/어미/기호 등 불용어 패턴을 제거
    2. 2글자 이상의 단어만 키워드로 인정
    3. 숫자+단위 조합(200억원 등)도 키워드로 포함
    """
    # 괄호, 특수기호 제거: [속보], (종합), 「」 등
    cleaned = re.sub(r'[\[\]()「」『』【】<>]', ' ', title)
    # 공백 기준으로 토큰 분리
    tokens = cleaned.split()

    # 한국어 불용어 (조사, 접속사, 관형사 등) — 단독으로 쓰이는 것만
    stopwords = {
        '의', '가', '이', '은', '는', '을', '를', '에', '에서', '와', '과',
        '도', '로', '으로', '만', '까지', '부터', '에게', '한', '할', '하는',
        '된', '되는', '및', '등', '더', '그', '이', '저', '것', '수', '때',
        '위해', '대한', '통해', '위한', '관련', '대해', '또는', '하지만',
    }

    keywords = []
    for token in tokens:
        # 끝에 붙은 조사 패턴 제거: "정부는" → "정부", "경제를" → "경제"
        # 1~2글자 조사가 끝에 붙어있으면 떼어냄
        word = re.sub(r'(은|는|이|가|을|를|의|에|와|과|로|도|만|까지|부터|에서|에게|으로|이다|했다|한다|된다|되는|하는|에는)$', '', token)
        # 2글자 이상이고 불용어가 아닌 단어만 키워드로 인정
        if len(word) >= 2 and word not in stopwords:
            keywords.append(word)

    return keywords

def analyze_content_similarity(title, body):
    """지표 1: 본문 일치도 (45%) — 코사인 유사도 + 키워드 매칭 결합

    [개선된 분석 방법]
    1. 코사인 유사도 (50%): 제목과 본문 첫 3문장(리드)만 비교
    2. 키워드 매칭 (50%): 제목의 핵심 명사가 본문에 등장하는 비율

    Returns: (최종 점수, 상세 정보 dict)
        상세 정보: 제목 키워드, 매칭된 키워드, 코사인 유사도 원본값
    """
    # ── 빈 입력 처리 ──
    if not title or not body:
        return 0.0, {"keywords": [], "matched": [], "cosine_raw": 0.0}

    # ── 1단계: 코사인 유사도 (제목 vs 리드 3문장) ──
    lead = extract_lead_sentences(body, n=3)
    if not lead:
        lead = body[:200]

    vectorizer = TfidfVectorizer()
    vectors = vectorizer.fit_transform([title, lead])
    cosine_score = cosine_similarity(vectors[0], vectors[1])[0][0]
    cosine_score_scaled = cosine_score * 100

    # ── 2단계: 키워드 매칭 ──
    keywords = extract_title_keywords(title)

    if not keywords:
        return round(cosine_score_scaled, 1), {
            "keywords": [], "matched": [],
            "cosine_raw": round(cosine_score, 4)
        }

    # 각 키워드가 본문에 등장하는지 확인하고, 매칭된 키워드 목록을 기록
    matched_list = [kw for kw in keywords if kw in body]
    match_ratio = len(matched_list) / len(keywords)
    keyword_score = match_ratio * 100

    # ── 3단계: 결합 ──
    final_score = (cosine_score_scaled * 0.5) + (keyword_score * 0.5)
    final_score = max(0, min(100, final_score))

    # ── 상세 정보를 함께 반환 (대시보드에서 분석 근거로 표시) ──
    details = {
        "keywords": keywords,           # 제목에서 추출한 전체 키워드
        "matched": matched_list,         # 본문에서 발견된 키워드
        "cosine_raw": round(cosine_score, 4)  # 코사인 유사도 원본값 (0~1)
    }

    return round(final_score, 1), details

def analyze_ai_sentiment(text):
    """AI 감성분석 보조 지표 — HuggingFace KR-FinBert-SC 모델 사용

    [동작 원리]
    - 기사 본문을 BERT 모델에 입력하여 긍정/부정/중립 확률값을 받음
    - 중립(neutral)일수록 객관적 보도 → 높은 점수 (신뢰도 높음)

    Returns: (점수, 상세 정보 dict)
        상세 정보: 각 라벨별 확률값 (대시보드에서 근거로 표시)
    """
    try:
        # BERT 모델의 최대 입력 길이는 512 토큰이므로 앞부분만 사용
        result = sentiment_analyzer(text[:512])
        label = result[0]['label']    # 'neutral', 'positive', 'negative'
        score = result[0]['score']    # 해당 라벨의 확률값 (0.0~1.0)

        # ── 라벨별 확률값을 정리 (대시보드 표시용) ──
        # 모델은 최고 확률 라벨 1개만 반환하므로, 나머지는 잔여 확률로 추정
        # (정확한 3-class 확률은 모델 출력이 top-1만 제공하여 근사치 사용)
        sentiment_detail = {"neutral": 0, "positive": 0, "negative": 0}
        sentiment_detail[label] = round(score * 100, 1)
        # 나머지 두 라벨에 잔여 확률을 균등 배분 (근사치)
        remaining = round((1 - score) * 100, 1)
        other_labels = [l for l in sentiment_detail if l != label]
        for ol in other_labels:
            sentiment_detail[ol] = round(remaining / 2, 1)

        if label == 'neutral':
            final = round(score * 100, 1)
        elif label == 'positive':
            final = round(score * 70, 1)
        else:
            final = round((1 - score) * 50, 1)

        return final, sentiment_detail
    except Exception as e:
        print(f"[Worker] AI 감성분석 오류: {e}")
        return 50.0, {"neutral": 33.3, "positive": 33.3, "negative": 33.3}

def analyze_provocative(title, body):
    """지표 2: 자극성 분석 (35%) — 단어 기반 분석 + AI 감성분석 결합

    [분석 방법]
    1. 단어 기반 분석 (50%): 카테고리별 자극적 표현 + 문장부호 남용 감지
    2. AI 모델 분석 (50%): KR-FinBert-SC로 본문의 감성(긍정/부정/중립) 판별
    3. 최종 점수 = (단어 기반 × 0.5) + (AI 모델 × 0.5)

    [단어 기반 분석 세부]
    - 자극적 단어를 4가지 카테고리로 분류하고 카테고리별 가중치 적용
    - 제목에 등장하는 자극적 단어는 가중치 2배 (클릭 유도 목적이 강함)
    - 본문 길이(글자 수) 대비 비율로 정규화하여 긴 기사가 불이익받지 않도록 함
    - 느낌표(!!)/물음표(??) 연속 사용도 감점 대상에 포함

    참고: Alonso et al., "Sentiment Analysis for Fake News Detection" (Electronics, MDPI, 2021)
    """

    # ── 카테고리별 자극적 단어 사전 ──
    # 각 카테고리는 서로 다른 유형의 자극성을 측정하며, 가중치가 다름
    PROVOCATIVE_CATEGORIES = {
        # 과장형: 사실을 부풀려서 클릭을 유도하는 표현 (가중치 1.0)
        'exaggeration': {
            'weight': 1.0,
            'words': [
                '역대급', '최초', '최악', '최고', '대박', '레전드',
                '실화', '미쳤', '놀라운', '엄청난', '압도적',
                '기적', '전무후무', '상상초월', '파격', '초대형',
            ]
        },
        # 혐오형: 특정 대상을 비하·공격하는 표현 (가중치 1.5 — 신뢰도에 더 큰 영향)
        'hate': {
            'weight': 1.5,
            'words': [
                '망신', '몰락', '추락', '폭망', '치욕', '굴욕',
                '망조', '쓰레기', '한심', '꼴불견', '역겹',
                '파렴치', '후안무치', '적반하장',
            ]
        },
        # 선정형: 감정을 자극하여 클릭을 유도하는 표현 (가중치 1.2)
        'sensational': {
            'weight': 1.2,
            'words': [
                '충격', '경악', '소름', '폭로', '단독', '특종',
                '발칵', '난리', '파문', '후폭풍', '대참사',
                '충격적', '소름끼치는', '믿기힘든', '경악스러운',
                '논란', '파장', '급반전', '반전',
            ]
        },
        # 공포형: 불안·공포를 조성하는 표현 (가중치 1.3)
        'fear': {
            'weight': 1.3,
            'words': [
                '긴급', '속보', '비상', '패닉', '공포', '참사',
                '전율', '아찔', '섬뜩', '끔찍', '절규',
                '날벼락', '치명적', '위험', '경고', '대재앙',
            ]
        },
    }

    # ── 1단계: 제목과 본문에서 카테고리별 자극적 단어 검출 ──
    TITLE_MULTIPLIER = 2.0
    weighted_hit_count = 0

    # 카테고리별 감지된 단어를 기록 (대시보드에서 근거로 표시)
    # 한글 카테고리명 매핑: 대시보드에서 [과장] 역대급 형태로 표시
    CATEGORY_LABELS = {
        'exaggeration': '과장',
        'hate': '혐오',
        'sensational': '선정',
        'fear': '공포',
    }
    detected_words = {}  # {"과장": ["역대급", "대박"], "선정": ["충격"]}

    for category, config in PROVOCATIVE_CATEGORIES.items():
        cat_weight = config['weight']
        cat_label = CATEGORY_LABELS[category]

        for word in config['words']:
            title_hits = len(re.findall(re.escape(word), title))
            body_hits = len(re.findall(re.escape(word), body))

            if title_hits > 0 or body_hits > 0:
                # 감지된 단어를 카테고리별로 기록
                if cat_label not in detected_words:
                    detected_words[cat_label] = []
                detected_words[cat_label].append(word)

            weighted_hit_count += cat_weight * (title_hits * TITLE_MULTIPLIER + body_hits)

    # ── 2단계: 느낌표/물음표 연속 사용 감지 ──
    punctuation_hits = len(re.findall(r'[!]{2,}', f"{title} {body}"))
    punctuation_hits += len(re.findall(r'[?]{2,}', f"{title} {body}"))
    title_punct = len(re.findall(r'[!]{2,}', title)) + len(re.findall(r'[?]{2,}', title))
    weighted_hit_count += title_punct * TITLE_MULTIPLIER + punctuation_hits

    # ── 3단계: 본문 길이 대비 비율로 정규화 ──
    total_chars = len(title) + len(body)
    if total_chars == 0:
        return 50.0, {"detected": {}, "ratio": 0, "ai": {}}

    ratio_per_100 = (weighted_hit_count / total_chars) * 100

    # ── 4단계: 단어 기반 점수 산출 ──
    word_score = max(0, 100 - (ratio_per_100 * 33.3))

    # ── 5단계: AI 감성분석 점수와 결합 ──
    ai_score, ai_detail = analyze_ai_sentiment(body)
    final_score = (word_score * 0.5) + (ai_score * 0.5)

    # ── 상세 정보를 함께 반환 (대시보드에서 분석 근거로 표시) ──
    details = {
        "detected": detected_words,                   # 카테고리별 감지된 단어
        "ratio": round(ratio_per_100, 2),              # 자극적 표현 비율 (%)
        "ai": ai_detail                                # AI 감성분석 라벨별 확률
    }

    return round(final_score, 1), details

def analyze_source(url, source_name=""):
    """지표 3: 출처 신뢰도 (20%) — 원본 언론사명 기반 신뢰도 판정

    [개선된 분석 방법]
    기존: URL의 도메인만 확인 → 네이버 뉴스는 전부 naver.com으로 65점 고정
    개선: 크롤링 시 추출한 원본 언론사명(source_name)으로 판정
          → "연합뉴스", "KBS" 등 실제 언론사를 구분하여 정확한 점수 부여

    [판정 기준 — 3단계]
    1. 주요 언론사 (85점): 통신사, 지상파, 종합일간지 등 공신력 있는 매체
    2. 등록 인터넷 매체 (65점): 인터넷 신문, 경제지, 전문지 등 등록된 매체
    3. 출처 불명 (35점): 언론사명을 확인할 수 없거나 미등록 매체

    Args:
        url: 기사 URL (언론사명 추출 실패 시 도메인 기반 폴백용)
        source_name: 크롤링 시 추출한 원본 언론사명 (예: "연합뉴스", "KBS")
    """

    # ── 주요 언론사 목록 (85점) ──
    # 통신사, 지상파 방송, 종합일간지 등 공신력이 검증된 매체
    MAJOR_SOURCES = [
        # 통신사
        '연합뉴스', '연합뉴스TV',
        # 지상파 방송
        'KBS', 'MBC', 'SBS', 'KBS 뉴스', 'MBC 뉴스', 'SBS 뉴스',
        # 종합일간지
        '조선일보', '중앙일보', '동아일보',
        '한겨레', '경향신문', '한국일보', '국민일보', '서울신문',
        '세계일보', '문화일보',
        # 종합편성채널
        'JTBC', 'TV조선', '채널A', 'MBN',
        # 통신사 영문명 (도메인 폴백 용)
        'yonhapnews.co.kr', 'yna.co.kr',
        'kbs.co.kr', 'mbc.co.kr', 'sbs.co.kr',
        'chosun.com', 'donga.com', 'hani.co.kr',
        'joongang.co.kr', 'khan.co.kr',
    ]

    # ── 등록 인터넷 매체 목록 (65점) ──
    # 인터넷 신문, 경제 전문지, IT 전문지 등 등록된 매체
    REGISTERED_SOURCES = [
        # 인터넷 신문
        '뉴시스', '뉴스1', '노컷뉴스', '오마이뉴스', '프레시안',
        '미디어오늘', '팩트체크뉴스', '더팩트',
        # 경제지
        '매일경제', '한국경제', '서울경제', '머니투데이', '이데일리',
        '파이낸셜뉴스', '아시아경제', '헤럴드경제',
        # IT/전문지
        'ZDNet', '전자신문', '디지털데일리', '블로터',
        # 도메인 폴백 용
        'newsis.com', 'news1.kr', 'edaily.co.kr',
        'hankyung.com', 'mk.co.kr', 'mt.co.kr',
        'sedaily.com', 'heraldcorp.com',
        'nocutnews.co.kr', 'ohmynews.com',
        'zdnet.co.kr', 'etnews.com',
    ]

    # ── 1단계: 원본 언론사명으로 매칭 ──
    if source_name:
        for name in MAJOR_SOURCES:
            if name in source_name or source_name in name:
                return 85.0, "주요 언론사"
        for name in REGISTERED_SOURCES:
            if name in source_name or source_name in name:
                return 65.0, "등록 매체"

    # ── 2단계: URL 도메인으로 폴백 ──
    for name in MAJOR_SOURCES:
        if name in url:
            return 85.0, "주요 언론사"
    for name in REGISTERED_SOURCES:
        if name in url:
            return 65.0, "등록 매체"

    # ── 3단계: 출처 불명 ──
    return 35.0, "출처 불명"

def calculate_total_score(content, provocative, source):
    """종합 점수: 본문일치도(45%) + 자극성분석(35%) + 출처신뢰도(20%)"""
    total = (content * 0.45) + (provocative * 0.35) + (source * 0.20)
    return round(total, 1)

def get_grade(score):
    """점수를 등급으로 변환"""
    if score >= 80:
        return "신뢰 가능"
    elif score >= 60:
        return "주의 필요"
    elif score >= 40:
        return "의심 기사"
    else:
        return "신뢰 낮음"

def save_to_db(result):
    """분석 결과를 SQLite에 저장 (동시 쓰기 경합 대응)

    Worker 여러 개가 동시에 INSERT하면 'database is locked' 에러가 발생할 수 있다.
    이를 방지하기 위해:
    1. timeout=30: 다른 Worker가 쓰기 중이면 최대 30초 대기
    2. try-except: locked 에러 발생 시 3초 후 재시도 (최대 3회)
    3. finally: 예외 발생 여부와 관계없이 DB 연결을 반드시 닫음
    """
    max_retries = 3  # 최대 재시도 횟수

    for attempt in range(1, max_retries + 1):
        conn = None
        try:
            # timeout=30: 잠금 대기 시간 (기본값 5초는 Worker 3개 이상에서 부족)
            conn = sqlite3.connect(DB_PATH, timeout=30)
            cursor = conn.cursor()
            # status: 분석 성공('done') 또는 크롤링 실패('failed')
            # matched_keywords ~ source_name: 분석 근거 상세 데이터 (JSON 문자열)
            cursor.execute('''
                INSERT INTO analysis_results
                (url, title, body, content_score, provocative_score, source_score,
                 total_score, grade, status, analyzed_at,
                 matched_keywords, detected_provocative, ai_sentiment, source_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                result['url'], result['title'], result['body'][:500],
                result['content'], result['provocative'], result['source'],
                result['total'], result['grade'], result.get('status', 'done'),
                result['analyzed_at'],
                result.get('matched_keywords', ''),
                result.get('detected_provocative', ''),
                result.get('ai_sentiment', ''),
                result.get('source_name', '')
            ))
            conn.commit()
            return  # 성공하면 함수 종료

        except sqlite3.OperationalError as e:
            # "database is locked" 에러: 다른 Worker가 쓰기 중
            if "locked" in str(e) and attempt < max_retries:
                print(f"[Worker] DB 잠금 감지, {attempt}/{max_retries} 재시도 (3초 후)")
                time.sleep(3)
            else:
                # 3회 모두 실패하거나 다른 종류의 에러면 로그 출력
                print(f"[Worker] DB 저장 실패: {e}")

        finally:
            # 예외 발생 여부와 관계없이 연결을 반드시 닫아 잠금 해제
            if conn:
                conn.close()

# ========== 메인: 큐에서 기사 꺼내서 분석 ==========

def is_already_analyzed(url):
    """해당 URL이 이미 분석되었는지 DB에서 확인한다.
    같은 기사를 중복 분석하면 DB에 동일한 결과가 쌓이고,
    Worker 리소스(크롤링 + AI 모델 추론)가 낭비되므로 사전에 체크한다.

    Returns: True면 이미 분석됨 → skip, False면 신규 → 분석 진행
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM analysis_results WHERE url = ?", (url,))
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        # DB 조회 실패 시에는 안전하게 분석 진행 (중복보다 누락이 더 나쁨)
        return False

def process_message(ch, method, properties, body):
    """큐에서 메시지를 받으면 실행되는 함수"""
    data = json.loads(body)
    url = data['url']
    print(f"[Worker] 분석 시작: {url}")

    # ── 중복 분석 방지: 이미 분석된 URL이면 건너뛰기 ──
    if is_already_analyzed(url):
        print(f"[Worker] 이미 분석된 기사입니다 (skip): {url}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    # 실제 뉴스 크롤링 — 제목, 본문, 원본 언론사명을 함께 추출
    try:
        title, article_body, source_name = crawl_article(url)
    except Exception as e:
        # 크롤링 실패 시 status='failed'로 DB에 기록
        # 사용자가 대시보드에서 실패 원인을 확인할 수 있도록 에러 메시지 저장
        print(f"[Worker] 크롤링 실패: {url} — {e}")
        fail_result = {
            'url': url, 'title': '크롤링 실패', 'body': str(e)[:500],
            'content': 0, 'provocative': 0, 'source': 0,
            'total': 0, 'grade': '분석 불가', 'status': 'failed',
            'analyzed_at': time.strftime("%Y-%m-%d %H:%M:%S")
        }
        save_to_db(fail_result)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    if not article_body or len(article_body.strip()) < 50:
        # 본문이 너무 짧으면 정상적인 분석이 불가능 → 실패 처리
        print(f"[Worker] 본문이 너무 짧거나 비어있음: {url}")
        fail_result = {
            'url': url, 'title': title or '본문 부족', 'body': article_body or '',
            'content': 0, 'provocative': 0, 'source': 0,
            'total': 0, 'grade': '분석 불가', 'status': 'failed',
            'analyzed_at': time.strftime("%Y-%m-%d %H:%M:%S")
        }
        save_to_db(fail_result)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    # ── 3가지 지표 분석 (상세 근거 데이터 함께 수집) ──
    content, content_details = analyze_content_similarity(title, article_body)
    provocative, provocative_details = analyze_provocative(title, article_body)
    source, source_class = analyze_source(url, source_name)
    total = calculate_total_score(content, provocative, source)
    grade = get_grade(total)

    result = {
        'url': url,
        'title': title,
        'body': article_body,
        'content': content,
        'provocative': provocative,
        'source': source,
        'total': total,
        'grade': grade,
        'analyzed_at': time.strftime("%Y-%m-%d %H:%M:%S"),
        # ── 분석 근거 상세 데이터 (JSON 문자열로 DB에 저장) ──
        'matched_keywords': json.dumps(content_details, ensure_ascii=False),
        'detected_provocative': json.dumps(provocative_details, ensure_ascii=False),
        'ai_sentiment': json.dumps(provocative_details.get('ai', {}), ensure_ascii=False),
        'source_name': f"{source_name}|{source_class}",  # "연합뉴스|주요 언론사" 형태
    }

    save_to_db(result)

    print(f"[Worker] 분석 완료: {title}")
    print(f"         본문일치: {content:.1f} | 자극성: {provocative:.1f} | 출처: {source:.1f} | 종합: {total:.1f}점 ({grade})")

    ch.basic_ack(delivery_tag=method.delivery_tag)

def main():
    """Worker 메인: RabbitMQ에 연결하고 큐에서 메시지 기다리기"""
    init_db()

    while True:
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host='rabbitmq')
            )
            break
        except pika.exceptions.AMQPConnectionError:
            print("[Worker] RabbitMQ 연결 대기 중... 5초 후 재시도")
            time.sleep(5)

    channel = connection.channel()
    channel.queue_declare(queue='news_queue', durable=True)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue='news_queue', on_message_callback=process_message)

    print("[Worker] 대기 중... 큐에서 기사를 기다리고 있습니다.")
    channel.start_consuming()

if __name__ == "__main__":
    main()
