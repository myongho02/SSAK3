import pika
import json
import sqlite3
import os
import re
import numpy as np
from transformers import pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ========== AI 모델 로딩 (Worker 시작 시 1번만) ==========
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
            sentiment_score REAL,
            tfidf_score REAL,
            source_score REAL,
            total_score REAL,
            grade TEXT,
            analyzed_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

# ========== 신뢰도 분석 함수들 ==========

def analyze_sentiment(text):
    """지표 1: AI 감성분석 (Softmax 확률값)"""
    try:
        # 텍스트가 너무 길면 앞부분만 사용
        result = sentiment_analyzer(text[:512])
        label = result[0]['label']
        score = result[0]['score']
        
        # 중립적일수록 높은 점수
        if label == 'neutral':
            return score * 100
        elif label == 'positive':
            return score * 70
        else:  # negative
            return (1 - score) * 50
    except:
        return 50  # 에러 시 중간값

def analyze_tfidf_similarity(title, body):
    """지표 2: TF-IDF 제목-본문 유사도 (코사인 유사도)"""
    try:
        if not title or not body:
            return 50
        vectorizer = TfidfVectorizer()
        vectors = vectorizer.fit_transform([title, body])
        similarity = cosine_similarity(vectors[0], vectors[1])[0][0]
        return similarity * 100
    except:
        return 50

def analyze_source(url):
    """지표 3: 출처 신뢰도 (등록 언론사 목록 대조)"""
    trusted_sources = [
        'naver.com', 'daum.net', 'yonhapnews.co.kr',
        'kbs.co.kr', 'mbc.co.kr', 'sbs.co.kr',
        'chosun.com', 'donga.com', 'hani.co.kr',
        'joongang.co.kr', 'khan.co.kr', 'yna.co.kr',
        'newsis.com', 'news1.kr', 'edaily.co.kr'
    ]
    
    for source in trusted_sources:
        if source in url:
            return 90  # 주요 언론사
    return 40  # 출처 불명 또는 비등록 매체

def calculate_total_score(sentiment, tfidf, source):
    """종합 점수 계산: 감성(40%) + 유사도(35%) + 출처(25%)"""
    total = (sentiment * 0.40) + (tfidf * 0.35) + (source * 0.25)
    return round(total, 1)

def get_grade(score):
    """점수를 등급으로 변환"""
    if score >= 80:
        return "신뢰 가능"
    elif score >= 60:
        return "주의 필요"
    elif score >= 40:
        return "의심"
    else:
        return "신뢰 어려움"

def save_to_db(result):
    """분석 결과를 SQLite에 저장"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO analysis_results 
        (url, title, body, sentiment_score, tfidf_score, source_score, total_score, grade, analyzed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        result['url'], result['title'], result['body'][:500],
        result['sentiment'], result['tfidf'], result['source'],
        result['total'], result['grade'], result['analyzed_at']
    ))
    conn.commit()
    conn.close()

# ========== 메인: 큐에서 기사 꺼내서 분석 ==========

def process_message(ch, method, properties, body):
    """큐에서 메시지를 받으면 실행되는 함수"""
    import time
    
    data = json.loads(body)
    url = data['url']
    print(f"[Worker] 분석 시작: {url}")
    
    # 실제로는 newspaper3k로 크롤링하지만, 지금은 테스트용 더미 데이터
    title = f"테스트 뉴스 기사 제목 {data['id']}"
    article_body = f"이것은 테스트용 뉴스 기사 본문입니다. 기사 ID: {data['id']}. 정부는 오늘 새로운 정책을 발표했습니다."
    
    # 3가지 지표 분석
    sentiment = analyze_sentiment(article_body)
    tfidf = analyze_tfidf_similarity(title, article_body)
    source = analyze_source(url)
    total = calculate_total_score(sentiment, tfidf, source)
    grade = get_grade(total)
    
    result = {
        'url': url,
        'title': title,
        'body': article_body,
        'sentiment': sentiment,
        'tfidf': tfidf,
        'source': source,
        'total': total,
        'grade': grade,
        'analyzed_at': time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    # DB에 저장
    save_to_db(result)
    
    print(f"[Worker] 분석 완료: {title}")
    print(f"         감성: {sentiment:.1f} | 유사도: {tfidf:.1f} | 출처: {source:.1f} | 종합: {total:.1f}점 ({grade})")
    
    # 처리 완료 신호 (이걸 보내야 큐에서 메시지가 삭제됨)
    ch.basic_ack(delivery_tag=method.delivery_tag)

def main():
    """Worker 메인: RabbitMQ에 연결하고 큐에서 메시지 기다리기"""
    init_db()
    
    # RabbitMQ 연결 (연결될 때까지 재시도)
    while True:
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host='rabbitmq')
            )
            break
        except pika.exceptions.AMQPConnectionError:
            print("[Worker] RabbitMQ 연결 대기 중... 5초 후 재시도")
            import time
            time.sleep(5)
    
    channel = connection.channel()
    channel.queue_declare(queue='news_queue', durable=True)
    
    # Worker 1개가 한 번에 1개씩만 처리 (공평하게 분배)
    channel.basic_qos(prefetch_count=1)
    
    # 큐에서 메시지가 오면 process_message 함수 실행
    channel.basic_consume(queue='news_queue', on_message_callback=process_message)
    
    print("[Worker] 대기 중... 큐에서 기사를 기다리고 있습니다.")
    channel.start_consuming()

if __name__ == "__main__":
    main()
    