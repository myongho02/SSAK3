from flask import Flask, request, jsonify
import pika
import json
import sqlite3
import time
import os

app = Flask(__name__)
DB_PATH = "/app/data/results.db"

def send_to_queue(url):
    """URL을 RabbitMQ 큐에 넣기

    RabbitMQ 연결 실패 시 예외를 호출자에게 전파하여
    API가 적절한 에러 응답을 반환할 수 있도록 한다.
    """
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host='rabbitmq')
    )
    channel = connection.channel()
    channel.queue_declare(queue='news_queue', durable=True)

    message = json.dumps({
        "id": int(time.time()),
        "url": url,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    })

    channel.basic_publish(
        exchange='',
        routing_key='news_queue',
        body=message,
        properties=pika.BasicProperties(delivery_mode=2)
    )
    connection.close()

@app.route('/')
def home():
    return jsonify({"message": "뉴스 신뢰도 분석 API 서버", "status": "running"})

@app.route('/analyze', methods=['POST'])
def analyze():
    """사용자가 URL을 보내면 큐에 넣기 (중복 URL 사전 체크 포함)"""
    data = request.get_json()

    if not data or 'url' not in data:
        return jsonify({"error": "URL을 입력해주세요"}), 400

    url = data['url']

    # ── 중복 URL 사전 체크 ──
    # Worker까지 가기 전에 API 단에서 미리 DB를 조회하여
    # 이미 분석된 URL이면 큐에 넣지 않고 바로 기존 결과를 반환한다.
    # → RabbitMQ 큐 부하 감소 + Worker의 불필요한 크롤링/AI 추론 방지
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM analysis_results WHERE url = ? ORDER BY analyzed_at DESC LIMIT 1",
            (url,)
        )
        existing = cursor.fetchone()
        conn.close()

        if existing:
            return jsonify({
                "message": "이미 분석된 기사입니다.",
                "url": url,
                "result": dict(existing)
            })
    except Exception as e:
        # DB 조회 실패 시(테이블 미생성 등)에는 무시하고 큐로 전달
        # Worker가 첫 실행 시 테이블을 생성하므로, 그 전에는 조회가 실패할 수 있음
        print(f"[API] 중복 체크 실패 (무시하고 큐 전달): {e}")

    # ── RabbitMQ 큐에 URL 전달 ──
    try:
        send_to_queue(url)
    except Exception as e:
        print(f"[API] RabbitMQ 전송 실패: {e}")
        return jsonify({"error": f"메시지 큐 전송 실패: {str(e)}"}), 503

    return jsonify({
        "message": "분석 요청 완료! Worker가 처리 중입니다.",
        "url": url
    })

@app.route('/analyze/bulk', methods=['POST'])
def analyze_bulk():
    """여러 URL을 한번에 받아 RabbitMQ 큐에 순서대로 넣기"""
    data = request.get_json()

    if not data or 'urls' not in data:
        return jsonify({"error": "urls 리스트를 입력해주세요"}), 400

    urls = data['urls']
    if not isinstance(urls, list) or len(urls) == 0:
        return jsonify({"error": "urls는 비어있지 않은 배열이어야 합니다"}), 400

    # ── 중복이 아닌 URL만 큐에 전달 ──
    queued_count = 0
    skipped_count = 0

    # DB 연결을 한 번만 열어 중복 체크 수행
    existing_urls = set()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM analysis_results")
        existing_urls = {row[0] for row in cursor.fetchall()}
        conn.close()
    except Exception:
        # 테이블 미생성 등의 경우 무시 — 전부 큐에 넣음
        pass

    # ── RabbitMQ 연결을 한 번만 열어 전체 URL을 순서대로 publish ──
    try:
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(host='rabbitmq')
        )
    except Exception as e:
        print(f"[API] RabbitMQ 연결 실패: {e}")
        return jsonify({"error": f"메시지 큐 연결 실패: {str(e)}"}), 503

    channel = connection.channel()
    channel.queue_declare(queue='news_queue', durable=True)

    for url in urls:
        url = url.strip()
        if not url or not url.startswith("http"):
            continue

        # 이미 분석된 URL은 건너뛰기
        if url in existing_urls:
            skipped_count += 1
            continue

        message = json.dumps({
            "id": int(time.time() * 1000) + queued_count,  # 밀리초 + 순번으로 고유 ID
            "url": url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        channel.basic_publish(
            exchange='',
            routing_key='news_queue',
            body=message,
            properties=pika.BasicProperties(delivery_mode=2)
        )
        queued_count += 1

    connection.close()

    return jsonify({
        "message": f"{queued_count}개 분석 요청 완료",
        "count": queued_count,
        "skipped": skipped_count
    })

@app.route('/results', methods=['GET'])
def get_results():
    """분석 결과 전체 조회"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM analysis_results ORDER BY analyzed_at DESC")
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        # DB 파일이 없거나 테이블이 아직 생성되지 않은 경우
        # Worker가 첫 분석을 완료하기 전에 조회하면 여기로 옴
        print(f"[API] 결과 조회 실패: {e}")
        return jsonify({"error": f"결과 조회 실패: {str(e)}"}), 500

@app.route('/results/<int:result_id>', methods=['GET'])
def get_result(result_id):
    """특정 분석 결과 조회"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM analysis_results WHERE id = ?", (result_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return jsonify(dict(row))
        return jsonify({"error": "결과를 찾을 수 없습니다"}), 404
    except Exception as e:
        # 존재하지 않는 ID 조회 또는 DB 접근 오류
        print(f"[API] 결과 조회 실패 (id={result_id}): {e}")
        return jsonify({"error": f"DB 조회 실패: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
    