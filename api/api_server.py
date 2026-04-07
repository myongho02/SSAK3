from flask import Flask, request, jsonify
import pika
import json
import sqlite3
import time
import os

app = Flask(__name__)
DB_PATH = "/app/data/results.db"

def send_to_queue(url):
    """URL을 RabbitMQ 큐에 넣기"""
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

    send_to_queue(url)

    return jsonify({
        "message": "분석 요청 완료! Worker가 처리 중입니다.",
        "url": url
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
    