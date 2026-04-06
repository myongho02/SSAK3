import pika
import json
import sqlite3
import os
import re
import time
from newspaper import Article
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

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

def crawl_article(url):
    """newspaper3k로 실제 뉴스 기사 크롤링"""
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

def analyze_provocative(title, body):
    """지표 2: 자극성 분석 (35%) — 자극적/선정적 표현 비율 측정
    자극적 표현이 많을수록 점수가 낮아짐 (100에서 차감)
    참고: Alonso et al., "Sentiment Analysis for Fake News Detection" (Electronics, MDPI, 2021)
    """
    provocative_words = [
        '충격', '경악', '소름', '긴급', '폭로', '속보', '단독',
        '발칵', '경악', '치명적', '최악', '대박', '헉',
        '눈물', '분노', '공포', '절규', '참사', '패닉',
        '날벼락', '파문', '망신', '몰락', '추락', '폭망',
        '역대급', '충격적', '소름끼치는', '믿기힘든', '경악스러운',
        '전율', '아찔', '섬뜩', '끔찍', '치욕', '굴욕',
        '논란', '파장', '후폭풍', '대참사', '비상',
        '급반전', '반전', '실화', '레전드', '미쳤',
    ]

    full_text = f"{title} {body}"
    # 형태소 단위가 아닌 단순 포함 여부로 체크
    total_words = len(full_text.split())
    if total_words == 0:
        return 50.0

    hit_count = 0
    for word in provocative_words:
        hit_count += len(re.findall(re.escape(word), full_text))

    # 자극적 단어 비율 (전체 단어 수 대비)
    ratio = hit_count / total_words

    # 비율이 높을수록 점수 차감 (100에서 시작)
    # ratio 0% → 100점, ratio 5% 이상 → 0점
    score = max(0, 100 - (ratio * 2000))
    return round(score, 1)

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
