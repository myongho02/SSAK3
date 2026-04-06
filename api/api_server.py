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
    """사용자가 URL을 보내면 큐에 넣기"""
    data = request.get_json()
    
    if not data or 'url' not in data:
        return jsonify({"error": "URL을 입력해주세요"}), 400
    
    url = data['url']
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
    