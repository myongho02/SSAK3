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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
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
            analyzed_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

# ========== 뉴스 크롤링 ==========

def crawl_naver_news(url):
    """네이버 뉴스 전용 크롤러 — BS4로 기사 본문 직접 파싱
    newspaper3k는 네이버 뉴스의 JS 렌더링 본문을 못 가져오므로,
    requests + BeautifulSoup으로 #dic_area에서 본문을 직접 추출한다.
    모바일/PC URL 모두 그대로 사용 가능.
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

    return title, body

def crawl_article(url):
    """URL에 따라 적절한 크롤러를 선택하여 기사를 가져온다.
    - 네이버 뉴스: BS4 직접 파싱 (JS 렌더링 우회)
    - 기타 사이트: newspaper3k 범용 크롤러 사용
    """
    if "news.naver.com" in url:
        return crawl_naver_news(url)

    # 네이버 외 사이트는 newspaper3k로 크롤링
    article = Article(url, language='ko')
    article.download()
    article.parse()
    return article.title, article.text

# ========== 신뢰도 분석 함수들 ==========

def analyze_content_similarity(title, body):
    """지표 1: 본문 일치도 (45%) — TF-IDF 코사인 유사도로 제목-본문 일치도 측정
    참고: Horne & Adali, "This Just In: Fake News Packs a Lot in Title" (AAAI, 2017)
    """
    if not title or not body:
        return 0.0
    vectorizer = TfidfVectorizer()
    vectors = vectorizer.fit_transform([title, body])
    similarity = cosine_similarity(vectors[0], vectors[1])[0][0]
    return round(similarity * 100, 1)

def analyze_ai_sentiment(text):
    """AI 감성분석 보조 지표 — HuggingFace KR-FinBert-SC 모델 사용

    [동작 원리]
    - 기사 본문을 BERT 모델에 입력하여 긍정/부정/중립 확률값을 받음
    - 중립(neutral)일수록 객관적 보도 → 높은 점수 (신뢰도 높음)
    - 부정(negative)이 강할수록 선정적·자극적 가능성 → 낮은 점수
    - 긍정(positive)도 과도하면 홍보성 기사 가능성 → 중간 점수

    [점수 산출 기준]
    - 중립: 확률값 × 100 (최대 100점)
    - 긍정: 확률값 × 70 (과도한 긍정은 홍보성 가능)
    - 부정: (1 - 확률값) × 50 (부정적일수록 낮은 점수)

    Returns: 0~100 사이의 감성 기반 신뢰도 점수
    """
    try:
        # BERT 모델의 최대 입력 길이는 512 토큰이므로 앞부분만 사용
        result = sentiment_analyzer(text[:512])
        label = result[0]['label']    # 'neutral', 'positive', 'negative'
        score = result[0]['score']    # 해당 라벨의 확률값 (0.0~1.0)

        if label == 'neutral':
            # 중립적 보도일수록 객관적 → 높은 점수
            return round(score * 100, 1)
        elif label == 'positive':
            # 긍정적이어도 과도하면 홍보성 가능 → 중간 점수
            return round(score * 70, 1)
        else:  # negative
            # 부정적일수록 선정적·자극적 → 낮은 점수
            return round((1 - score) * 50, 1)
    except Exception as e:
        # 모델 추론 실패 시 중간값 반환 (분석 불가 상태)
        print(f"[Worker] AI 감성분석 오류: {e}")
        return 50.0

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
    # 제목의 자극적 단어는 가중치 2배 (헤드라인은 클릭 유도 의도가 더 강함)
    TITLE_MULTIPLIER = 2.0

    weighted_hit_count = 0  # 가중치가 적용된 총 자극 점수

    for category, config in PROVOCATIVE_CATEGORIES.items():
        cat_weight = config['weight']  # 카테고리 가중치

        for word in config['words']:
            # 제목에서 검출된 횟수 (가중치 2배 적용)
            title_hits = len(re.findall(re.escape(word), title))
            # 본문에서 검출된 횟수 (가중치 1배)
            body_hits = len(re.findall(re.escape(word), body))

            # 카테고리 가중치 × (제목 2배 + 본문 1배)
            weighted_hit_count += cat_weight * (title_hits * TITLE_MULTIPLIER + body_hits)

    # ── 2단계: 느낌표/물음표 연속 사용 감지 ──
    # "충격!!!", "진짜???" 같은 과도한 문장부호는 자극성의 신호
    # 2개 이상 연속된 느낌표/물음표를 감점 대상으로 카운트
    punctuation_hits = len(re.findall(r'[!]{2,}', f"{title} {body}"))   # !! 이상
    punctuation_hits += len(re.findall(r'[?]{2,}', f"{title} {body}"))  # ?? 이상

    # 제목의 연속 문장부호는 가중치 2배
    title_punct = len(re.findall(r'[!]{2,}', title)) + len(re.findall(r'[?]{2,}', title))
    # 본문 문장부호는 이미 포함되어 있으므로, 제목 추가분만 더함
    weighted_hit_count += title_punct * TITLE_MULTIPLIER + punctuation_hits

    # ── 3단계: 본문 길이 대비 비율로 정규화 ──
    # 글자 수 기준으로 나누어 긴 기사가 불이익받지 않도록 함
    # (짧은 기사에 자극적 단어가 몰려 있으면 비율이 높아짐)
    total_chars = len(title) + len(body)
    if total_chars == 0:
        return 50.0

    # 100글자당 자극 점수 비율로 정규화
    ratio_per_100 = (weighted_hit_count / total_chars) * 100

    # ── 4단계: 단어 기반 점수 산출 ──
    # ratio_per_100이 0이면 100점 (자극성 없음)
    # ratio_per_100이 3 이상이면 0점 (매우 자극적)
    # 선형 감점: 100 - (비율 × 33.3)
    word_score = max(0, 100 - (ratio_per_100 * 33.3))

    # ── 5단계: AI 감성분석 점수와 결합 ──
    # 단어 기반(50%) + AI 모델(50%)로 최종 자극성 점수 산출
    # 단어 기반: 명시적 자극 표현 감지 (규칙 기반, 빠르고 예측 가능)
    # AI 모델: 문맥 속 감성/논조 파악 (딥러닝 기반, 미묘한 뉘앙스 포착)
    ai_score = analyze_ai_sentiment(body)
    final_score = (word_score * 0.5) + (ai_score * 0.5)

    return round(final_score, 1)

def analyze_source(url):
    """지표 3: 출처 신뢰도 (20%) — 언론사 신뢰도 + 등록 도메인 여부"""
    # 주요 언론사 (높은 점수: 90점)
    major_sources = [
        'yonhapnews.co.kr', 'yna.co.kr',       # 연합뉴스
        'kbs.co.kr', 'mbc.co.kr', 'sbs.co.kr',  # 지상파
        'chosun.com', 'donga.com', 'hani.co.kr', # 종합일간지
        'joongang.co.kr', 'khan.co.kr',
    ]
    # 등록 인터넷 매체 (중간 점수: 65점)
    registered_sources = [
        'naver.com', 'daum.net',                # 포털
        'newsis.com', 'news1.kr', 'edaily.co.kr',
        'hankyung.com', 'mk.co.kr', 'mt.co.kr',
        'sedaily.com', 'heraldcorp.com',
        'nocutnews.co.kr', 'ohmynews.com',
        'zdnet.co.kr', 'etnews.com',
    ]

    for source in major_sources:
        if source in url:
            return 90.0
    for source in registered_sources:
        if source in url:
            return 65.0
    return 30.0  # 출처 불명

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
    """분석 결과를 SQLite에 저장"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO analysis_results
        (url, title, body, content_score, provocative_score, source_score,
         total_score, grade, analyzed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        result['url'], result['title'], result['body'][:500],
        result['content'], result['provocative'], result['source'],
        result['total'], result['grade'], result['analyzed_at']
    ))
    conn.commit()
    conn.close()

# ========== 메인: 큐에서 기사 꺼내서 분석 ==========

def process_message(ch, method, properties, body):
    """큐에서 메시지를 받으면 실행되는 함수"""
    data = json.loads(body)
    url = data['url']
    print(f"[Worker] 분석 시작: {url}")

    # newspaper3k로 실제 뉴스 크롤링
    try:
        title, article_body = crawl_article(url)
    except Exception as e:
        print(f"[Worker] 크롤링 실패: {url} — {e}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    if not article_body or len(article_body.strip()) < 50:
        print(f"[Worker] 본문이 너무 짧거나 비어있음: {url}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    # 3가지 지표 분석
    content = analyze_content_similarity(title, article_body)
    provocative = analyze_provocative(title, article_body)
    source = analyze_source(url)
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
        'analyzed_at': time.strftime("%Y-%m-%d %H:%M:%S")
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
