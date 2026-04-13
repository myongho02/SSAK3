from flask import Flask, request, jsonify
import pika
import json
import sqlite3
import time
import os

app = Flask(__name__)
DB_PATH = "/app/data/results.db"


# ========== DB 초기화 ==========
def init_db():
    """API 서버 시작 시 DB 테이블을 생성한다.
    Worker도 동일한 init_db를 갖고 있으므로 어느 쪽이 먼저 시작해도 안전하다.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")

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

    # ── worker_id 컬럼: 어떤 Worker가 해당 기사를 분석했는지 추적 ──
    # Worker가 분석 결과를 저장할 때 자신의 WORKER_ID를 함께 기록한다.
    # 대시보드에서 Worker별 처리 현황을 시각화하는 데 사용
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN worker_id TEXT")
    except sqlite3.OperationalError:
        pass  # 이미 존재하는 컬럼

    conn.commit()
    conn.close()


def create_job(url):
    """jobs 테이블에 분석 요청을 pending 상태로 생성한다.

    Returns: job_id (정수) 또는 실패 시 None
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO jobs (url, status, queued_at) VALUES (?, 'pending', ?)",
            (url, time.strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        job_id = cursor.lastrowid
        conn.close()
        return job_id
    except Exception as e:
        print(f"[API] job 생성 실패: {e}")
        return None


def send_to_queue(job_id, url):
    """job_id와 URL을 RabbitMQ 큐에 넣기.
    큐 메시지에 job_id를 포함하여 Worker가 정확한 job을 추적할 수 있게 한다.
    """
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host='rabbitmq')
    )
    channel = connection.channel()
    channel.queue_declare(queue='news_queue', durable=True)

    message = json.dumps({
        "job_id": job_id,
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


def check_existing_result(url):
    """이미 분석 완료(done)된 결과가 있는지 확인한다.
    Returns: dict(결과) 또는 None
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM analysis_results WHERE url = ? AND status = 'done' "
            "ORDER BY analyzed_at DESC LIMIT 1",
            (url,)
        )
        existing = cursor.fetchone()
        conn.close()
        return dict(existing) if existing else None
    except Exception:
        return None


def check_pending_or_processing(url):
    """해당 URL이 현재 pending 또는 processing 상태인 job이 있는지 확인한다.
    Returns: True면 이미 큐에 있거나 처리 중
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE url = ? AND status IN ('pending', 'processing')",
            (url,)
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


# ========== API 엔드포인트 ==========

@app.route('/')
def home():
    return jsonify({
        "message": "뉴스 신뢰도 점수화 API 서버",
        "description": "규칙 기반 분석 + AI 보조지표를 결합한 뉴스 신뢰도 점수화 시스템",
        "status": "running"
    })


@app.route('/analyze', methods=['POST'])
def analyze():
    """단건 URL 분석 요청.

    중복 방지 기준:
    - done: 기존 결과 즉시 반환
    - pending/processing: 이미 처리 중이므로 대기 안내
    - failed: 재시도 허용 (새 job 생성)
    """
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "URL을 입력해주세요"}), 400

    url = data['url']

    # 1) 이미 분석 완료된 결과가 있으면 즉시 반환
    existing = check_existing_result(url)
    if existing:
        return jsonify({
            "message": "이미 분석된 기사입니다.",
            "url": url,
            "result": existing
        })

    # 2) 현재 pending/processing 중이면 중복 요청 방지
    if check_pending_or_processing(url):
        return jsonify({
            "message": "이미 분석이 진행 중입니다. 잠시 후 결과를 확인해주세요.",
            "url": url
        })

    # 3) 새 job 생성 → 큐에 전달
    job_id = create_job(url)
    if job_id is None:
        return jsonify({"error": "분석 요청 생성 실패"}), 500

    try:
        send_to_queue(job_id, url)
    except Exception as e:
        print(f"[API] RabbitMQ 전송 실패: {e}")
        # 큐 전송 실패 시 job을 failed로 마킹
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            conn.execute(
                "UPDATE jobs SET status = 'failed', error_message = ? WHERE id = ?",
                (f"큐 전송 실패: {str(e)[:200]}", job_id)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return jsonify({"error": f"메시지 큐 전송 실패: {str(e)}"}), 503

    return jsonify({
        "message": "분석 요청 완료! Worker가 처리 중입니다.",
        "url": url,
        "job_id": job_id
    })


@app.route('/analyze/bulk', methods=['POST'])
def analyze_bulk():
    """여러 URL을 한번에 받아 분석 요청.

    중복 방지 기준 (단건과 동일):
    - done → skip
    - pending/processing → skip
    - failed/신규 → 새 job 생성 후 큐에 전달
    """
    data = request.get_json()
    if not data or 'urls' not in data:
        return jsonify({"error": "urls 리스트를 입력해주세요"}), 400

    urls = data['urls']
    if not isinstance(urls, list) or len(urls) == 0:
        return jsonify({"error": "urls는 비어있지 않은 배열이어야 합니다"}), 400

    queued_count = 0
    skipped_count = 0

    # done 상태 URL 집합
    done_urls = set()
    # pending/processing 상태 URL 집합
    active_urls = set()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM analysis_results WHERE status = 'done'")
        done_urls = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT url FROM jobs WHERE status IN ('pending', 'processing')")
        active_urls = {row[0] for row in cursor.fetchall()}
        conn.close()
    except Exception:
        pass

    # RabbitMQ 연결
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

        # done이거나 이미 큐에 있으면 skip
        if url in done_urls or url in active_urls:
            skipped_count += 1
            continue

        job_id = create_job(url)
        if job_id is None:
            continue

        message = json.dumps({
            "job_id": job_id,
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


# ========== 조회 엔드포인트 ==========

@app.route('/jobs', methods=['GET'])
def get_jobs():
    """jobs 테이블에서 분석 작업 현황을 조회한다."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs ORDER BY id DESC")
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        print(f"[API] jobs 조회 실패: {e}")
        return jsonify([])


@app.route('/jobs/summary', methods=['GET'])
def get_jobs_summary():
    """jobs 상태별 건수 요약을 반환한다."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
        )
        summary = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()
        return jsonify(summary)
    except Exception as e:
        print(f"[API] jobs 요약 조회 실패: {e}")
        return jsonify({})


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
        print(f"[API] 결과 조회 실패 (id={result_id}): {e}")
        return jsonify({"error": f"DB 조회 실패: {str(e)}"}), 500


if __name__ == "__main__":
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
