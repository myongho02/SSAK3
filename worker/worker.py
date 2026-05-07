import pika
import json
import sqlite3
import os
import re
import time
import socket       # 컨테이너 hostname(=컨테이너 ID) 조회용
import hashlib      # 캐시 키 생성용 (긴 텍스트 → 짧은 해시)
import requests
from functools import lru_cache
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from newspaper import Article
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

# ========== AI 보조지표 모델 로딩 (Worker 시작 시 1번만 로딩) ==========
# snunlp/KR-FinBert-SC: 한국어 감성분석 특화 모델 (긍정/부정/중립 분류)
# 금융 뉴스 학습 기반이라 뉴스 기사의 논조 판별에 적합
#
# [역할] 자극성 분석의 보조지표로 사용 (단독 판정이 아닌 규칙 기반 분석과 50:50 결합)
# - 뉴스의 진위를 판별하는 모델이 아님
# - 기사의 논조(중립/긍정/부정)를 분류하여 자극성 점수 산출에 보조적으로 활용
#
# [최적화] pipeline() 대신 tokenizer + model을 직접 로딩
# - torch.no_grad()로 그래디언트 계산 비활성화 → 메모리 절감 + 속도 향상
# - 배치 추론 가능 → 여러 기사를 한번에 처리할 때 효율적
# ========== Worker ID 및 컨테이너 정보 설정 ==========
# docker-compose.yml에서 WORKER_ID 환경변수를 주입받아 로그에 사용한다.
# 예: WORKER_ID=1 → 로그에 "[Worker-1]"로 표시
# 환경변수가 없으면 기본값 "0"을 사용 (로컬 단독 실행 시)
WORKER_ID = os.environ.get("WORKER_ID", "0")
WORKER_TAG = f"[Worker-{WORKER_ID}]"  # 로그 출력에 사용할 태그

# ── 컨테이너 ID(hostname) 조회 ──
# Docker 컨테이너 안에서 hostname은 컨테이너 ID(12자리 해시)가 된다.
# 이를 통해 각 Worker가 독립적인 컴퓨터(컨테이너)에서 실행되고 있음을 증명한다.
# 예: "Worker-1 (컨테이너: a3f8b2c1d4e5) 시작"
CONTAINER_ID = socket.gethostname()
print(f"{'='*60}")
print(f"[시스템] {WORKER_TAG} 시작 — 컨테이너 ID: {CONTAINER_ID}")
print(f"[시스템] 독립 컨테이너에서 실행 중 (분산 병렬 처리)")
print(f"{'='*60}")

print(f"{WORKER_TAG} 자극성 보조모델(KR-FinBert-SC) 로딩 중... (처음에 1~2분 걸릴 수 있음)")
MODEL_NAME = "snunlp/KR-FinBert-SC"
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
model.eval()  # 추론 모드 전환 — dropout 등 학습 전용 레이어 비활성화
print(f"{WORKER_TAG} 자극성 보조모델 로딩 완료!")

# ========== 본문 일치도 NLI 모델 로딩 ==========
# 논문 2.2 명시: "자연어 추론(NLI) 모델로 제목이 본문 핵심을 논리적으로 포함하는지"
# MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli:
#   - 다국어 NLI 모델 (XNLI에 한국어 포함)
#   - ~110MB로 가벼움 → Worker 컨테이너 부담 적음
#   - 3-class 분류: entailment(함의) / neutral(중립) / contradiction(모순)
#
# [사용 방식] premise=본문 리드, hypothesis=제목 → entailment 확률을 본문 일치도 메인 신호로
# - entailment 높음 → 제목이 본문 핵심을 논리적으로 포함 → 일치도 점수↑
# - contradiction 높음 → 제목과 본문이 어긋남(낚시성) → 일치도 점수↓
print(f"{WORKER_TAG} 본문 일치도 NLI 모델 로딩 중...")
NLI_MODEL_NAME = "MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli"
nli_tokenizer = AutoTokenizer.from_pretrained(NLI_MODEL_NAME)
nli_model = AutoModelForSequenceClassification.from_pretrained(NLI_MODEL_NAME)
nli_model.eval()
# 라벨 매핑 확인 (XNLI 표준: {0:'entailment', 1:'neutral', 2:'contradiction'})
NLI_ID2LABEL = nli_model.config.id2label
print(f"{WORKER_TAG} NLI 라벨 매핑: {NLI_ID2LABEL}")
print(f"{WORKER_TAG} 본문 일치도 NLI 모델 로딩 완료!")

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

    # ── worker_id 컬럼: 어떤 Worker가 이 기사를 분석했는지 기록 ──
    # 대시보드에서 "Worker-1이 처리", "Worker-2가 처리" 등으로 표시하기 위해 사용
    # 부하 분산 시각화(Worker별 처리 건수 막대 그래프)에도 활용
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN worker_id TEXT")
    except sqlite3.OperationalError:
        pass  # 이미 존재하는 컬럼 — 무시

    # ── processing_time 컬럼: 기사 1건 처리에 걸린 시간 (초 단위, 소수점) ──
    # 크롤링 + 분석 + DB 저장까지의 전체 소요 시간을 기록한다.
    # 대시보드에서 평균 처리 시간, 초당 처리량(throughput) 계산에 사용
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN processing_time REAL")
    except sqlite3.OperationalError:
        pass  # 이미 존재하는 컬럼 — 무시

    # ── cache_stats 컬럼: 처리 완료 시점의 캐시 적중률 스냅샷 (JSON) ──
    # 논문 abstract "캐싱 최적화" 효과를 대시보드에서 시각화하기 위한 데이터
    # 형식: {"nli":{"hit_rate_pct":80.5,...}, "sentiment":{...}, "source":{...}}
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN cache_stats TEXT")
    except sqlite3.OperationalError:
        pass

    # ── user_label 컬럼: 분석 요청자 프로필 라벨 (E1, 경량 사용자화) ──
    # 향후 OAuth/JWT 기반 정식 user_id로 확장 가능. 지금은 자유 입력 텍스트.
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN user_label TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN user_label TEXT")
    except sqlite3.OperationalError:
        pass

    # ── user_id 컬럼 (Phase G — 사용자 격리): 인증된 회원 식별자 ──
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass

    # ── jobs 테이블: 분석 요청의 생명주기를 추적 ──
    # API가 job을 생성(pending)하고, Worker가 상태를 갱신(processing→done/failed)한다.
    # 대시보드는 jobs 기준으로 진행률을 표시한다.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            queued_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error_message TEXT,
            result_id INTEGER,
            retry_count INTEGER DEFAULT 0
        )
    ''')

    # 마이그레이션: 기존 jobs 테이블에 retry_count가 없으면 추가
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN retry_count INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

# ========== 뉴스 크롤링 ==========

# ── HTTP 세션 재사용 (성능 최적화) ──
# requests.Session()을 Worker 시작 시 1번만 생성하고 모든 크롤링에 재사용한다.
# 매번 새 TCP 연결을 맺지 않고 기존 연결을 재활용(Keep-Alive)하므로
# 같은 서버에 여러 번 요청할 때 연결 수립 시간을 절약한다.
http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/91.0 Mobile Safari/537.36"
})

# ── 크롤링 설정 상수 ──
CRAWL_TIMEOUT = 10          # HTTP 응답 대기 최대 10초 (무한 대기 방지)
CRAWL_MAX_RETRIES = 2       # 크롤링 내부 재시도 최대 2회 (네트워크 일시 오류 대응)


def convert_to_mobile_url(url):
    """네이버 뉴스 URL을 모바일 버전으로 변환한다.

    모바일 페이지(m.news.naver.com)는 PC 페이지보다 HTML이 가볍고
    JS 렌더링 의존도가 낮아 크롤링 속도가 빨라진다.

    변환 규칙:
    - n.news.naver.com → m.news.naver.com
    - news.naver.com → m.news.naver.com
    - /mnews/ → /mnews/ (이미 모바일이면 그대로)

    Returns: 변환된 URL 문자열
    """
    # 이미 모바일 URL이면 그대로 반환
    if "m.news.naver.com" in url:
        return url
    # n.news.naver.com/mnews/... 또는 n.news.naver.com/article/... 패턴
    url = url.replace("n.news.naver.com", "m.news.naver.com")
    url = url.replace("news.naver.com", "m.news.naver.com")
    # 중복 치환 방지: m.m.news... 가 되지 않도록
    url = url.replace("m.m.news", "m.news")
    # 모바일 URL은 /mnews/ 가 빠지는 형식: m.news.naver.com/article/... 이 정상
    # 원본이 /mnews/article/이면 /article/로 정리
    url = url.replace("m.news.naver.com/mnews/article/", "m.news.naver.com/article/")
    # comment 경로도 모바일 본문으로 리다이렉트되어야 정상 크롤링됨
    url = url.replace("/article/comment/", "/article/")
    return url


def crawl_naver_news(url):
    """네이버 뉴스 전용 크롤러 — BS4로 기사 본문 + 원본 언론사명 추출

    newspaper3k는 네이버 뉴스의 JS 렌더링 본문을 못 가져오므로,
    requests + BeautifulSoup으로 직접 추출한다.
    모바일 URL로 변환하여 가벼운 HTML을 받아 처리 속도를 높인다.

    Returns: (제목, 본문, 원본 언론사명)
    """
    # 모바일 URL로 변환 (HTML이 가볍고 크롤링이 빠름)
    mobile_url = convert_to_mobile_url(url)
    print(f"{WORKER_TAG} 모바일 URL 사용: {mobile_url}")

    # ── 크롤링 내부 재시도 (최대 CRAWL_MAX_RETRIES회) ──
    # 네트워크 일시 오류(타임아웃, 연결 끊김 등)에 대응
    # 큐 레벨 재시도(retry_to_queue)와 별개로, HTTP 요청 자체를 재시도
    last_error = None
    for attempt in range(1, CRAWL_MAX_RETRIES + 1):
        try:
            resp = http_session.get(mobile_url, timeout=CRAWL_TIMEOUT)
            resp.raise_for_status()
            break  # 성공 시 루프 탈출
        except Exception as e:
            last_error = e
            if attempt < CRAWL_MAX_RETRIES:
                print(f"{WORKER_TAG} 크롤링 재시도 {attempt}/{CRAWL_MAX_RETRIES}: {mobile_url}")
                time.sleep(1)  # 1초 대기 후 재시도
            else:
                raise last_error  # 최종 실패 — 상위 호출자에게 예외 전파

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

    - 네이버 뉴스: 모바일 URL 변환 + BS4 직접 파싱 + 내부 재시도
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
    domain = urlparse(url).netloc.replace("www.", "")
    return article.title, article.text, domain

# ========== 신뢰도 분석 함수들 ==========

def strip_quoted_text(text):
    """따옴표(" ", ' ', 「 」, 『 』) 안의 인용문을 제거한다.

    자극적 단어가 인용문 안에 있으면 기자의 표현이 아니라
    취재원의 발언이므로 자극성 감점 대상에서 제외해야 한다.
    예: "충격적 사건"이라고 밝혔다 → "충격적 사건" 부분이 제거됨

    Returns: 인용문이 제거된 텍스트
    """
    # 큰따옴표 (유니코드 스마트 쿼트 포함)
    text = re.sub(r'\u201c[^\u201d]*\u201d', '', text)  # "…"
    text = re.sub(r'"[^"]*"', '', text)                  # "…"
    # 작은따옴표 (유니코드 스마트 쿼트 포함)
    text = re.sub(r'\u2018[^\u2019]*\u2019', '', text)  # '…'
    text = re.sub(r"'[^']*'", '', text)                  # '…'
    # 꺾쇠 인용 부호
    text = re.sub(r'「[^」]*」', '', text)
    text = re.sub(r'『[^』]*』', '', text)
    return text


def is_negated_in_context(keyword, body, window=10):
    """본문에서 키워드 주변(앞뒤 window글자)에 부정어가 있는지 확인한다.

    제목 키워드가 본문에 등장하더라도 부정어와 함께 쓰이면
    제목과 본문의 의미가 반대일 수 있으므로, 해당 매칭을 취소한다.
    예: 제목 "정부, 인상 확정" / 본문 "인상을 하지 않겠다고 발표" → 매칭 취소

    한국어는 부정어가 키워드에서 수 글자 떨어질 수 있으므로
    기본 윈도우를 10글자로 설정한다 (예: "인상을 하지 않겠다"에서 인상↔않 거리 ≈ 7)

    Args:
        keyword: 확인할 키워드
        body: 본문 텍스트
        window: 키워드 앞뒤로 확인할 글자 수 (기본 10)
    Returns: True면 부정어가 근처에 있음 (매칭 취소 대상)
    """
    NEGATION_WORDS = ['않', '아니', '부인', '거부', '반박', '부정', '없', '못']

    # 키워드가 본문에서 등장하는 모든 위치를 찾음
    for match in re.finditer(re.escape(keyword), body):
        start = match.start()
        end = match.end()
        # 키워드 앞뒤 window글자 범위의 텍스트를 추출
        context_start = max(0, start - window)
        context_end = min(len(body), end + window)
        context = body[context_start:context_end]

        # 부정어가 이 범위 안에 있으면 부정 문맥으로 판단
        for neg in NEGATION_WORDS:
            if neg in context:
                return True

    return False


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
    1. 따옴표·괄호·말줄임표 등 모든 특수기호 제거
    2. 조사/어미/기호 등 불용어 패턴을 제거
    3. 2글자 이상의 단어만 키워드로 인정
    4. 숫자+단위 조합(200억원 등)도 키워드로 포함

    [F1 개선] 따옴표(", ', " ", ' '), 말줄임표(…, ...), 물결(~), 콜론(:),
    하이픈(-)을 토큰화 전에 모두 공백으로 치환하여 키워드가 깨끗이 분리되도록 함.
    """
    # 다양한 괄호/따옴표/특수기호를 공백으로 치환 (이전: 일부만)
    cleaned = re.sub(
        r'[\[\]()「」『』【】<>"\'“”‘’…—–·:;,?!]',
        ' ', title
    )
    # 한자(漢字) 영역도 공백으로 치환 (예: 前총리 → 총리, 李대통령 → 대통령)
    cleaned = re.sub(r'[一-鿿]+', ' ', cleaned)
    # 말줄임표 처리 (위에서 …는 처리됐지만 ... 도 대비)
    cleaned = cleaned.replace('...', ' ')
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
        # F1: '서'(처소격), '에서'도 추가하여 "정부서" → "정부" 매칭 가능
        word = re.sub(
            r'(은|는|이|가|을|를|의|에|와|과|로|도|만|까지|부터|에서|에게|으로|서|이다|했다|한다|된다|되는|하는|에는)$',
            '', token
        )
        # 2글자 이상이고 불용어가 아닌 단어만 키워드로 인정
        if len(word) >= 2 and word not in stopwords:
            keywords.append(word)

    return keywords


def keyword_in_body(keyword, body):
    """키워드가 본문에 있는지 점진적 stem fallback으로 검사한다.

    [F1 개선] 어미 자르기가 정확하지 않아 매칭이 실패할 수 있으므로
    실패 시 더 짧은 stem(마지막 1자, 2자 자른 형태)으로 재시도하여
    "확정한" → "확정" 같은 부분 매칭을 가능하게 한다.
    """
    if not keyword or not body:
        return False
    # 1차: 그대로 매칭
    if keyword in body:
        return True
    # 2차: 마지막 1글자 잘라서 매칭 (3글자 이상일 때만)
    if len(keyword) >= 3 and keyword[:-1] in body:
        return True
    # 3차: 마지막 2글자 잘라서 매칭 (4글자 이상일 때만)
    if len(keyword) >= 4 and keyword[:-2] in body:
        return True
    return False

# ========== 캐시 계층 (논문 abstract 명시: "캐싱 최적화") ==========
# Worker 컨테이너별 로컬 인메모리 LRU 캐시.
# 같은 텍스트(예: 자주 인용되는 본문 리드 또는 같은 출처 도메인)가 반복 입력될 때
# 모델 추론을 생략하여 처리 시간과 GPU/CPU 부담을 줄인다.
#
# [캐시 대상]
# 1. NLI 추론 — (본문 리드 해시, 제목 해시) 페어 키
# 2. 감성 분석 추론 — 본문 리드 해시 키
# 3. 출처 분류 — (URL, source_name) 페어 키 (도메인 매핑 룩업 캐시)
#
# [성능 효과]
# 같은 도메인의 다른 기사가 들어와도 출처 분류는 즉시 캐시 히트.
# 같은 본문이 재분석되거나 비슷한 리드를 가진 기사가 많을 때 NLI/감성 캐시 히트.
#
# 컨테이너 메모리 제약(LRU maxsize)으로 무제한 누적 방지.
NLI_CACHE_MAX = 512
SENT_CACHE_MAX = 512
SRC_CACHE_MAX = 256


def _hash_text(text):
    """긴 텍스트를 짧은 해시 키로 변환 (lru_cache 메모리 절약)"""
    return hashlib.md5(text.encode('utf-8', errors='ignore')).hexdigest()


def get_cache_stats():
    """3개 캐시(NLI/감성/출처)의 적중 통계를 dict로 반환.

    발표/시연에서 "캐싱 최적화" 효과를 정량적으로 보여주기 위한 헬퍼.
    각 캐시의 hits/misses/currsize/maxsize를 노출.
    """
    stats = {}
    for name, fn in [("nli", _nli_inference_cached),
                     ("sentiment", _sentiment_inference_cached),
                     ("source", _source_classify_cached)]:
        ci = fn.cache_info()
        total = ci.hits + ci.misses
        hit_rate = round(ci.hits / total * 100, 1) if total > 0 else 0.0
        stats[name] = {
            "hits": ci.hits, "misses": ci.misses,
            "currsize": ci.currsize, "maxsize": ci.maxsize,
            "hit_rate_pct": hit_rate,
        }
    return stats


@lru_cache(maxsize=NLI_CACHE_MAX)
def _nli_inference_cached(premise_hash, hypothesis_hash, premise, hypothesis):
    """NLI 추론을 캐시한다. 키는 텍스트 해시 (직접 텍스트로 캐시하면 메모리↑).

    Returns: (entailment_p, neutral_p, contradiction_p)
    """
    inputs = nli_tokenizer(
        premise, hypothesis,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=256
    )
    with torch.no_grad():
        outputs = nli_model(**inputs)
    probs = torch.softmax(outputs.logits, dim=-1)[0]

    label_probs = {NLI_ID2LABEL[i].lower(): round(probs[i].item(), 4)
                   for i in range(len(probs))}
    return (
        label_probs.get("entailment", 0.0),
        label_probs.get("neutral", 0.0),
        label_probs.get("contradiction", 0.0),
    )


def analyze_nli_entailment(premise, hypothesis):
    """제목이 본문 핵심을 논리적으로 포함하는지 NLI 모델로 판단한다.

    [논문 정렬]
    논문 2.2: "자연어 추론(NLI) 기술로 제목이 본문 핵심을 논리적으로 포함하는지를
    따져보며, 내용이 본문과 어긋나거나 무관하면 점수를 크게 깎는다 [Bowman 2015]"

    Args:
        premise: 본문 리드 3문장 (전제 — 무엇이 사실인가)
        hypothesis: 제목 (가설 — 제목이 사실로 따라오는가)

    Returns:
        (score, details)
        score: 0~100 (entailment 비율이 높을수록 높음, contradiction이 높으면 큰 감점)
        details: {"entailment": 0.85, "neutral": 0.10, "contradiction": 0.05}
    """
    # ── 빈 입력 폴백 ──
    if not premise or not hypothesis:
        return 50.0, {"entailment": 0.33, "neutral": 0.33, "contradiction": 0.33}

    try:
        # 캐시된 추론 호출 (해시 키 기반 LRU)
        ph = _hash_text(premise)
        hh = _hash_text(hypothesis)
        ent, neu, con = _nli_inference_cached(ph, hh, premise, hypothesis)

        # ── 점수 산식 ──
        # entailment 강하면 100점에 가깝게, contradiction 강하면 0점에 가깝게
        # neutral은 중립(50점) 기여 — 무관한 제목은 중간 점수
        # 직관: 제목이 본문 핵심을 논리적으로 포함 → 만점, 어긋남 → 큰 감점
        nli_score = (ent * 100.0) + (neu * 50.0) + (con * 0.0)
        nli_score = max(0.0, min(100.0, nli_score))

        return round(nli_score, 1), {
            "entailment": ent, "neutral": neu, "contradiction": con
        }
    except Exception as e:
        print(f"{WORKER_TAG} NLI 추론 오류: {e}")
        return 50.0, {"entailment": 0.33, "neutral": 0.33, "contradiction": 0.33}


def analyze_content_similarity(title, body):
    """지표 1: 본문 일치도 (45%) — NLI 메인 + 코사인 유사도/키워드 매칭 보조

    [논문 정렬]
    논문 2.2의 NLI 기반 분석을 메인 신호로 사용하면서, 기존 TF-IDF 코사인 유사도와
    키워드 매칭은 보조 신호로 결합 → 모델 단일 의존도 완화 + XAI 근거 풍부화.

    [결합 비율]
    1. NLI entailment (60%): 제목 ↔ 본문 리드의 논리적 함의 관계
    2. TF-IDF 코사인 유사도 (20%): 어휘 수준 의미 유사도
    3. 키워드 매칭 (20%): 제목 핵심 명사의 본문 등장 비율 (부정어 근접 체크 포함)

    Returns: (최종 점수, 상세 정보 dict)
    """
    # ── 빈 입력 처리 ──
    if not title or not body:
        return 0.0, {
            "keywords": [], "matched": [], "negated": [],
            "cosine_raw": 0.0, "nli": {"entailment": 0, "neutral": 0, "contradiction": 0},
            "nli_score": 0, "cosine_score": 0, "keyword_score": 0
        }

    # ── 본문 리드 3문장 추출 (NLI/TF-IDF 공통 입력) ──
    lead = extract_lead_sentences(body, n=3)
    if not lead:
        lead = body[:200]

    # ── 1단계: NLI 함의 분석 (메인 신호, 60%) ──
    nli_score, nli_detail = analyze_nli_entailment(lead, title)

    # ── 2단계: TF-IDF 코사인 유사도 (보조, 20%) ──
    try:
        vectorizer = TfidfVectorizer()
        vectors = vectorizer.fit_transform([title, lead])
        cosine_raw = cosine_similarity(vectors[0], vectors[1])[0][0]
        cosine_score = cosine_raw * 100
    except Exception:
        cosine_raw = 0.0
        cosine_score = 0.0

    # ── 3단계: 키워드 매칭 (보조, 20%) — 부정어 근접 체크 포함 ──
    keywords = extract_title_keywords(title)
    matched_list = []
    negated_list = []

    if keywords:
        for kw in keywords:
            # F1: keyword_in_body로 stem fallback 매칭
            if keyword_in_body(kw, body):
                if is_negated_in_context(kw, body):
                    negated_list.append(kw)
                else:
                    matched_list.append(kw)
        keyword_score = (len(matched_list) / len(keywords)) * 100
    else:
        keyword_score = 50.0  # 키워드 추출 실패 시 중립값

    # ── 4단계: 가중 결합 (NLI 60% + 코사인 20% + 키워드 20%) ──
    final_score = (nli_score * 0.6) + (cosine_score * 0.2) + (keyword_score * 0.2)
    final_score = max(0, min(100, final_score))

    details = {
        "keywords": keywords,                   # 제목 추출 키워드
        "matched": matched_list,                 # 본문 매칭 키워드
        "negated": negated_list,                 # 부정어로 매칭 취소된 키워드
        "cosine_raw": round(cosine_raw, 4),     # 코사인 유사도 원본 (0~1)
        "nli": nli_detail,                       # NLI 라벨별 확률
        "nli_score": round(nli_score, 1),       # NLI 점수 (0~100)
        "cosine_score": round(cosine_score, 1), # 코사인 점수 (0~100)
        "keyword_score": round(keyword_score, 1) # 키워드 점수 (0~100)
    }

    return round(final_score, 1), details

def _run_sentiment_model(texts):
    """내부 함수: 텍스트 리스트를 배치로 모델에 입력하여 3-class 확률을 반환한다.

    [최적화 포인트]
    - torch.no_grad(): 그래디언트 계산 비활성화 → 메모리 40%↓, 속도 20~30%↑
    - 배치 토크나이징: 여러 텍스트를 한번에 인코딩 → GPU/CPU 병렬 처리 활용
    - softmax로 3개 라벨(neutral/positive/negative) 확률을 전부 계산
      (pipeline의 top-1 근사치가 아닌 정확한 3-class 확률)

    Args:
        texts: 분석할 텍스트 리스트 (각 텍스트는 리드 3문장 정도의 짧은 문자열)
    Returns:
        리스트 of dict: [{"neutral": 0.85, "positive": 0.10, "negative": 0.05}, ...]
    """
    # 토크나이징 — padding=True로 배치 내 길이를 통일, truncation으로 512 초과 방지
    inputs = tokenizer(
        texts,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=512
    )

    # 그래디언트 비활성화 상태에서 추론 수행
    with torch.no_grad():
        outputs = model(**inputs)

    # softmax로 3개 라벨의 정확한 확률값 계산 (모델 출력은 logit이므로 변환 필요)
    probs = torch.softmax(outputs.logits, dim=-1)

    # 모델의 id2label 매핑으로 라벨명 연결 (예: {0: 'neutral', 1: 'positive', 2: 'negative'})
    id2label = model.config.id2label
    results = []
    for prob in probs:
        result = {}
        for idx, p in enumerate(prob):
            result[id2label[idx]] = round(p.item(), 4)
        results.append(result)

    return results

@lru_cache(maxsize=SENT_CACHE_MAX)
def _sentiment_inference_cached(lead_hash, lead):
    """감성 분석 모델 추론 결과를 캐시한다 (논문 abstract 명시 "캐싱 최적화").

    같은 본문 리드가 재분석되거나 같은 인용 문단이 여러 기사에 등장할 때
    KR-FinBert-SC 추론을 생략한다.

    Returns: (neutral_p, positive_p, negative_p) — 0~1 사이 확률값
    """
    probs = _run_sentiment_model([lead])[0]
    return (
        probs.get("neutral", 0.0),
        probs.get("positive", 0.0),
        probs.get("negative", 0.0),
    )


def analyze_ai_sentiment(text):
    """자극성/논조 보조지표 — KR-FinBert-SC 모델로 기사 논조를 분류

    이 함수는 뉴스의 진위를 판별하지 않는다.
    기사의 논조(중립/긍정/부정)를 분류하여 자극성 점수 산출에 보조적으로 활용한다.
    규칙 기반 자극적 단어 감지와 50:50으로 결합되어 최종 자극성 점수에 반영된다.

    [최적화 내용]
    - 본문 전체(512자) 대신 리드 3문장만 입력 → 토큰 수 감소로 추론 속도 향상
      (뉴스는 역피라미드 구조라 리드에 핵심 논조가 집중됨)
    - torch.no_grad()로 그래디언트 비활성화 → 메모리·속도 최적화
    - softmax로 3-class 정확한 확률 계산 (pipeline의 근사치 대체)
    - LRU 캐시: 같은 본문 리드 재추론 생략 (논문 abstract "캐싱 최적화" 명시 충족)

    Returns: (점수, 상세 정보 dict)
        상세 정보: 각 라벨별 확률값 (대시보드에서 근거로 표시)
    """
    try:
        # 리드 3문장 추출 — 본문 전체보다 짧아서 토크나이징+추론이 빠름
        lead = extract_lead_sentences(text, n=3)
        if not lead or len(lead.strip()) < 10:
            lead = text[:300]  # 리드 추출 실패 시 앞 300자 폴백

        # 캐시된 추론 호출 (해시 키 기반 LRU)
        lead_h = _hash_text(lead)
        neutral_p, positive_p, negative_p = _sentiment_inference_cached(lead_h, lead)
        probs = {"neutral": neutral_p, "positive": positive_p, "negative": negative_p}

        # ── 라벨별 확률값을 백분율로 변환 (대시보드 표시용) ──
        sentiment_detail = {
            "neutral": round(probs.get("neutral", 0) * 100, 1),
            "positive": round(probs.get("positive", 0) * 100, 1),
            "negative": round(probs.get("negative", 0) * 100, 1),
        }

        # ── 최종 점수 산출: 중립일수록 높은 점수 (객관적 보도) ──
        neutral_p = probs.get("neutral", 0)
        positive_p = probs.get("positive", 0)
        negative_p = probs.get("negative", 0)

        # 중립 확률이 높으면 높은 점수, 부정이 높으면 낮은 점수
        final = round(neutral_p * 100 + positive_p * 70 + negative_p * 30, 1)
        final = max(0, min(100, final))

        return final, sentiment_detail
    except Exception as e:
        print(f"{WORKER_TAG} AI 감성분석 오류: {e}")
        return 50.0, {"neutral": 33.3, "positive": 33.3, "negative": 33.3}

def analyze_ai_sentiment_batch(texts):
    """여러 기사의 본문을 배치로 논조 분석한다 (자극성/논조 보조지표).

    TODO: 현재 Worker의 process_message()는 기사를 1건씩 처리하므로
    이 배치 함수가 실제로 호출되지 않는다.
    Worker가 큐에서 여러 메시지를 모아 배치 처리하는 구조로 전환하면
    analyze_provocative() 내부에서 이 함수를 활용할 수 있다.

    Args:
        texts: 기사 본문 리스트
    Returns:
        리스트 of (점수, 상세 정보 dict) — analyze_ai_sentiment와 동일한 형식
    """
    # 각 본문에서 리드 3문장 추출
    leads = []
    for text in texts:
        lead = extract_lead_sentences(text, n=3)
        if not lead or len(lead.strip()) < 10:
            lead = text[:300]
        leads.append(lead)

    # 배치 추론 — 모든 리드를 한번에 모델에 입력
    try:
        all_probs = _run_sentiment_model(leads)
    except Exception as e:
        print(f"{WORKER_TAG} 배치 감성분석 오류: {e}")
        return [(50.0, {"neutral": 33.3, "positive": 33.3, "negative": 33.3})] * len(texts)

    # 각 기사별 점수·상세 정보 생성
    results = []
    for probs in all_probs:
        sentiment_detail = {
            "neutral": round(probs.get("neutral", 0) * 100, 1),
            "positive": round(probs.get("positive", 0) * 100, 1),
            "negative": round(probs.get("negative", 0) * 100, 1),
        }
        neutral_p = probs.get("neutral", 0)
        positive_p = probs.get("positive", 0)
        negative_p = probs.get("negative", 0)
        final = round(neutral_p * 100 + positive_p * 70 + negative_p * 30, 1)
        final = max(0, min(100, final))
        results.append((final, sentiment_detail))

    return results

def analyze_provocative(title, body, source_score=0):
    """지표 2: 자극성 분석 (35%) — 단어 기반 분석 + AI 논조 보조지표 결합

    [분석 방법]
    1. 단어 기반 분석 (50%): 카테고리별 자극적 표현 + 문장부호 남용 감지
    2. AI 보조지표 (50%): KR-FinBert-SC로 본문의 논조(긍정/부정/중립) 분류
    3. 최종 점수 = (단어 기반 × 0.5) + (AI 보조 × 0.5)

    [단어 기반 분석 세부]
    - 자극적 단어를 4가지 카테고리로 분류하고 카테고리별 가중치 적용
    - 제목에 등장하는 자극적 단어는 가중치 2배 (클릭 유도 목적이 강함)
    - 본문 길이(글자 수) 대비 비율로 정규화하여 긴 기사가 불이익받지 않도록 함
    - 느낌표(!!)/물음표(??) 연속 사용도 감점 대상에 포함

    [개선 사항]
    - 인용문(" " 등) 안의 자극적 단어는 감점하지 않음 (취재원 발언이므로)
    - "속보", "단독"은 주요 언론사(85점) 기사에서는 뉴스 용어로 간주하여 면제

    Args:
        title: 기사 제목
        body: 기사 본문
        source_score: 출처 신뢰도 점수 (주요 언론사 면제 판단용, 기본 0)

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
        # "속보", "단독"은 제거 — 주요 언론사에서는 뉴스 용어로 사용하므로
        # source_score가 85점(주요 언론사)이면 아래 NEWS_TERMS로 면제 처리
        'sensational': {
            'weight': 1.2,
            'words': [
                '충격', '경악', '소름', '폭로', '특종',
                '발칵', '난리', '파문', '후폭풍', '대참사',
                '충격적', '소름끼치는', '믿기힘든', '경악스러운',
                '논란', '파장', '급반전', '반전',
            ]
        },
        # 공포형: 불안·공포를 조성하는 표현 (가중치 1.3)
        # "속보"는 NEWS_TERMS로 분리 — 주요 언론사 면제 대상
        'fear': {
            'weight': 1.3,
            'words': [
                '긴급', '비상', '패닉', '공포', '참사',
                '전율', '아찔', '섬뜩', '끔찍', '절규',
                '날벼락', '치명적', '위험', '경고', '대재앙',
            ]
        },
    }

    # ── "속보", "단독"은 주요 언론사(85점)이면 면제, 아니면 감점 대상 ──
    # 주요 언론사에서는 정당한 뉴스 용어로 사용하지만,
    # 출처 불명 매체에서는 클릭 유도 목적일 수 있으므로 구분
    NEWS_TERMS = ['속보', '단독']
    is_major_source = (source_score >= 85)

    # ── 0단계: 인용문 제거 ──
    # 따옴표 안의 텍스트는 취재원의 발언이므로 자극성 감점 대상에서 제외
    # 예: "충격적 사건"이라고 밝혔다 → "충격적 사건" 부분을 제거
    title_for_check = strip_quoted_text(title)
    body_for_check = strip_quoted_text(body)

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
            # 인용문이 제거된 텍스트에서 자극적 단어를 검색
            title_hits = len(re.findall(re.escape(word), title_for_check))
            body_hits = len(re.findall(re.escape(word), body_for_check))

            if title_hits > 0 or body_hits > 0:
                # 감지된 단어를 카테고리별로 기록
                if cat_label not in detected_words:
                    detected_words[cat_label] = []
                detected_words[cat_label].append(word)

            weighted_hit_count += cat_weight * (title_hits * TITLE_MULTIPLIER + body_hits)

    # ── 1-1단계: "속보", "단독" 처리 ──
    # 주요 언론사(85점)이면 뉴스 용어로 간주하여 면제
    # 그 외에는 선정형(가중치 1.2)으로 감점
    for term in NEWS_TERMS:
        title_hits = len(re.findall(re.escape(term), title_for_check))
        body_hits = len(re.findall(re.escape(term), body_for_check))

        if title_hits > 0 or body_hits > 0:
            if is_major_source:
                # 주요 언론사에서는 뉴스 용어이므로 감점하지 않음
                # 대신 근거에는 기록 (면제된 사실을 표시)
                if '뉴스용어(면제)' not in detected_words:
                    detected_words['뉴스용어(면제)'] = []
                detected_words['뉴스용어(면제)'].append(term)
            else:
                # 주요 언론사가 아니면 선정형으로 감점
                if '선정' not in detected_words:
                    detected_words['선정'] = []
                detected_words['선정'].append(term)
                weighted_hit_count += 1.2 * (title_hits * TITLE_MULTIPLIER + body_hits)

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
    # [F3 보정] 이전: max(0, 100 - ratio*33.3) → 자극적 단어 0건이면 100점 만점.
    # 실제 데이터에서 47.8%가 100점에 몰려 변별력이 부족했음.
    # 새 산식: 95점에서 시작(약간의 패널티) + multiplier 33.3 → 50으로 강화
    # → 자극적 단어가 적으면 90~95점, 많으면 50점 이하로 분포가 넓어짐
    word_score = max(0, 95 - (ratio_per_100 * 50))

    # ── 5단계: AI 감성분석 점수와 결합 ──
    ai_score, ai_detail = analyze_ai_sentiment(body)
    final_score = (word_score * 0.5) + (ai_score * 0.5)

    # ── 상세 정보를 함께 반환 (대시보드에서 분석 근거로 표시) ──
    details = {
        "detected": detected_words,                   # 카테고리별 감지된 단어
        "ratio": round(ratio_per_100, 2),              # 자극적 표현 비율 (%)
        "ai": ai_detail,                               # AI 감성분석 라벨별 확률
        # F4: 점수 분해 — 단어 점수와 AI 점수를 분리하여 산출식 가시화
        "word_score": round(word_score, 1),
        "ai_score": round(ai_score, 1),
    }

    return round(final_score, 1), details

def analyze_source(url, source_name=""):
    """출처 신뢰도 분석 진입점.
    캐시 효율을 높이기 위해 (도메인, source_name) 페어를 캐시 키로 사용한다.
    같은 언론사의 다른 기사도 즉시 캐시 히트.
    """
    domain = urlparse(url).netloc.replace("www.", "") if url else ""
    return _source_classify_cached(domain, source_name or "")


@lru_cache(maxsize=SRC_CACHE_MAX)
def _source_classify_cached(domain, source_name):
    """지표 3: 출처 신뢰도 (20%) — 원본 언론사명 기반 신뢰도 판정

    규칙 기반 분석으로, AI 모델은 사용하지 않는다.
    크롤링 시 추출한 원본 언론사명을 사전 정의된 목록과 대조하여 점수를 부여한다.

    [판정 기준 — 3단계]
    1. 주요 언론사 (85점): 통신사, 지상파, 종합일간지 등 공신력 있는 매체
    2. 등록 인터넷 매체 (65점): 인터넷 신문, 경제지, 전문지 등 등록된 매체
    3. 출처 불명 (35점): 언론사명을 확인할 수 없거나 미등록 매체

    Args:
        url: 기사 URL (언론사명 추출 실패 시 도메인 기반 폴백용)
        source_name: 크롤링 시 추출한 원본 언론사명 (예: "연합뉴스", "KBS")

    TODO: MAJOR_SOURCES, REGISTERED_SOURCES를 별도 config 파일(JSON/YAML)로 분리하면
    코드 수정 없이 언론사 목록을 관리할 수 있다.
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

    # ── 2단계: 도메인으로 폴백 ──
    # (wrapper analyze_source가 도메인을 키로 전달하므로 변수 이름은 domain)
    for name in MAJOR_SOURCES:
        if name in domain:
            return 85.0, "주요 언론사"
    for name in REGISTERED_SOURCES:
        if name in domain:
            return 65.0, "등록 매체"

    # ── 3단계: 출처 불명 ──
    return 35.0, "출처 불명"

def calculate_total_score(content, provocative, source):
    """종합 점수: 본문일치도(45%) + 자극성분석(35%) + 출처신뢰도(20%)
    규칙 기반 분석과 AI 보조지표를 결합한 가중 평균 점수.
    """
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

    Returns: True면 저장 성공, False면 저장 실패
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
            # worker_id: 이 기사를 처리한 Worker의 ID (분산 처리 추적용)
            cursor.execute('''
                INSERT INTO analysis_results
                (url, title, body, content_score, provocative_score, source_score,
                 total_score, grade, status, analyzed_at,
                 matched_keywords, detected_provocative, ai_sentiment, source_name,
                 worker_id, processing_time, cache_stats, user_label, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                result['url'], result['title'], result['body'][:500],
                result['content'], result['provocative'], result['source'],
                result['total'], result['grade'], result.get('status', 'done'),
                result['analyzed_at'],
                result.get('matched_keywords', ''),
                result.get('detected_provocative', ''),
                result.get('ai_sentiment', ''),
                result.get('source_name', ''),
                WORKER_ID,                              # 현재 Worker의 ID
                result.get('processing_time', None),    # 처리 소요 시간 (초)
                result.get('cache_stats', None),        # 캐시 적중률 스냅샷 (JSON)
                result.get('user_label', ''),           # 분석 요청자 프로필 라벨
                result.get('user_id')                   # Phase G — 인증된 회원 식별자
            ))
            conn.commit()
            return True  # 저장 성공

        except sqlite3.OperationalError as e:
            # "database is locked" 에러: 다른 Worker가 쓰기 중
            if "locked" in str(e) and attempt < max_retries:
                print(f"{WORKER_TAG} DB 잠금 감지, {attempt}/{max_retries} 재시도 (3초 후)")
                time.sleep(3)
            else:
                # 3회 모두 실패하거나 다른 종류의 에러면 로그 출력
                print(f"{WORKER_TAG} DB 저장 실패 (url={result['url']}): {e}")
                return False  # 저장 실패

        except Exception as e:
            print(f"{WORKER_TAG} DB 저장 중 예상치 못한 오류 (url={result['url']}): {e}")
            return False  # 저장 실패

        finally:
            # 예외 발생 여부와 관계없이 연결을 반드시 닫아 잠금 해제
            if conn:
                conn.close()

    print(f"{WORKER_TAG} DB 저장 최종 실패 — {max_retries}회 재시도 모두 실패 (url={result['url']})")
    return False

# ========== 메인: 큐에서 기사 꺼내서 분석 ==========

def is_already_analyzed(url, user_id=None):
    """해당 URL이 이미 본인의 분석 결과로 저장되었는지 확인한다 (Phase G 격리).

    [중요] user_id별로 격리: 다른 사용자가 같은 URL을 분석했어도 본인은 새로 분석.
    이렇게 하지 않으면 본인 분석 이력이 비어있는데도 "이미 분석됨"으로 skip되어
    본인 분석 페이지에서 결과를 볼 수 없게 된다.

    status='done'인 본인 결과만 중복으로 간주.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute(
                "SELECT COUNT(*) FROM analysis_results WHERE url = ? AND status = 'done' AND user_id IS NULL",
                (url,)
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM analysis_results WHERE url = ? AND status = 'done' AND user_id = ?",
                (url, user_id)
            )
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        # DB 조회 실패 시에는 안전하게 분석 진행 (중복보다 누락이 더 나쁨)
        return False

def update_job_status(job_id, status, error_message=None, result_id=None):
    """jobs 테이블의 해당 job을 상태 갱신한다.
    job_id 기반으로 정확한 job을 추적한다.
    pending → processing → done/failed 순서로 상태가 전이된다.

    job_id가 None이면 (구버전 메시지 호환) 아무것도 하지 않는다.
    """
    if job_id is None:
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cursor = conn.cursor()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if status == 'processing':
            cursor.execute(
                "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
                (status, now, job_id)
            )
        elif status in ('done', 'failed'):
            cursor.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error_message = ?, result_id = ? WHERE id = ?",
                (status, now, error_message, result_id, job_id)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"{WORKER_TAG} jobs 상태 갱신 실패 (job_id={job_id}, status={status}): {e}")

# ========== 장애 대응: 재시도 로직 ==========
# 크롤링이 실패하면 바로 포기하지 않고, 최대 MAX_RETRY_COUNT회까지 재시도한다.
# 재시도 횟수는 메시지 JSON에 retry_count 필드로 추적한다.
# 재시도 시 메시지를 다시 큐에 넣으면, 다른 Worker가 가져갈 수도 있다.
# → 한 Worker가 죽어도 다른 Worker가 이어서 처리하는 장애 대응 구조
MAX_RETRY_COUNT = 3  # 최대 재시도 횟수

def retry_to_queue(ch, method, data, reason):
    """크롤링/분석 실패 시 재시도를 위해 메시지를 다시 큐에 넣는다.

    동작 방식:
    1. 현재 메시지의 retry_count를 확인
    2. MAX_RETRY_COUNT 미만이면 retry_count를 1 증가시켜 다시 큐에 발행
    3. MAX_RETRY_COUNT 이상이면 최종 실패로 처리 (DB에 failed 기록)

    이렇게 하면 한 Worker가 크래시해도 RabbitMQ가 메시지를 다른 Worker에게
    자동으로 재분배하므로, 시스템 전체가 멈추지 않는다.
    """
    current_retry = data.get('retry_count', 0)
    job_id = data.get('job_id')
    url = data['url']

    if current_retry < MAX_RETRY_COUNT:
        # ── 재시도 가능: 카운트를 올려서 큐에 다시 넣기 ──
        data['retry_count'] = current_retry + 1
        print(f"{WORKER_TAG} 재시도 {data['retry_count']}/{MAX_RETRY_COUNT}: {url} (사유: {reason})")

        # 현재 메시지를 ACK 처리한 뒤 새 메시지로 발행
        # (basic_nack + requeue=True를 쓰면 같은 Worker가 반복 수신할 수 있으므로,
        #  명시적으로 ACK + 재발행하여 다른 Worker에게도 기회를 준다)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        ch.basic_publish(
            exchange='',
            routing_key='news_queue',
            body=json.dumps(data),
            properties=pika.BasicProperties(delivery_mode=2)  # 메시지 영속화
        )
    else:
        # ── 최대 재시도 횟수 초과: 최종 실패 처리 ──
        print(f"{WORKER_TAG} 최대 재시도 횟수({MAX_RETRY_COUNT}회) 초과 — 최종 실패: {url}")
        fail_result = {
            'url': url, 'title': f'실패 ({reason})', 'body': reason[:500],
            'content': 0, 'provocative': 0, 'source': 0,
            'total': 0, 'grade': '분석 불가', 'status': 'failed',
            'analyzed_at': time.strftime("%Y-%m-%d %H:%M:%S")
        }
        save_to_db(fail_result)
        update_job_status(job_id, 'failed', error_message=f"{reason} (재시도 {MAX_RETRY_COUNT}회 실패)")
        ch.basic_ack(delivery_tag=method.delivery_tag)


def process_message(ch, method, properties, body):
    """큐에서 메시지를 받으면 실행되는 콜백 함수.

    ── 장애 대응 핵심 ──
    auto_ack=False 설정으로, Worker가 처리를 완료한 뒤에만 ACK를 보낸다.
    → Worker가 처리 도중 죽으면 RabbitMQ가 메시지를 다른 Worker에게 재분배.
    → 이것이 "한 Worker가 죽어도 시스템이 계속 돌아가는" 핵심 메커니즘.

    메시지 형식: {"job_id": 123, "url": "https://...", "retry_count": 0}
    """
    data = json.loads(body)
    url = data['url']
    job_id = data.get('job_id')  # 구버전 메시지는 job_id가 없을 수 있음
    user_label = data.get('user_label', '') or ''  # 프로필 라벨 (E1, 경량 사용자화)
    user_id = data.get('user_id')  # Phase G — 인증된 회원 식별자 (None=비회원)
    retry_count = data.get('retry_count', 0)  # 현재까지의 재시도 횟수

    # ── 처리 시간 측정 시작 ──
    # time.perf_counter()는 time.time()보다 정밀한 성능 측정용 타이머
    _start_time = time.perf_counter()

    # 어떤 Worker가 어떤 기사를 처리하는지 로그에 명시
    retry_info = f" (재시도 {retry_count}/{MAX_RETRY_COUNT})" if retry_count > 0 else ""
    print(f"{WORKER_TAG} 기사 분석 시작: {url} (job_id={job_id}){retry_info}")

    # job 상태를 processing으로 갱신
    update_job_status(job_id, 'processing')

    # ── 중복 분석 방지: 본인이 이미 분석 완료(done)한 URL이면 건너뛰기 ──
    # Phase G 격리: 다른 사용자가 분석한 동일 URL은 본인 입장에서 새로 분석해야 함
    if is_already_analyzed(url, user_id=user_id):
        print(f"{WORKER_TAG} 이미 분석된 기사입니다 (skip): {url}")
        update_job_status(job_id, 'done')
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    # ── 실제 뉴스 크롤링 — 실패 시 재시도 큐에 넣기 ──
    try:
        title, article_body, source_name = crawl_article(url)
    except Exception as e:
        # 크롤링 실패 → 재시도 로직으로 전달
        # MAX_RETRY_COUNT 미만이면 다시 큐에 넣고, 초과하면 최종 실패 처리
        print(f"{WORKER_TAG} 크롤링 실패: {url} — {e}")
        retry_to_queue(ch, method, data, reason=f"크롤링 실패: {str(e)[:200]}")
        return

    if not article_body or len(article_body.strip()) < 50:
        # 본문이 너무 짧으면 정상적인 분석이 불가능 → 재시도 로직으로 전달
        err_msg = "본문이 너무 짧거나 비어있음"
        print(f"{WORKER_TAG} {err_msg}: {url}")
        retry_to_queue(ch, method, data, reason=err_msg)
        return

    # ── 3가지 지표 분석 (상세 근거 데이터 함께 수집) ──
    # 출처 분석을 먼저 수행: 자극성 분석에서 주요 언론사 면제 판단에 필요
    source, source_class = analyze_source(url, source_name)
    content, content_details = analyze_content_similarity(title, article_body)
    provocative, provocative_details = analyze_provocative(title, article_body, source_score=source)
    total = calculate_total_score(content, provocative, source)
    grade = get_grade(total)

    # ── 처리 시간 측정 완료 ──
    _elapsed = time.perf_counter() - _start_time

    # 캐시 통계 스냅샷 (논문 "캐싱 최적화" 효과 가시화)
    cache_snapshot = json.dumps(get_cache_stats(), ensure_ascii=False)

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
        'processing_time': round(_elapsed, 2),  # 처리 소요 시간 (초, 소수점 2자리)
        'cache_stats': cache_snapshot,  # 처리 완료 시점의 LRU 캐시 적중률 스냅샷
        'user_label': user_label,  # 분석 요청자 프로필 라벨
        'user_id': user_id,  # Phase G — 인증된 회원 식별자 (None=비회원)
    }

    if save_to_db(result):
        # 저장된 result_id를 가져와서 jobs에 연결
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM analysis_results WHERE url = ? ORDER BY id DESC LIMIT 1",
                (url,)
            )
            row = cursor.fetchone()
            conn.close()
            rid = row[0] if row else None
        except Exception:
            rid = None
        update_job_status(job_id, 'done', result_id=rid)
        print(f"{WORKER_TAG} 처리 완료: {_elapsed:.1f}초 — {title}")
        print(f"         본문일치: {content:.1f} | 자극성: {provocative:.1f} | 출처: {source:.1f} | 종합: {total:.1f}점 ({grade})")
        # ── 캐시 적중률 로그 (논문 abstract "캐싱 최적화" 효과 추적) ──
        cs = get_cache_stats()
        print(f"         캐시 — NLI: {cs['nli']['hit_rate_pct']}% (h{cs['nli']['hits']}/m{cs['nli']['misses']}) | "
              f"감성: {cs['sentiment']['hit_rate_pct']}% (h{cs['sentiment']['hits']}/m{cs['sentiment']['misses']}) | "
              f"출처: {cs['source']['hit_rate_pct']}% (h{cs['source']['hits']}/m{cs['source']['misses']})")
        ch.basic_ack(delivery_tag=method.delivery_tag)
    else:
        print(f"{WORKER_TAG} DB 저장 실패 — 메시지 requeue: {url}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

def main():
    """Worker 메인: RabbitMQ에 연결하고 큐에서 메시지를 소비한다.

    ── 분산 병렬 처리의 핵심 동작 원리 ──
    1. 모든 Worker가 동일한 큐(news_queue)에 연결
    2. RabbitMQ가 Round-Robin 방식으로 메시지를 균등 분배
    3. prefetch_count=1: 한 번에 1개씩만 가져와서 처리 → 느린 Worker에 몰리지 않음
    4. auto_ack=False: Worker가 완료 후 ACK → 처리 중 크래시되면 자동 재분배

    ── 확장성 ──
    Worker를 5개, 10개로 늘려도 이 코드를 수정할 필요 없음.
    docker-compose.yml에 worker-4, worker-5를 추가하면 자동으로 큐에 연결되어
    부하가 분산된다. 이것이 메시지 큐 기반 분산 처리의 핵심 장점.
    """
    init_db()

    # ── RabbitMQ 연결 (재시도 루프) ──
    # RabbitMQ가 아직 준비되지 않았을 수 있으므로 연결될 때까지 대기
    while True:
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host='rabbitmq')
            )
            break
        except pika.exceptions.AMQPConnectionError:
            print(f"{WORKER_TAG} RabbitMQ 연결 대기 중... 5초 후 재시도")
            time.sleep(5)

    channel = connection.channel()
    # durable=True: RabbitMQ가 재시작되어도 큐가 유지됨
    channel.queue_declare(queue='news_queue', durable=True)
    # prefetch_count=1: 한 Worker가 1건을 처리 완료해야 다음 메시지를 받음
    # → 처리 속도가 느린 Worker에 메시지가 쌓이지 않아 부하가 균등하게 분배됨
    channel.basic_qos(prefetch_count=1)
    # auto_ack=False (기본값): 명시적 ACK 전까지 메시지가 큐에 남아있음
    # → Worker 장애 시 RabbitMQ가 다른 Worker에게 메시지를 재분배하는 핵심 설정
    channel.basic_consume(queue='news_queue', on_message_callback=process_message)

    print(f"{'='*60}")
    print(f"[시스템] {WORKER_TAG} 연결 완료! (컨테이너: {CONTAINER_ID})")
    print(f"[시스템] 큐 대기 중... Worker를 추가해도 코드 수정 없이 자동 확장됩니다.")
    print(f"[시스템] 장애 대응: auto_ack=False, 재시도 최대 {MAX_RETRY_COUNT}회")
    print(f"{'='*60}")
    channel.start_consuming()

if __name__ == "__main__":
    main()
