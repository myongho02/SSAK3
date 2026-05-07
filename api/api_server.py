from flask import Flask, request, jsonify
from collections import deque, defaultdict
import pika
import json
import sqlite3
import time
import os
import secrets
import bcrypt

app = Flask(__name__)
# H3-4 환경 변수 분리: DB 경로, 세션 TTL을 환경에서 오버라이드 가능
DB_PATH = os.environ.get("SSAK3_DB_PATH", "/app/data/results.db")

# ========== Phase G — 사용자 격리 설정 ==========
# 세션 토큰 만료 시간 (초). 기본 7일.
SESSION_TTL_SECONDS = int(os.environ.get("SSAK3_SESSION_TTL", 7 * 24 * 60 * 60))


# ========== H3-1 Rate Limit (인메모리 슬라이딩 윈도우) ==========
# 운영 수준: 무차별 회원가입/로그인/분석 요청 차단.
# IP+endpoint별 윈도우 추적. 무거운 의존성(Redis) 없이 가벼운 in-memory.
_rate_buckets = defaultdict(deque)
RATE_LIMITS = {
    'auth_register': (5, 60),    # 분당 5회
    'auth_login':    (10, 60),   # 분당 10회
    'analyze':       (60, 60),   # 분당 60회 (분석은 인증 후라 좀 더 너그럽게)
    'analyze_bulk':  (10, 60),
    'change_password': (3, 60),
    'delete_account':  (3, 60),
}


def check_rate_limit(name):
    """endpoint별 분당 제한. 초과 시 (False, retry_after_sec) 반환.
    키는 IP+endpoint+(인증된 user_id가 있으면 그것). 익명 사용자도 IP 기반.
    """
    if name not in RATE_LIMITS:
        return True, 0
    max_calls, window = RATE_LIMITS[name]
    # remote IP — Streamlit/대시보드는 같은 docker 네트워크에서 호출되므로
    # X-Forwarded-For가 있으면 그 IP를 우선 사용
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    user_id = auth_user(request)
    key = f"{name}:{ip}:{user_id or 'anon'}"

    now = time.time()
    bucket = _rate_buckets[key]
    # 윈도우 밖 항목 제거
    while bucket and bucket[0] < now - window:
        bucket.popleft()
    if len(bucket) >= max_calls:
        retry_after = int(window - (now - bucket[0]))
        return False, max(1, retry_after)
    bucket.append(now)
    return True, 0


def rate_limit_response(name):
    """rate limit 위반 시 표준 429 응답."""
    ok, retry = check_rate_limit(name)
    if not ok:
        resp = jsonify({"error": f"요청이 너무 많습니다. {retry}초 뒤에 다시 시도해주세요."})
        resp.status_code = 429
        resp.headers['Retry-After'] = str(retry)
        return resp
    return None


# ========== H3-2 보안 헤더 (운영 수준) ==========
@app.after_request
def add_security_headers(resp):
    """모든 응답에 기본 보안 헤더 부착."""
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # HSTS: HTTPS 노출 시 강제 (ngrok 등 HTTPS 터널 사용 시 효과)
    resp.headers.setdefault('Strict-Transport-Security', 'max-age=15552000; includeSubDomains')
    return resp


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

    # ── processing_time 컬럼: 기사 1건 처리 소요 시간 (초) ──
    # Worker가 크롤링~분석~저장까지 걸린 시간을 기록
    # 대시보드 성능 지표(평균 처리 시간, throughput)에 사용
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN processing_time REAL")
    except sqlite3.OperationalError:
        pass  # 이미 존재하는 컬럼

    # ── cache_stats 컬럼: 캐시 적중률 스냅샷 (JSON, 논문 "캐싱 최적화") ──
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

    # ========== Phase G — 사용자 격리 (Multi-tenant) ==========
    # users: 회원 계정 (username unique, bcrypt 패스워드 해싱)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    # sessions: 로그인 토큰 (만료 시간 포함)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # ── user_id 외래키: 본인 분석만 격리 조회 가능하게 ──
    # NULL 허용 (비회원 모드 보존 — 기존 분석 결과와 호환)
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass

    # ── J4 분석 결과 공유 토큰: 본인이 자기 결과를 read-only 링크로 공유 ──
    # share_token이 NULL이면 비공개, 값이 있으면 누구나 그 토큰으로 read-only 조회 가능
    try:
        cursor.execute("ALTER TABLE analysis_results ADD COLUMN share_token TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


# ========== Phase G — 인증 헬퍼 ==========

def hash_password(password):
    """bcrypt로 패스워드 해싱"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password, password_hash):
    """bcrypt 검증"""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    except Exception:
        return False


def create_session(user_id):
    """새 세션 토큰을 발급하고 DB에 저장한다."""
    token = secrets.token_hex(32)
    now = int(time.time())
    expires = now + SESSION_TTL_SECONDS
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, time.strftime("%Y-%m-%d %H:%M:%S"), expires)
        )
        conn.commit()
        conn.close()
        return token
    except Exception as e:
        print(f"[API] 세션 생성 실패: {e}")
        return None


def get_user_from_token(token):
    """Authorization 헤더의 토큰으로 user_id를 조회한다.
    만료된 세션은 자동 정리(삭제)하고 None 반환.
    """
    if not token:
        return None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        # 만료된 세션 청소
        now = int(time.time())
        cursor.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.commit()
        # 토큰으로 user_id 조회
        cursor.execute("SELECT user_id FROM sessions WHERE token = ?", (token,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def auth_user(req):
    """요청 헤더에서 토큰을 꺼내 user_id를 반환한다.
    Authorization: Bearer <token> 형식 또는 X-Auth-Token 헤더 지원.
    Returns: user_id (int) 또는 None (비회원/익명).
    """
    token = req.headers.get('Authorization', '').replace('Bearer ', '').strip()
    if not token:
        token = req.headers.get('X-Auth-Token', '').strip()
    return get_user_from_token(token) if token else None


# ========== Phase G — 인증 엔드포인트 ==========

@app.route('/auth/register', methods=['POST'])
def auth_register():
    """회원가입. {username, password} JSON 입력.
    username 중복 체크 후 bcrypt로 해싱하여 저장.
    """
    rl = rate_limit_response('auth_register')
    if rl is not None: return rl
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return jsonify({"error": "username과 password가 필요합니다"}), 400
    if len(username) < 3 or len(username) > 30:
        return jsonify({"error": "username은 3~30자여야 합니다"}), 400
    # H2-5: 비밀번호 강도 검증 (운영 수준)
    pw_ok, pw_err = is_password_strong(password)
    if not pw_ok:
        return jsonify({"error": pw_err}), 400

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        if cursor.fetchone():
            conn.close()
            return jsonify({"error": "이미 사용 중인 username입니다"}), 409
        cursor.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), time.strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        token = create_session(user_id)
        return jsonify({
            "message": "회원가입 완료",
            "user_id": user_id,
            "username": username,
            "token": token,
        })
    except Exception as e:
        print(f"[API] 회원가입 실패: {e}")
        return jsonify({"error": f"회원가입 실패: {str(e)}"}), 500


@app.route('/auth/login', methods=['POST'])
def auth_login():
    """로그인. {username, password} JSON 입력.
    인증 성공 시 새 세션 토큰을 발급한다.
    """
    rl = rate_limit_response('auth_login')
    if rl is not None: return rl
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return jsonify({"error": "username과 password가 필요합니다"}), 400

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()
        if not row or not verify_password(password, row[1]):
            return jsonify({"error": "username 또는 password가 올바르지 않습니다"}), 401
        user_id = row[0]
        token = create_session(user_id)
        return jsonify({
            "message": "로그인 성공",
            "user_id": user_id,
            "username": username,
            "token": token,
        })
    except Exception as e:
        print(f"[API] 로그인 실패: {e}")
        return jsonify({"error": f"로그인 실패: {str(e)}"}), 500


@app.route('/auth/logout', methods=['POST'])
def auth_logout():
    """로그아웃 — 세션 토큰 무효화."""
    token = request.headers.get('Authorization', '').replace('Bearer ', '').strip()
    if not token:
        return jsonify({"message": "이미 비회원 상태"}), 200
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return jsonify({"message": "로그아웃 완료"})


def is_password_strong(password):
    """[H2-5] 비밀번호 강도 검증 (운영 수준).
    최소 8자, 영문/숫자 모두 포함.
    """
    if not password or len(password) < 8:
        return False, "비밀번호는 최소 8자 이상이어야 합니다"
    has_alpha = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_alpha and has_digit):
        return False, "비밀번호는 영문과 숫자를 모두 포함해야 합니다"
    return True, None


@app.route('/auth/change_password', methods=['POST'])
def auth_change_password():
    """[H2-1] 비밀번호 변경. 현재 비번 검증 후 새 비번으로 갱신.
    성공 시 기존 세션은 모두 무효화 (다른 디바이스 자동 로그아웃)
    """
    rl = rate_limit_response('change_password')
    if rl is not None: return rl
    user_id = auth_user(request)
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다"}), 401
    data = request.get_json() or {}
    current_pw = data.get('current_password') or ''
    new_pw = data.get('new_password') or ''

    ok, err = is_password_strong(new_pw)
    if not ok:
        return jsonify({"error": err}), 400

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row or not verify_password(current_pw, row[0]):
            conn.close()
            return jsonify({"error": "현재 비밀번호가 올바르지 않습니다"}), 401
        cursor.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_pw), user_id),
        )
        # 보안: 모든 기존 세션 무효화 (다른 디바이스 강제 로그아웃)
        cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        # 새 세션 발급해서 현재 사용자는 자동 로그인 유지
        new_token = create_session(user_id)
        return jsonify({
            "message": "비밀번호 변경 완료. 다른 디바이스는 자동 로그아웃됩니다.",
            "token": new_token,
        })
    except Exception as e:
        return jsonify({"error": f"변경 실패: {str(e)}"}), 500


@app.route('/auth/delete_account', methods=['POST'])
def auth_delete_account():
    """[H2-2] 회원 탈퇴 — 본인 데이터(분석 결과/jobs/세션/사용자) 모두 삭제.
    GDPR 수준: 비밀번호 재확인 후 영구 삭제.
    """
    rl = rate_limit_response('delete_account')
    if rl is not None: return rl
    user_id = auth_user(request)
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다"}), 401
    data = request.get_json() or {}
    password = data.get('password') or ''
    confirm = data.get('confirm', False)

    if not confirm:
        return jsonify({"error": "탈퇴를 진행하려면 confirm=true를 보내주세요"}), 400

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT username, password_hash FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "사용자를 찾을 수 없습니다"}), 404
        if not verify_password(password, row[1]):
            conn.close()
            return jsonify({"error": "비밀번호가 올바르지 않습니다"}), 401
        username = row[0]
        # CASCADE 삭제: 본인 분석 결과 / jobs / 세션 / 사용자 본체
        cursor.execute("DELETE FROM analysis_results WHERE user_id = ?", (user_id,))
        deleted_results = cursor.rowcount
        cursor.execute("DELETE FROM jobs WHERE user_id = ?", (user_id,))
        deleted_jobs = cursor.rowcount
        cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()
        return jsonify({
            "message": f"회원 '{username}' 탈퇴 완료. 분석 결과 {deleted_results}건과 작업 {deleted_jobs}건이 영구 삭제되었습니다.",
        })
    except Exception as e:
        return jsonify({"error": f"탈퇴 실패: {str(e)}"}), 500


@app.route('/auth/me', methods=['GET'])
def auth_me():
    """현재 토큰의 사용자 정보 조회."""
    user_id = auth_user(request)
    if not user_id:
        return jsonify({"authenticated": False})
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT username, created_at FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return jsonify({
                "authenticated": True,
                "user_id": user_id,
                "username": row[0],
                "created_at": row[1],
            })
    except Exception:
        pass
    return jsonify({"authenticated": False})


def create_job(url, user_label="", user_id=None):
    """jobs 테이블에 분석 요청을 pending 상태로 생성한다.

    Args:
        url: 분석 대상 URL
        user_label: 분석 요청자 프로필 라벨 (E1, 경량 사용자화). 없으면 빈 문자열.
        user_id: 인증된 회원의 user_id (Phase G). 비회원은 None.

    Returns: job_id (정수) 또는 실패 시 None
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO jobs (url, status, queued_at, user_label, user_id) VALUES (?, 'pending', ?, ?, ?)",
            (url, time.strftime("%Y-%m-%d %H:%M:%S"), user_label or "", user_id)
        )
        conn.commit()
        job_id = cursor.lastrowid
        conn.close()
        return job_id
    except Exception as e:
        print(f"[API] job 생성 실패: {e}")
        return None


def send_to_queue(job_id, url, user_label="", user_id=None):
    """job_id와 URL을 RabbitMQ 큐에 넣기.
    큐 메시지에 job_id, user_label, user_id를 포함하여 Worker가 결과 저장 시 함께 기록한다.
    """
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host='rabbitmq')
    )
    channel = connection.channel()
    channel.queue_declare(queue='news_queue', durable=True)

    message = json.dumps({
        "job_id": job_id,
        "url": url,
        "user_label": user_label or "",
        "user_id": user_id,  # 인증된 회원의 user_id (None이면 비회원)
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    })

    channel.basic_publish(
        exchange='',
        routing_key='news_queue',
        body=message,
        properties=pika.BasicProperties(delivery_mode=2)
    )
    connection.close()


def check_existing_result(url, user_id=None):
    """이미 분석 완료(done)된 결과가 있는지 확인한다.
    [Phase G] user_id별 격리: 본인이 분석한 결과만 "이미 분석됨"으로 인정.
    다른 사용자가 같은 URL을 분석했어도 본인 입장에서는 새로 분석해야 함.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if user_id is None:
            # 비회원: user_id IS NULL인 결과만 본인 것으로 간주
            cursor.execute(
                "SELECT * FROM analysis_results WHERE url = ? AND status = 'done' "
                "AND user_id IS NULL "
                "ORDER BY analyzed_at DESC LIMIT 1",
                (url,)
            )
        else:
            cursor.execute(
                "SELECT * FROM analysis_results WHERE url = ? AND status = 'done' "
                "AND user_id = ? "
                "ORDER BY analyzed_at DESC LIMIT 1",
                (url, user_id)
            )
        existing = cursor.fetchone()
        conn.close()
        return dict(existing) if existing else None
    except Exception:
        return None


def check_pending_or_processing(url, user_id=None):
    """해당 URL이 현재 pending/processing 상태인 본인 job이 있는지 확인한다."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute(
                "SELECT COUNT(*) FROM jobs WHERE url = ? AND status IN ('pending', 'processing') "
                "AND user_id IS NULL",
                (url,)
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM jobs WHERE url = ? AND status IN ('pending', 'processing') "
                "AND user_id = ?",
                (url, user_id)
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
    rl = rate_limit_response('analyze')
    if rl is not None: return rl
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "URL을 입력해주세요"}), 400

    url = data['url']
    user_label = data.get('user_label', '') or ''
    # Phase G: 인증된 사용자 식별
    user_id = auth_user(request)

    # 1) 이미 분석 완료된 결과가 있으면 즉시 반환 (본인 결과만)
    existing = check_existing_result(url, user_id=user_id)
    if existing:
        return jsonify({
            "message": "이미 분석된 기사입니다.",
            "url": url,
            "result": existing
        })

    # 2) 현재 pending/processing 중이면 중복 요청 방지 (본인 job만)
    if check_pending_or_processing(url, user_id=user_id):
        return jsonify({
            "message": "이미 분석이 진행 중입니다. 잠시 후 결과를 확인해주세요.",
            "url": url
        })

    # 3) 새 job 생성 → 큐에 전달 (user_id + user_label 포함)
    job_id = create_job(url, user_label=user_label, user_id=user_id)
    if job_id is None:
        return jsonify({"error": "분석 요청 생성 실패"}), 500

    try:
        send_to_queue(job_id, url, user_label=user_label, user_id=user_id)
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
    rl = rate_limit_response('analyze_bulk')
    if rl is not None: return rl
    data = request.get_json()
    if not data or 'urls' not in data:
        return jsonify({"error": "urls 리스트를 입력해주세요"}), 400

    urls = data['urls']
    user_label = data.get('user_label', '') or ''
    user_id = auth_user(request)  # Phase G: 인증된 회원 식별
    if not isinstance(urls, list) or len(urls) == 0:
        return jsonify({"error": "urls는 비어있지 않은 배열이어야 합니다"}), 400

    queued_count = 0
    skipped_count = 0

    # done/active URL 집합 — Phase G: user_id별로 격리해 본인 데이터만 중복 체크
    done_urls = set()
    active_urls = set()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute("SELECT url FROM analysis_results WHERE status = 'done' AND user_id IS NULL")
            done_urls = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT url FROM jobs WHERE status IN ('pending', 'processing') AND user_id IS NULL")
            active_urls = {row[0] for row in cursor.fetchall()}
        else:
            cursor.execute("SELECT url FROM analysis_results WHERE status = 'done' AND user_id = ?", (user_id,))
            done_urls = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT url FROM jobs WHERE status IN ('pending', 'processing') AND user_id = ?", (user_id,))
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

        job_id = create_job(url, user_label=user_label, user_id=user_id)
        if job_id is None:
            continue

        message = json.dumps({
            "job_id": job_id,
            "url": url,
            "user_label": user_label,
            "user_id": user_id,
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
    """jobs 테이블에서 본인 분석 작업 현황을 조회한다 (Phase G — user_id 격리)."""
    user_id = auth_user(request)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute("SELECT * FROM jobs WHERE user_id IS NULL ORDER BY id DESC")
        else:
            cursor.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC", (user_id,))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        print(f"[API] jobs 조회 실패: {e}")
        return jsonify([])


@app.route('/jobs/summary', methods=['GET'])
def get_jobs_summary():
    """본인의 jobs 상태별 건수 요약 (Phase G)."""
    user_id = auth_user(request)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute(
                "SELECT status, COUNT(*) as cnt FROM jobs WHERE user_id IS NULL GROUP BY status"
            )
        else:
            cursor.execute(
                "SELECT status, COUNT(*) as cnt FROM jobs WHERE user_id = ? GROUP BY status",
                (user_id,)
            )
        summary = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()
        return jsonify(summary)
    except Exception as e:
        print(f"[API] jobs 요약 조회 실패: {e}")
        return jsonify({})


@app.route('/results', methods=['GET'])
def get_results():
    """본인 분석 결과만 조회 (Phase G — user_id 격리)."""
    user_id = auth_user(request)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute(
                "SELECT * FROM analysis_results WHERE user_id IS NULL ORDER BY analyzed_at DESC"
            )
        else:
            cursor.execute(
                "SELECT * FROM analysis_results WHERE user_id = ? ORDER BY analyzed_at DESC",
                (user_id,)
            )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        print(f"[API] 결과 조회 실패: {e}")
        return jsonify({"error": f"결과 조회 실패: {str(e)}"}), 500


@app.route('/results/<int:result_id>', methods=['GET'])
def get_result(result_id):
    """특정 분석 결과 조회 — 본인 것만 허용 (Phase G)."""
    user_id = auth_user(request)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM analysis_results WHERE id = ?", (result_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "결과를 찾을 수 없습니다"}), 404
        # 본인 결과만 반환
        result_user_id = dict(row).get('user_id')
        if result_user_id != user_id:
            return jsonify({"error": "다른 사용자의 분석 결과는 조회할 수 없습니다"}), 403
        return jsonify(dict(row))
    except Exception as e:
        print(f"[API] 결과 조회 실패 (id={result_id}): {e}")
        return jsonify({"error": f"DB 조회 실패: {str(e)}"}), 500


# ========== J4 공유 링크 ==========

@app.route('/results/<int:result_id>/share', methods=['POST'])
def create_share_link(result_id):
    """[J4] 본인 분석 결과를 누구나 볼 수 있는 read-only 공유 링크로 변환.

    토큰은 1회 발급 후 유지 (재호출 시 동일 토큰 반환).
    Returns: {"share_token": "abc123...", "url": "/share/abc123..."}
    """
    user_id = auth_user(request)
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다 (본인 결과만 공유 가능)"}), 401
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, share_token FROM analysis_results WHERE id = ?",
            (result_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "분석 결과를 찾을 수 없습니다"}), 404
        if row[0] != user_id:
            conn.close()
            return jsonify({"error": "본인이 분석한 결과만 공유 가능합니다"}), 403
        existing_token = row[1]
        if existing_token:
            conn.close()
            return jsonify({"share_token": existing_token})
        # 새 토큰 발급
        token = secrets.token_urlsafe(16)  # 22자 URL-safe
        cursor.execute(
            "UPDATE analysis_results SET share_token = ? WHERE id = ?",
            (token, result_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"share_token": token})
    except Exception as e:
        return jsonify({"error": f"공유 링크 발급 실패: {str(e)}"}), 500


@app.route('/results/<int:result_id>/unshare', methods=['POST'])
def revoke_share_link(result_id):
    """공유 링크 무효화 (본인 결과만)."""
    user_id = auth_user(request)
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다"}), 401
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM analysis_results WHERE id = ?", (result_id,)
        )
        row = cursor.fetchone()
        if not row or row[0] != user_id:
            conn.close()
            return jsonify({"error": "본인 결과만 변경 가능"}), 403
        cursor.execute(
            "UPDATE analysis_results SET share_token = NULL WHERE id = ?",
            (result_id,)
        )
        conn.commit()
        conn.close()
        return jsonify({"message": "공유 링크가 무효화되었습니다"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/share/<share_token>', methods=['GET'])
def get_shared_result(share_token):
    """공유 토큰으로 분석 결과 조회 (인증 불필요, read-only).

    [공개 정보] 점수, 등급, 분석 근거 — 학회 발표 시연 가능
    [비공개 정보] user_id, body 일부 — 보안 위해 마스킹
    """
    if not share_token or len(share_token) < 16:
        return jsonify({"error": "유효하지 않은 공유 링크"}), 400
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM analysis_results WHERE share_token = ?", (share_token,)
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "공유된 분석 결과를 찾을 수 없습니다"}), 404
        result = dict(row)
        # 보안: user_id는 노출 안 함
        result.pop('user_id', None)
        result.pop('user_label', None)
        # body는 첫 200자만 노출 (이미 본문 일부 저장이지만 추가 안전장치)
        if result.get('body'):
            result['body'] = result['body'][:200]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"조회 실패: {str(e)}"}), 500


if __name__ == "__main__":
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
