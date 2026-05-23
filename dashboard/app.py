import streamlit as st
import sqlite3
import pandas as pd
import requests
import os
import json
import time
from html import escape as _html_escape


def safe(value):
    """[P0-4 XSS 방어] 외부 입력(제목/URL/출처 등)을 HTML에 삽입하기 전 이스케이프.

    크롤링된 데이터에 <script>, <img onerror=...> 같은 위험 문자열이 있을 수 있으므로
    unsafe_allow_html=True로 삽입되는 모든 외부 데이터는 이 함수를 거쳐야 한다.
    """
    if value is None:
        return ""
    return _html_escape(str(value), quote=True)

# ========== 기본 설정 ==========
DB_PATH = "/app/data/results.db"
# Flask API 서버 주소 (docker-compose 내부 네트워크에서 서비스명으로 접근)
API_URL = "http://api:5000"

st.set_page_config(page_title="뉴스 신뢰도 분석", page_icon="📰", layout="wide",
                   initial_sidebar_state="expanded")

# ========== J9 PWA 주입 (모바일 앱처럼 "홈 화면에 추가" 가능) ==========
# Streamlit은 head 직접 수정이 어려우므로 components.html로 manifest + theme-color 주입.
# 효과:
# - Android Chrome: "앱 설치" 프롬프트 표시
# - iOS Safari: "홈 화면에 추가" 시 standalone 모드로 실행
# - 발표 멘트: "모바일에서도 앱처럼 설치해서 사용할 수 있습니다"
import streamlit.components.v1 as components
_PWA_MANIFEST_INLINE = json.dumps({
    "name": "SSAK3 뉴스 신뢰도 분석",
    "short_name": "SSAK3",
    "description": "AI 기반 가짜뉴스 신뢰도 분석 서비스",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0F1B2D",
    "theme_color": "#0D9488",
    "lang": "ko",
    "icons": [{
        "src": "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIj48cmVjdCB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9IjgwIiBmaWxsPSIjMEYxQjJEIi8+PHRleHQgeD0iMjU2IiB5PSIzMjAiIGZvbnQtc2l6ZT0iMjAwIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmaWxsPSIjMEQ5NDg4Ij7wn5OwPC90ZXh0Pjwvc3ZnPg==",
        "sizes": "512x512",
        "type": "image/svg+xml",
        "purpose": "any maskable"
    }]
})
components.html(
    f"""
<script>
(function () {{
  try {{
    const top = window.parent && window.parent.document ? window.parent.document : document;
    const head = top.head || top.getElementsByTagName('head')[0];
    if (!head) return;
    // manifest (data URL — 외부 파일 의존 X)
    if (!top.querySelector('link[rel="manifest"]')) {{
      const m = document.createElement('link');
      m.rel = 'manifest';
      m.href = 'data:application/json;base64,' + btoa(unescape(encodeURIComponent({json.dumps(_PWA_MANIFEST_INLINE)})));
      head.appendChild(m);
    }}
    // theme-color
    if (!top.querySelector('meta[name="theme-color"]')) {{
      const tc = document.createElement('meta');
      tc.name = 'theme-color';
      tc.content = '#0D9488';
      head.appendChild(tc);
    }}
    // iOS PWA — apple-mobile-web-app-capable
    if (!top.querySelector('meta[name="apple-mobile-web-app-capable"]')) {{
      const a = document.createElement('meta');
      a.name = 'apple-mobile-web-app-capable';
      a.content = 'yes';
      head.appendChild(a);
    }}
    if (!top.querySelector('meta[name="apple-mobile-web-app-title"]')) {{
      const at = document.createElement('meta');
      at.name = 'apple-mobile-web-app-title';
      at.content = 'SSAK3';
      head.appendChild(at);
    }}
    // viewport (모바일 가독성)
    let vp = top.querySelector('meta[name="viewport"]');
    if (vp) vp.setAttribute('content', 'width=device-width, initial-scale=1, viewport-fit=cover');
  }} catch (e) {{ console.warn('PWA inject failed:', e); }}
}})();
</script>
""",
    height=0,
)


# ========== Phase G — 인증 헬퍼 ==========

def auth_headers():
    """현재 세션 토큰을 Authorization 헤더로 변환."""
    tok = st.session_state.get("auth_token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def auth_user_info():
    """현재 로그인 사용자 정보. 없으면 None.
    페이지 새로고침 시 session_state가 휘발되면 URL query param의 't'로 복원 시도."""
    info = st.session_state.get("auth_user")
    if info:
        return info
    # ── 새로고침 복원: URL의 ?t=<token> 으로 세션 복구 ──
    try:
        qp = st.query_params if hasattr(st, "query_params") else {}
        tok = qp.get("t")
        if isinstance(tok, list):
            tok = tok[0] if tok else None
        if not tok:
            return None
        r = requests.get(f"{API_URL}/auth/me",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data.get("authenticated"):
                st.session_state["auth_token"] = tok
                st.session_state["auth_user"] = {
                    "user_id": data["user_id"], "username": data["username"]
                }
                return st.session_state["auth_user"]
    except Exception:
        pass
    return None


def _persist_token_to_url(token):
    """토큰을 URL query param에 저장 → 새로고침 시에도 세션 유지.
    [주의] 학교 발표용 단순 영속화. 운영 환경에서는 httpOnly cookie 권장."""
    try:
        if hasattr(st, "query_params"):
            st.query_params["t"] = token
    except Exception:
        pass


def auth_login(username, password):
    """로그인 API 호출 → 토큰 저장. 실패 시 (False, error_msg)."""
    try:
        r = requests.post(f"{API_URL}/auth/login",
                          json={"username": username, "password": password},
                          timeout=10)
        if r.status_code == 200:
            data = r.json()
            st.session_state["auth_token"] = data["token"]
            st.session_state["auth_user"] = {
                "user_id": data["user_id"], "username": data["username"]
            }
            _persist_token_to_url(data["token"])
            return True, None
        return False, r.json().get("error", "로그인 실패")
    except Exception as e:
        return False, f"API 연결 실패: {e}"


def auth_register(username, password):
    """회원가입 API 호출 → 토큰 저장."""
    try:
        r = requests.post(f"{API_URL}/auth/register",
                          json={"username": username, "password": password},
                          timeout=10)
        if r.status_code == 200:
            data = r.json()
            st.session_state["auth_token"] = data["token"]
            st.session_state["auth_user"] = {
                "user_id": data["user_id"], "username": data["username"]
            }
            _persist_token_to_url(data["token"])
            return True, None
        return False, r.json().get("error", "회원가입 실패")
    except Exception as e:
        return False, f"API 연결 실패: {e}"


def auth_logout():
    """로그아웃 → session_state + URL query param 모두 정리."""
    try:
        requests.post(f"{API_URL}/auth/logout",
                      headers=auth_headers(), timeout=5)
    except Exception:
        pass
    st.session_state.pop("auth_token", None)
    st.session_state.pop("auth_user", None)
    try:
        if hasattr(st, "query_params") and "t" in st.query_params:
            del st.query_params["t"]
    except Exception:
        pass


# ── 로그인 게이트 페이지 (비로그인 시) ──
# [정책] 비회원 모드 제거 — 회원가입은 간단(아이디 3~30자, 비밀번호 8자+영숫자)이며
# 사용자별 분석 이력 격리·세션 영속·새로고침 안정성을 보장하기 위해 회원만 진입 허용.
def render_login_gate():
    """로그인/회원가입 진입 페이지."""
    st.markdown("""<div style="background: linear-gradient(135deg, #0F1B2D 0%, #1E3A5F 100%); padding: 40px 48px; border-radius: 20px; margin-bottom: 32px;">
<h1 style="color: #FFFFFF; margin: 0; font-size: 36px; font-weight: 700;">📰 SSAK3 뉴스 신뢰도 분석</h1>
<p style="color: #94A3B8; margin: 8px 0 0 0; font-size: 16px; font-weight: 300;">사용자별 분석 이력 격리 — 본인 분석만 본인에게 보입니다</p>
</div>""", unsafe_allow_html=True)

    tab_login, tab_register = st.tabs(["🔑 로그인", "📝 회원가입"])

    with tab_login:
        with st.form("login_form"):
            u = st.text_input("아이디", key="login_u")
            p = st.text_input("비밀번호", type="password", key="login_p")
            if st.form_submit_button("로그인", use_container_width=True):
                ok, err = auth_login(u, p)
                if ok:
                    st.success("로그인 성공!")
                    st.rerun()
                else:
                    st.error(f"❌ {err}")

    with tab_register:
        # [P1-4] 백엔드 검증 (영문+숫자 8자 이상)과 일치
        st.caption("아이디 3~30자, 비밀번호 **8자 이상 + 영문·숫자 모두 포함** (예: Test1234)")
        with st.form("register_form"):
            u = st.text_input("새 아이디", key="reg_u")
            p = st.text_input("새 비밀번호", type="password", key="reg_p")
            p2 = st.text_input("비밀번호 확인", type="password", key="reg_p2")
            if st.form_submit_button("회원가입", use_container_width=True):
                if p != p2:
                    st.error("❌ 비밀번호가 일치하지 않습니다")
                else:
                    ok, err = auth_register(u, p)
                    if ok:
                        st.success("회원가입 완료! 자동 로그인됩니다.")
                        st.rerun()
                    else:
                        st.error(f"❌ {err}")


# ========== J4 공유 링크 페이지 (게이트 우회 — 누구나 접근 가능) ==========
# URL이 ?share=<토큰>이면 인증 없이 read-only 결과 카드 1건만 표시.
_qp = st.query_params if hasattr(st, "query_params") else st.experimental_get_query_params()
_share_token = _qp.get("share") if isinstance(_qp, dict) else None
if isinstance(_share_token, list):
    _share_token = _share_token[0] if _share_token else None

if _share_token:
    st.markdown("""<div style="background: linear-gradient(135deg, #0F1B2D 0%, #1E3A5F 100%); padding: 32px 48px; border-radius: 20px; margin-bottom: 24px;">
<h1 style="color: #FFFFFF; margin: 0; font-size: 28px;">📰 SSAK3 공유 분석 결과</h1>
<p style="color: #94A3B8; margin: 8px 0 0 0; font-size: 14px;">공유된 read-only 분석 결과입니다. 본인이 분석을 시도하려면 메인 페이지로 이동하세요.</p>
</div>""", unsafe_allow_html=True)
    try:
        _r = requests.get(f"{API_URL}/share/{_share_token}", timeout=10)
        if _r.status_code != 200:
            st.error(f"❌ 공유 결과를 불러올 수 없습니다: {_r.json().get('error', '오류')}")
            if st.button("메인 페이지로"):
                st.query_params.clear() if hasattr(st, "query_params") else None
                st.rerun()
            st.stop()
        _shared = _r.json()
        # 핵심 정보 카드
        _grade_color = {"신뢰 가능": "#10B981", "주의 필요": "#F59E0B", "의심 기사": "#EF4444", "신뢰 낮음": "#6B7280"}.get(_shared.get("grade","-"), "#94A3B8")
        st.markdown(f"""<div style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:16px;padding:32px;box-shadow:0 2px 12px rgba(0,0,0,0.06);">
<h2 style="margin:0 0 8px 0;color:#1E293B;">{safe(_shared.get('title','-'))}</h2>
<p style="font-size:13px;color:#94A3B8;margin:0 0 24px 0;">{safe(_shared.get('url',''))}<br>분석: {safe(_shared.get('analyzed_at','-'))}</p>
<div style="display:flex;align-items:center;gap:24px;margin-bottom:24px;">
<div style="font-size:64px;font-weight:800;color:{_grade_color};">{_shared.get('total_score',0):.1f}<span style="font-size:24px;color:#94A3B8;">/100</span></div>
<div><span style="display:inline-block;padding:6px 18px;border-radius:20px;background:{_grade_color};color:white;font-weight:700;">{safe(_shared.get('grade','-'))}</span></div>
</div>
<table style="width:100%;border-collapse:collapse;font-size:14px;">
<tr><td style="padding:8px 0;color:#64748B;">본문 일치도 (45%)</td><td style="padding:8px 0;text-align:right;font-weight:600;">{_shared.get('content_score',0):.1f}점</td></tr>
<tr><td style="padding:8px 0;color:#64748B;border-top:1px solid #F1F5F9;">자극성 분석 (35%)</td><td style="padding:8px 0;text-align:right;font-weight:600;border-top:1px solid #F1F5F9;">{_shared.get('provocative_score',0):.1f}점</td></tr>
<tr><td style="padding:8px 0;color:#64748B;border-top:1px solid #F1F5F9;">출처 신뢰도 (20%)</td><td style="padding:8px 0;text-align:right;font-weight:600;border-top:1px solid #F1F5F9;">{_shared.get('source_score',0):.1f}점</td></tr>
</table>
<p style="font-size:12px;color:#94A3B8;margin-top:16px;">출처: {safe(_shared.get('source_name','-'))}</p>
</div>""", unsafe_allow_html=True)
        st.markdown("---")
        if st.button("📊 본인 분석을 시작하려면 메인 페이지로"):
            try:
                st.query_params.clear()
            except Exception:
                pass
            st.rerun()
    except Exception as e:
        st.error(f"❌ API 연결 실패: {e}")
    st.stop()


# ── 인증 게이트 — 회원만 진입 (비회원 모드 폐지) ──
# auth_user_info() 내부에서 URL ?t=토큰 자동 복원 → 새로고침 시에도 세션 유지.
if not auth_user_info():
    render_login_gate()
    st.stop()

# ================================================================
# 글로벌 CSS 주입 — 네이비(#0F1B2D) + 틸(#0D9488) 테마
# st.markdown 안의 HTML/CSS는 반드시 왼쪽 정렬해야 한다.
# Markdown은 4칸 이상 들여쓴 텍스트를 코드 블록으로 인식하기 때문.
# ================================================================
st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700&display=swap');
html, body, [class*="css"] { font-family: 'Noto Sans KR', sans-serif; }
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
/* [사이드바 고정] 접기 버튼 자체를 숨겨 사이드바를 항상 펼친 상태로 유지.
   원인: header{visibility:hidden}로 펼치기 버튼이 가려져 한번 접으면 다시 못 엶.
   해결: 접기 버튼(>)을 없애 시연 중 실수로 접는 일 자체를 차단. */
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] {
    display: none !important;
}
/* 혹시 접힌 상태로 진입하더라도 펼치기 컨트롤은 보이도록 백업 (위와 상충 시 아래 우선) */
[data-testid="stSidebar"][aria-expanded="false"] ~ [data-testid="stSidebarCollapsedControl"] {
    display: flex !important;
    visibility: visible !important;
    z-index: 999999 !important;
    color: #0D9488 !important;
    background: #FFFFFF !important;
    border: 1px solid #0D9488 !important;
    border-radius: 8px !important;
}
[data-testid="stSidebar"] { background-color: #0F1B2D !important; }
[data-testid="stSidebar"] * { color: #E2E8F0 !important; }
[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,0.1) !important; }
[data-testid="stSidebar"] [role="radiogroup"] label[data-checked="true"] {
    color: #0D9488 !important; font-weight: 700 !important;
}
[data-testid="stForm"] {
    background: #F8FAFC;
    border: 1px solid #E2E8F0 !important;
    border-radius: 16px !important;
    padding: 28px 32px !important;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    margin-bottom: 24px;
}
.summary-card {
    border-radius: 16px; padding: 24px; text-align: center;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06); border: 1px solid #E2E8F0;
}
.summary-card .icon { font-size: 32px; margin-bottom: 4px; }
.summary-card .value { font-size: 32px; font-weight: 700; margin: 4px 0; }
.summary-card .label { font-size: 14px; color: #64748B; font-weight: 400; }
.article-card {
    background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 16px;
    padding: 0; margin-bottom: 20px; box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    overflow: hidden; display: flex;
}
.article-card .grade-bar { width: 6px; flex-shrink: 0; }
.article-card .card-body { padding: 24px 28px; flex: 1; }
.article-card .card-title {
    font-size: 17px; font-weight: 700; color: #1E293B;
    margin-bottom: 6px; line-height: 1.5;
}
.article-card .card-meta { font-size: 13px; color: #94A3B8; margin-bottom: 16px; }
.gauge-wrap { text-align: center; }
.gauge-wrap svg { display: block; margin: 0 auto; }
.gauge-score { font-size: 30px; font-weight: 800; }
.gauge-unit { font-size: 12px; fill: #94A3B8; }
.gauge-grade {
    display: inline-block; margin-top: 6px; padding: 3px 14px; border-radius: 20px;
    font-size: 12px; font-weight: 700; color: #fff;
}
.indicator-row { display: flex; align-items: center; margin: 8px 0; }
.indicator-label { width: 150px; font-size: 13px; font-weight: 500; color: #475569; flex-shrink: 0; }
.indicator-track { flex: 1; background: #F1F5F9; border-radius: 8px; height: 14px; overflow: hidden; }
.indicator-fill { height: 100%; border-radius: 8px; transition: width 0.5s ease; }
.indicator-value {
    width: 50px; text-align: right; font-size: 13px; font-weight: 700;
    color: #334155; flex-shrink: 0; padding-left: 10px;
}
.grade-badge {
    display: inline-block; padding: 4px 14px; border-radius: 20px;
    font-size: 13px; font-weight: 700; color: #fff;
}
.article-card.failed { opacity: 0.55; }
.article-card.failed .card-body { background: #F8FAFC; }
.stFormSubmitButton > button {
    background-color: #0D9488 !important; color: white !important;
    border: none !important; border-radius: 8px !important;
    padding: 8px 32px !important; font-weight: 600 !important;
}
.stFormSubmitButton > button:hover { background-color: #0F766E !important; }
.section-title {
    font-size: 20px; font-weight: 700; color: #0F1B2D;
    margin: 32px 0 16px 0; padding-bottom: 8px;
    border-bottom: 2px solid #0D9488; display: inline-block;
}
.evidence-section {
    background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px;
    padding: 14px 18px; margin-top: 12px;
}
.evidence-title {
    font-size: 13px; font-weight: 700; color: #0F1B2D; margin-bottom: 8px;
}
.evidence-row {
    font-size: 12px; color: #475569; line-height: 1.8;
}
.kw-tag {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600; margin: 2px;
}
.kw-tag.matched { background: #D1FAE5; color: #065F46; }
.kw-tag.missed { background: #FEE2E2; color: #991B1B; }
.prov-tag {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600; margin: 2px;
    background: #FEF3C7; color: #92400E;
}
.cat-label {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 10px; font-weight: 700; margin-right: 4px;
    background: #E2E8F0; color: #334155;
}
</style>""", unsafe_allow_html=True)

# ================================================================
# 페이지 상단 헤더 — 네이비 배경, 흰색 텍스트
# HTML은 반드시 왼쪽 정렬 (들여쓰기 금지 — Markdown 코드 블록 방지)
# ================================================================
st.markdown("""<div style="background: linear-gradient(135deg, #0F1B2D 0%, #1E3A5F 100%); padding: 40px 48px; border-radius: 20px; margin-bottom: 32px; box-shadow: 0 4px 20px rgba(15,27,45,0.3);">
<h1 style="color: #FFFFFF; margin: 0; font-size: 36px; font-weight: 700; letter-spacing: -0.5px;">📰 뉴스 신뢰도 분석 시스템</h1>
<p style="color: #94A3B8; margin: 8px 0 0 0; font-size: 16px; font-weight: 300;">규칙 기반 분석 + AI 보조지표 결합 &nbsp;|&nbsp; 팀 싹쓰리</p>
</div>""", unsafe_allow_html=True)

# ================================================================
# 사이드바 — 페이지 네비게이션 + 필터 + 시스템 상태
# ================================================================

# ── 사이드바 로고/타이틀 ──
st.sidebar.markdown("""<div style="text-align:center; padding: 16px 0 8px 0;">
<span style="font-size: 36px;">📰</span>
<h3 style="margin: 4px 0 0 0; font-weight: 700; letter-spacing: -0.3px;">SSAK3</h3>
<p style="font-size: 12px; opacity: 0.6; margin: 0;">News Credibility Analyzer</p>
</div>""", unsafe_allow_html=True)

# ── Phase G — 로그인 상태 표시 + 로그아웃 ──
_user = auth_user_info()
if _user:
    st.sidebar.markdown(
        f"""<div style="text-align:center; padding: 8px 0; background: #0D9488; border-radius: 8px; margin: 0 0 8px 0;">
<span style="color: white; font-weight: 600;">👤 {_user['username']}</span>
</div>""",
        unsafe_allow_html=True,
    )
    if st.sidebar.button("로그아웃", use_container_width=True):
        auth_logout()
        st.rerun()

st.sidebar.markdown("---")

# ── 페이지 네비게이션 ──
# 회원만 "내 통계", "계정 설정" 페이지 노출
_pages = ["📊 분석 대시보드", "🏎️ 성능 측정"]
if auth_user_info():
    _pages += ["📈 내 통계", "⚙️ 계정 설정"]
page_mode = st.sidebar.radio(
    "페이지 선택",
    _pages,
    label_visibility="collapsed"
)
st.sidebar.markdown("---")

# ── 1) 등급 필터 ──
st.sidebar.markdown("**🔍 등급 필터**")
grade_filter = st.sidebar.radio(
    "표시할 등급 선택",
    ["전체", "신뢰 가능", "주의 필요", "의심 기사", "신뢰 낮음"],
    label_visibility="collapsed"
)

st.sidebar.markdown("---")

# ── 2) 날짜 범위 필터 ──
st.sidebar.markdown("**📅 날짜 범위**")
date_start = st.sidebar.date_input("시작일", value=None, key="date_start")
date_end = st.sidebar.date_input("종료일", value=None, key="date_end")

st.sidebar.markdown("---")

# ── 3) 키워드 검색 ──
st.sidebar.markdown("**🔎 키워드 검색**")
keyword_search = st.sidebar.text_input(
    "제목에서 검색",
    placeholder="검색어 입력…",
    label_visibility="collapsed"
)

st.sidebar.markdown("---")

# ================================================================
# 사용자화 패널 — 분야 프리셋(E4) + 가중치(E2) + 등급 임계치(E6)
# ================================================================
# 분석 결과의 raw 점수(content/provocative/source)는 DB에 저장된 그대로 두고,
# 종합 점수와 등급만 사용자 설정에 따라 즉시 재계산.
# 학계 기본값(45/35/20, 80/60/40)을 유지하면서 사용자 맞춤 모드 제공.
st.sidebar.markdown("**🎚️ 분석 사용자화**")

# ── 분야별 프리셋 (E4) — 도메인 적응 ──
# 분야 특성을 반영하여 가중치와 임계치를 자동 전환:
# - 정치: 출처 신뢰도 비중↑, 임계치 엄격 (가짜뉴스 영향력 큼)
# - 경제: 본문 일치도 비중↑ (수치/사실 정확성 중요)
# - 연예: 자극성 비중↑, 임계치 완화 (선정적 표현 빈도 높음 감안)
# - 일반: 학계 기본값 (45/35/20, 80/60/40)
PRESETS = {
    "일반 (기본값)": {"w": (45, 35, 20), "th": (80, 60, 40)},
    "정치 — 출처 엄격": {"w": (40, 30, 30), "th": (85, 65, 45)},
    "경제 — 본문 정확성 중시": {"w": (50, 30, 20), "th": (80, 60, 40)},
    "연예 — 자극성 비중↑": {"w": (35, 45, 20), "th": (75, 55, 35)},
}
preset_choice = st.sidebar.selectbox(
    "분야 프리셋",
    list(PRESETS.keys()),
    index=0,
    key="preset_choice",
    help="분야별로 가중치와 등급 임계치가 자동 조정됩니다",
)

# 프리셋이 변경되면 세션 슬라이더 값을 일괄 갱신 (선택 즉시 반영)
_p = PRESETS[preset_choice]
if st.session_state.get("_active_preset") != preset_choice:
    st.session_state["_active_preset"] = preset_choice
    st.session_state["w_content"] = _p["w"][0]
    st.session_state["w_prov"] = _p["w"][1]
    st.session_state["w_source"] = _p["w"][2]
    st.session_state["th_reliable"] = _p["th"][0]
    st.session_state["th_caution"] = _p["th"][1]
    st.session_state["th_suspect"] = _p["th"][2]

with st.sidebar.expander("가중치 / 임계치 세부조정", expanded=False):
    st.markdown("**가중치 (합계 100%)**")
    w_content = st.slider("본문 일치도", 0, 100, _p["w"][0], step=5, key="w_content")
    w_prov = st.slider("자극성 분석", 0, 100, _p["w"][1], step=5, key="w_prov")
    w_source = st.slider("출처 신뢰도", 0, 100, _p["w"][2], step=5, key="w_source")

    w_sum = w_content + w_prov + w_source
    if w_sum != 100:
        st.warning(f"⚠️ 가중치 합계 {w_sum}% (100%로 정규화하여 적용)")

    st.markdown("**등급 임계치**")
    th_reliable = st.slider("신뢰 가능 ≥", 60, 95, _p["th"][0], step=5, key="th_reliable")
    th_caution = st.slider("주의 필요 ≥", 40, 75, _p["th"][1], step=5, key="th_caution")
    th_suspect = st.slider("의심 기사 ≥", 20, 55, _p["th"][2], step=5, key="th_suspect")

    if not (th_reliable > th_caution > th_suspect):
        st.error("⚠️ 임계치는 신뢰 > 주의 > 의심 순으로 내려가야 합니다")

    st.caption("프리셋 변경 시 자동 적용됩니다. 기본값 복원은 분야 프리셋을 '일반(기본값)'으로 선택하세요.")

# 정규화된 사용자 가중치 (raw 점수 → 종합 점수 재계산용)
_w_total = max(1, w_content + w_prov + w_source)
USER_W_CONTENT = w_content / _w_total
USER_W_PROV = w_prov / _w_total
USER_W_SOURCE = w_source / _w_total
USER_TH_RELIABLE = th_reliable
USER_TH_CAUTION = th_caution
USER_TH_SUSPECT = th_suspect


def recompute_score(content, prov, source):
    """사용자 가중치로 종합 점수 재계산"""
    return round(
        (content or 0) * USER_W_CONTENT
        + (prov or 0) * USER_W_PROV
        + (source or 0) * USER_W_SOURCE,
        1,
    )


def recompute_grade(score):
    """사용자 임계치로 등급 재판정"""
    if score >= USER_TH_RELIABLE:
        return "신뢰 가능"
    elif score >= USER_TH_CAUTION:
        return "주의 필요"
    elif score >= USER_TH_SUSPECT:
        return "의심 기사"
    else:
        return "신뢰 낮음"


# 사용자화 모드 표시 (발표 멘트용 — 학계 기본값 + 분야 적응 + 사용자 미세조정)
_is_default = (
    preset_choice == "일반 (기본값)"
    and w_content == 45 and w_prov == 35 and w_source == 20
    and th_reliable == 80 and th_caution == 60 and th_suspect == 40
)
if not _is_default:
    st.sidebar.success(
        f"🎚️ 사용자화 모드 [{preset_choice.split(' —')[0]}] — "
        f"가중치 {w_content}/{w_prov}/{w_source} · "
        f"임계치 {th_reliable}/{th_caution}/{th_suspect}"
    )
else:
    st.sidebar.caption("학계 기본값(45/35/20, 80/60/40)으로 분석 표시 중")

# ── 사용자 프로필 (E1, 경량) — 분석 이력 라벨링 ──
# 실제 인증 시스템 대신, 발표용 가벼운 "프로필 이름" 라벨링.
# 분석 요청 시 user_label로 함께 전송되어 DB에 저장 → 본인 분석만 필터 가능.
# 향후 OAuth/JWT 기반 정식 로그인으로 확장 가능한 구조.
with st.sidebar.expander("👤 프로필 (분석 이력 라벨)", expanded=False):
    user_label = st.text_input(
        "프로필 이름",
        value=st.session_state.get("user_label", ""),
        placeholder="예: 명호 / 익명 비워두기",
        key="user_label",
        help="입력 시 분석 결과에 라벨이 함께 저장되어 본인 분석만 필터할 수 있습니다",
    )
    only_mine = st.checkbox(
        "내 분석만 보기",
        value=False,
        disabled=not user_label,
        help="프로필 이름이 입력되어야 활성화됩니다",
    )

USER_LABEL = (user_label or "").strip()
ONLY_MINE = only_mine and bool(USER_LABEL)

# ── 사용자 자극성 사전 (E3) — 추가/면제 단어 ──
# 시스템 기본 사전(과장/혐오/선정/공포 4카테고리) 외에 사용자가 도메인 특화
# 단어를 추가하거나 일반 사전 중 일부를 면제 처리할 수 있음.
# 결과 카드의 자극성 근거 섹션에서 시각적으로 강조 표시.
with st.sidebar.expander("📝 사용자 자극성 사전", expanded=False):
    user_extra = st.text_area(
        "추가 단어 (콤마로 구분)",
        value=st.session_state.get("user_extra", ""),
        placeholder="예: 폭락, 패닉셀, 떡상",
        key="user_extra",
        height=68,
        help="본문/제목에 등장하면 자극성 근거에 추가로 표시됩니다",
    )
    user_exempt = st.text_area(
        "면제 단어 (콤마로 구분)",
        value=st.session_state.get("user_exempt", ""),
        placeholder="예: 충격, 경고",
        key="user_exempt",
        height=68,
        help="시스템 사전에서 검출되더라도 면제 처리하여 강조에서 제외됩니다",
    )

USER_EXTRA_WORDS = [w.strip() for w in (user_extra or "").split(",") if w.strip()]
USER_EXEMPT_WORDS = [w.strip() for w in (user_exempt or "").split(",") if w.strip()]

st.sidebar.markdown("---")

# ================================================================
# DB에서 분석 결과 로드
# ================================================================
def load_data():
    """API를 통해 본인 분석 결과만 가져오기 (Phase G — user_id 격리).

    이전: SQLite를 직접 SELECT해서 모든 사용자의 결과를 봤음 → 격리 위반.
    변경: API의 /results를 호출하면 서버가 Authorization 토큰으로 본인 결과만 필터.
    """
    try:
        r = requests.get(f"{API_URL}/results", headers=auth_headers(), timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return pd.DataFrame(data)
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

df = load_data()

# ── 사이드바 하단: 시스템 상태 ──
st.sidebar.markdown("**⚙️ 시스템 상태**")
total_count = len(df) if not df.empty else 0
done_count = len(df[df['status'] == 'done']) if not df.empty else 0
failed_count = len(df[df['status'] == 'failed']) if not df.empty else 0
st.sidebar.markdown(f"""<div style="font-size: 13px; line-height: 2;">
📊 총 분석 건수: <b>{total_count}건</b><br>
✅ 성공: <b>{done_count}건</b><br>
❌ 실패: <b>{failed_count}건</b>
</div>""", unsafe_allow_html=True)

# ── 사이드바: 캐시 적중률 (논문 abstract "캐싱 최적화" 효과) ──
# 가장 최근 분석 결과의 cache_stats 스냅샷을 사이드바에 표시.
# 발표 시연 시 캐시 효과를 정량적으로 보여줄 수 있다.
if not df.empty and 'cache_stats' in df.columns:
    latest_cache = None
    for _, row in df.iterrows():
        cs_raw = row.get('cache_stats')
        if cs_raw:
            try:
                latest_cache = json.loads(cs_raw)
                break
            except Exception:
                continue
    if latest_cache:
        nli = latest_cache.get('nli', {})
        sent = latest_cache.get('sentiment', {})
        src = latest_cache.get('source', {})
        st.sidebar.markdown("---")
        st.sidebar.markdown("**⚡ 캐시 적중률**")
        st.sidebar.markdown(f"""<div style="font-size: 12px; line-height: 1.8;">
🧠 NLI: <b>{nli.get('hit_rate_pct', 0)}%</b> ({nli.get('hits',0)}/{nli.get('hits',0)+nli.get('misses',0)})<br>
💬 감성: <b>{sent.get('hit_rate_pct', 0)}%</b> ({sent.get('hits',0)}/{sent.get('hits',0)+sent.get('misses',0)})<br>
🌐 출처: <b>{src.get('hit_rate_pct', 0)}%</b> ({src.get('hits',0)}/{src.get('hits',0)+src.get('misses',0)})
</div>
<div style="font-size: 10px; color: #94A3B8; margin-top: 4px;">
최근 처리 Worker 기준 / 컨테이너별 LRU 캐시
</div>""", unsafe_allow_html=True)

# ================================================================
# 성능 측정 페이지 — 벤치마크 결과 시각화
# ================================================================
if page_mode == "🏎️ 성능 측정":
    st.markdown('<div class="section-title">🏎️ Worker 스케일링 성능 비교</div>', unsafe_allow_html=True)

    # ── 벤치마크 결과 JSON 로드 ──
    BENCH_PATH = "/app/data/benchmark_results.json"
    bench_data = None
    try:
        if os.path.exists(BENCH_PATH):
            with open(BENCH_PATH, "r", encoding="utf-8") as f:
                bench_data = json.load(f)
    except Exception:
        pass

    if bench_data is None:
        st.markdown("""<div style="text-align: center; padding: 60px 20px; color: #94A3B8;">
<p style="font-size: 48px; margin-bottom: 16px;">🏎️</p>
<p style="font-size: 18px; font-weight: 500;">벤치마크 결과가 없습니다</p>
<p style="font-size: 14px;">호스트에서 아래 명령을 실행해주세요:</p>
<pre style="background: #1E293B; color: #E2E8F0; padding: 16px; border-radius: 8px; text-align: left; display: inline-block; margin-top: 12px;">python3 benchmark.py</pre>
</div>""", unsafe_allow_html=True)
        st.stop()

    results = bench_data.get("results", [])
    num_articles = bench_data.get("num_articles", 0)
    bench_time = bench_data.get("timestamp", "")

    st.markdown(f"""<div style="background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 12px; padding: 16px 24px; margin-bottom: 24px;">
<span style="font-size: 13px; color: #64748B;">측정 시각: <b>{bench_time}</b> &nbsp;|&nbsp; 테스트 기사: <b>{num_articles}개</b></span>
</div>""", unsafe_allow_html=True)

    # ── 요약 카드 3개 ──
    if results:
        cols = st.columns(len(results))
        for i, r in enumerate(results):
            with cols[i]:
                # Worker 1개 대비 속도 향상 비율 계산
                speedup = results[0]["total_time"] / r["total_time"] if r["total_time"] > 0 else 1
                speedup_text = f"{speedup:.1f}x" if i > 0 else "기준"
                card_border = "#0D9488" if i == len(results) - 1 else "#E2E8F0"
                st.markdown(f"""<div style="border: 2px solid {card_border}; border-radius: 16px; padding: 24px; text-align: center; box-shadow: 0 2px 12px rgba(0,0,0,0.06);">
<div style="font-size: 14px; color: #64748B; margin-bottom: 4px;">Worker {r['workers']}개</div>
<div style="font-size: 36px; font-weight: 800; color: #0F1B2D;">{r['total_time']}초</div>
<div style="font-size: 13px; color: #94A3B8; margin: 4px 0;">기사당 {r['avg_time']}초</div>
<div style="display: inline-block; padding: 3px 12px; border-radius: 20px; background: {'#D1FAE5' if i > 0 else '#F1F5F9'}; color: {'#065F46' if i > 0 else '#64748B'}; font-size: 12px; font-weight: 700; margin-top: 4px;">{speedup_text}</div>
</div>""", unsafe_allow_html=True)

    # ── 막대 그래프: 총 처리 시간 비교 ──
    st.markdown('<div class="section-title">총 처리 시간 비교</div>', unsafe_allow_html=True)
    if results:
        max_time = max(r["total_time"] for r in results)
        for r in results:
            bar_pct = (r["total_time"] / max_time * 100) if max_time > 0 else 0
            # Worker 수에 따른 색상 변화 (1: 빨강 → 3: 노랑 → 5: 초록)
            bar_colors = {1: "#EF4444", 3: "#F59E0B", 5: "#10B981"}
            bar_color = bar_colors.get(r["workers"], "#0D9488")
            st.markdown(f"""<div style="display: flex; align-items: center; margin: 12px 0;">
<span style="width: 100px; font-size: 14px; font-weight: 600; color: #334155;">Worker {r['workers']}개</span>
<div style="flex: 1; background: #F1F5F9; border-radius: 8px; height: 36px; overflow: hidden; margin: 0 16px;">
<div style="width: {bar_pct}%; background: linear-gradient(90deg, {bar_color}, {bar_color}CC); height: 100%; border-radius: 8px; display: flex; align-items: center; padding-left: 12px; min-width: 60px; transition: width 0.5s ease;">
<span style="color: #fff; font-size: 14px; font-weight: 700;">{r['total_time']}초</span>
</div></div>
<span style="width: 90px; font-size: 13px; color: #64748B; text-align: right;">avg {r['avg_time']}초</span>
</div>""", unsafe_allow_html=True)

    # ── 막대 그래프: 1000개 처리 예상 시간 ──
    st.markdown('<div class="section-title">1000개 기사 처리 예상 시간</div>', unsafe_allow_html=True)
    if results:
        max_est = max(r["est_1000_min"] for r in results)
        for r in results:
            bar_pct = (r["est_1000_min"] / max_est * 100) if max_est > 0 else 0
            bar_colors = {1: "#EF4444", 3: "#F59E0B", 5: "#10B981"}
            bar_color = bar_colors.get(r["workers"], "#0D9488")
            # 1시간 이상이면 시간 단위로도 표시
            time_label = f"{r['est_1000_min']}분"
            if r["est_1000_min"] >= 60:
                time_label += f" ({r['est_1000_min']/60:.1f}시간)"
            st.markdown(f"""<div style="display: flex; align-items: center; margin: 12px 0;">
<span style="width: 100px; font-size: 14px; font-weight: 600; color: #334155;">Worker {r['workers']}개</span>
<div style="flex: 1; background: #F1F5F9; border-radius: 8px; height: 36px; overflow: hidden; margin: 0 16px;">
<div style="width: {bar_pct}%; background: linear-gradient(90deg, {bar_color}, {bar_color}CC); height: 100%; border-radius: 8px; display: flex; align-items: center; padding-left: 12px; min-width: 80px; transition: width 0.5s ease;">
<span style="color: #fff; font-size: 14px; font-weight: 700;">{time_label}</span>
</div></div></div>""", unsafe_allow_html=True)

    # ── 결과 테이블 ──
    st.markdown('<div class="section-title">상세 결과 표</div>', unsafe_allow_html=True)
    if results:
        table_rows = ""
        for r in results:
            speedup = results[0]["total_time"] / r["total_time"] if r["total_time"] > 0 else 1
            table_rows += f"""<tr>
<td style="padding: 12px 16px; font-weight: 700;">{r['workers']}개</td>
<td style="padding: 12px 16px;">{r['total_time']}초</td>
<td style="padding: 12px 16px;">{r['avg_time']}초</td>
<td style="padding: 12px 16px;">{r['est_1000_min']}분</td>
<td style="padding: 12px 16px; color: #0D9488; font-weight: 700;">{speedup:.2f}x</td>
</tr>"""

        st.markdown(f"""<table style="width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,0.06);">
<thead><tr style="background: #0F1B2D; color: #E2E8F0;">
<th style="padding: 14px 16px; text-align: left; font-size: 13px;">Worker 수</th>
<th style="padding: 14px 16px; text-align: left; font-size: 13px;">총 처리 시간</th>
<th style="padding: 14px 16px; text-align: left; font-size: 13px;">기사당 평균</th>
<th style="padding: 14px 16px; text-align: left; font-size: 13px;">1000개 예상</th>
<th style="padding: 14px 16px; text-align: left; font-size: 13px;">속도 향상</th>
</tr></thead>
<tbody style="font-size: 14px; color: #334155;">{table_rows}</tbody>
</table>""", unsafe_allow_html=True)

    # ── 페이지 하단 푸터 ──
    st.markdown("""<div style="text-align: center; padding: 32px 0 16px 0; color: #94A3B8; font-size: 13px;">
SSAK3 — 규칙 기반 분석 + AI 보조지표 결합 뉴스 신뢰도 점수화 시스템
</div>""", unsafe_allow_html=True)

    st.stop()  # 성능 측정 페이지에서는 여기서 종료 — 아래의 분석 대시보드는 렌더링하지 않음


# ================================================================
# H2-4 내 통계 페이지 — 회원 전용 (총/평균/등급 분포/도메인 TOP)
# ================================================================
if page_mode == "📈 내 통계":
    st.markdown('<div class="section-title">📈 내 분석 통계</div>', unsafe_allow_html=True)
    _u = auth_user_info()
    st.caption(f"@{_u['username']} 님의 분석 이력 통계")

    _df = load_data()
    _df = _df[_df['status'] == 'done'] if not _df.empty else _df

    if _df.empty:
        st.info("아직 분석한 기사가 없습니다. 메인 대시보드에서 URL을 분석해보세요.")
        st.stop()

    # 통계 카드
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("총 분석 건수", f"{len(_df)}건")
    c2.metric("평균 신뢰도", f"{_df['total_score'].mean():.1f}점")
    c3.metric("신뢰 가능", f"{(_df['total_score'] >= 80).sum()}건")
    c4.metric("의심/낮음", f"{(_df['total_score'] < 60).sum()}건")

    st.markdown("---")

    # 도메인 TOP 10
    st.markdown("### 자주 분석한 도메인 TOP 10")
    from urllib.parse import urlparse as _up
    _df_domain = _df.copy()
    _df_domain['domain'] = _df_domain['url'].apply(
        lambda u: _up(u).netloc.replace('www.', '') if isinstance(u, str) else '-'
    )
    _top = _df_domain['domain'].value_counts().head(10)
    if not _top.empty:
        st.bar_chart(_top)
    else:
        st.write("(데이터 부족)")

    st.markdown("---")

    # J4 공유 링크 발급/철회
    st.markdown("### 🔗 분석 결과 공유 링크")
    st.caption("본인 분석 결과를 누구나 볼 수 있는 read-only 링크로 변환할 수 있습니다 (점수/등급/출처만 노출, 본인 정보는 비공개).")
    _share_df = _df.head(20).copy()
    if not _share_df.empty:
        _share_options = {
            f"#{int(r['id'])} {str(r.get('title',''))[:50]} ({r.get('total_score',0):.1f}점)": int(r['id'])
            for _, r in _share_df.iterrows()
        }
        _selected_label = st.selectbox(
            "공유할 분석 결과 선택 (최근 20건)",
            options=list(_share_options.keys()),
        )
        _selected_id = _share_options[_selected_label]
        _c1, _c2 = st.columns(2)
        with _c1:
            if st.button("🔗 공유 링크 발급", use_container_width=True):
                try:
                    _r = requests.post(
                        f"{API_URL}/results/{_selected_id}/share",
                        headers=auth_headers(), timeout=10
                    )
                    if _r.status_code == 200:
                        _t = _r.json().get("share_token")
                        # 학회 시연 시 사용 — 호스트 IP는 사용자가 직접 변경
                        # 기본은 현재 페이지 base url 추정 (브라우저 측에서 처리해야 정확)
                        _public = f"?share={_t}"
                        st.success("✅ 공유 링크 발급 완료")
                        st.code(_public, language=None)
                        st.caption("브라우저에서 현재 도메인 + 위 ?share=...을 붙여 접속하면 됩니다. 예: http://localhost:8501/?share=...")
                    else:
                        st.error(f"❌ {_r.json().get('error','실패')}")
                except Exception as _e:
                    st.error(f"❌ {_e}")
        with _c2:
            if st.button("🔒 공유 무효화", use_container_width=True):
                try:
                    _r = requests.post(
                        f"{API_URL}/results/{_selected_id}/unshare",
                        headers=auth_headers(), timeout=10
                    )
                    if _r.status_code == 200:
                        st.success("✅ 공유가 비활성화되었습니다")
                    else:
                        st.error(f"❌ {_r.json().get('error','실패')}")
                except Exception as _e:
                    st.error(f"❌ {_e}")

    st.markdown("---")

    # H2-3 CSV 다운로드
    st.markdown("### 📥 분석 이력 내보내기 (CSV)")
    _csv_cols = ['analyzed_at', 'url', 'title', 'total_score', 'grade',
                 'content_score', 'provocative_score', 'source_score', 'source_name']
    _csv_df = _df[[c for c in _csv_cols if c in _df.columns]].copy()
    _csv_bytes = _csv_df.to_csv(index=False).encode('utf-8-sig')
    st.download_button(
        "📥 내 분석 이력 CSV 다운로드",
        data=_csv_bytes,
        file_name=f"ssak3_history_{_u['username']}_{time.strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )
    st.caption("Excel에서 한글이 깨지지 않도록 UTF-8 with BOM으로 인코딩됩니다.")

    st.stop()


# ================================================================
# H2-1 + H2-2 계정 설정 페이지 — 비밀번호 변경 / 회원 탈퇴
# ================================================================
if page_mode == "⚙️ 계정 설정":
    st.markdown('<div class="section-title">⚙️ 계정 설정</div>', unsafe_allow_html=True)
    _u = auth_user_info()
    st.caption(f"로그인 계정: **@{_u['username']}** (user_id={_u['user_id']})")

    st.markdown("---")

    # H2-1 비밀번호 변경
    st.markdown("### 🔐 비밀번호 변경")
    st.caption("새 비밀번호는 **영문+숫자 포함 8자 이상**이어야 합니다. 변경 후 다른 디바이스는 자동 로그아웃됩니다.")
    with st.form("change_pw_form"):
        cur_pw = st.text_input("현재 비밀번호", type="password", key="cur_pw")
        new_pw = st.text_input("새 비밀번호", type="password", key="new_pw")
        new_pw2 = st.text_input("새 비밀번호 확인", type="password", key="new_pw2")
        if st.form_submit_button("비밀번호 변경"):
            if new_pw != new_pw2:
                st.error("❌ 새 비밀번호가 일치하지 않습니다")
            else:
                try:
                    r = requests.post(
                        f"{API_URL}/auth/change_password",
                        json={"current_password": cur_pw, "new_password": new_pw},
                        headers=auth_headers(),
                        timeout=10,
                    )
                    if r.status_code == 200:
                        # 새 토큰으로 자동 갱신
                        new_token = r.json().get("token")
                        if new_token:
                            st.session_state["auth_token"] = new_token
                        st.success("✅ 비밀번호 변경 완료. 다른 디바이스는 자동 로그아웃되었습니다.")
                    else:
                        st.error(f"❌ {r.json().get('error', '변경 실패')}")
                except Exception as e:
                    st.error(f"❌ API 연결 실패: {e}")

    st.markdown("---")

    # H2-2 회원 탈퇴
    st.markdown("### ❌ 회원 탈퇴")
    st.warning("⚠️ 탈퇴 시 본인의 모든 분석 이력과 작업 기록이 **영구 삭제**되며 복구할 수 없습니다.")
    with st.expander("회원 탈퇴 진행"):
        with st.form("delete_account_form"):
            del_pw = st.text_input("비밀번호 재확인", type="password", key="del_pw")
            confirm_text = st.text_input(
                f"탈퇴를 확인하려면 본인 아이디 '{_u['username']}'를 정확히 입력하세요",
                key="del_confirm",
            )
            if st.form_submit_button("회원 탈퇴 (영구)"):
                if confirm_text != _u['username']:
                    st.error("❌ 아이디가 일치하지 않습니다")
                else:
                    try:
                        r = requests.post(
                            f"{API_URL}/auth/delete_account",
                            json={"password": del_pw, "confirm": True},
                            headers=auth_headers(),
                            timeout=10,
                        )
                        if r.status_code == 200:
                            st.success(r.json().get("message", "탈퇴 완료"))
                            st.session_state.pop("auth_token", None)
                            st.session_state.pop("auth_user", None)
                            time.sleep(2)
                            st.rerun()
                        else:
                            st.error(f"❌ {r.json().get('error', '탈퇴 실패')}")
                    except Exception as e:
                        st.error(f"❌ API 연결 실패: {e}")

    st.stop()

# ================================================================
# 분석 프로세스 시각화 — 5단계 가로 스텝
# ================================================================
st.markdown("""<div style="background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 16px; padding: 28px 32px; margin-bottom: 32px; box-shadow: 0 2px 12px rgba(0,0,0,0.06);">
<p style="font-size: 15px; font-weight: 700; color: #0F1B2D; margin: 0 0 20px 0; padding-bottom: 8px; border-bottom: 2px solid #0D9488; display: inline-block;">분석 프로세스</p>
<div style="display: flex; align-items: center; justify-content: center; gap: 0; flex-wrap: wrap;">
<div style="flex: 1; min-width: 120px; text-align: center; padding: 12px 8px;">
<div style="width: 56px; height: 56px; border-radius: 50%; background: linear-gradient(135deg, #0D9488, #14B8A6); display: flex; align-items: center; justify-content: center; margin: 0 auto 10px auto; box-shadow: 0 3px 10px rgba(13,148,136,0.3);">
<span style="font-size: 24px;">🔗</span>
</div>
<p style="font-size: 13px; font-weight: 700; color: #0F1B2D; margin: 0 0 2px 0;">1. URL 입력</p>
<p style="font-size: 11px; color: #94A3B8; margin: 0;">뉴스 기사 URL을<br>입력합니다</p>
</div>
<div style="color: #0D9488; font-size: 22px; font-weight: 700; flex-shrink: 0; margin: 0 2px;">→</div>
<div style="flex: 1; min-width: 120px; text-align: center; padding: 12px 8px;">
<div style="width: 56px; height: 56px; border-radius: 50%; background: linear-gradient(135deg, #0D9488, #14B8A6); display: flex; align-items: center; justify-content: center; margin: 0 auto 10px auto; box-shadow: 0 3px 10px rgba(13,148,136,0.3);">
<span style="font-size: 24px;">📄</span>
</div>
<p style="font-size: 13px; font-weight: 700; color: #0F1B2D; margin: 0 0 2px 0;">2. 기사 크롤링</p>
<p style="font-size: 11px; color: #94A3B8; margin: 0;">제목·본문·날짜를<br>자동 수집합니다</p>
</div>
<div style="color: #0D9488; font-size: 22px; font-weight: 700; flex-shrink: 0; margin: 0 2px;">→</div>
<div style="flex: 1; min-width: 120px; text-align: center; padding: 12px 8px;">
<div style="width: 56px; height: 56px; border-radius: 50%; background: linear-gradient(135deg, #0D9488, #14B8A6); display: flex; align-items: center; justify-content: center; margin: 0 auto 10px auto; box-shadow: 0 3px 10px rgba(13,148,136,0.3);">
<span style="font-size: 24px;">🔍</span>
</div>
<p style="font-size: 13px; font-weight: 700; color: #0F1B2D; margin: 0 0 2px 0;">3. 3대 지표 분석</p>
<p style="font-size: 11px; color: #94A3B8; margin: 0;">키워드·자극성·출처<br>규칙+AI 보조 분석</p>
</div>
<div style="color: #0D9488; font-size: 22px; font-weight: 700; flex-shrink: 0; margin: 0 2px;">→</div>
<div style="flex: 1; min-width: 120px; text-align: center; padding: 12px 8px;">
<div style="width: 56px; height: 56px; border-radius: 50%; background: linear-gradient(135deg, #0D9488, #14B8A6); display: flex; align-items: center; justify-content: center; margin: 0 auto 10px auto; box-shadow: 0 3px 10px rgba(13,148,136,0.3);">
<span style="font-size: 24px;">📊</span>
</div>
<p style="font-size: 13px; font-weight: 700; color: #0F1B2D; margin: 0 0 2px 0;">4. 종합 점수 산출</p>
<p style="font-size: 11px; color: #94A3B8; margin: 0;">가중 평균으로<br>신뢰도 점수 계산</p>
</div>
<div style="color: #0D9488; font-size: 22px; font-weight: 700; flex-shrink: 0; margin: 0 2px;">→</div>
<div style="flex: 1; min-width: 120px; text-align: center; padding: 12px 8px;">
<div style="width: 56px; height: 56px; border-radius: 50%; background: linear-gradient(135deg, #0F1B2D, #1E3A5F); display: flex; align-items: center; justify-content: center; margin: 0 auto 10px auto; box-shadow: 0 3px 10px rgba(15,27,45,0.3);">
<span style="font-size: 24px;">✅</span>
</div>
<p style="font-size: 13px; font-weight: 700; color: #0F1B2D; margin: 0 0 2px 0;">5. 결과 표시</p>
<p style="font-size: 11px; color: #94A3B8; margin: 0;">등급·근거와 함께<br>결과를 제공합니다</p>
</div>
</div>
</div>""", unsafe_allow_html=True)

# ================================================================
# 등급 판별 헬퍼 함수
# ================================================================

# 등급별 색상 매핑 — PPT 테마와 통일
GRADE_COLORS = {
    "신뢰 가능": "#10B981",   # 에메랄드 그린
    "주의 필요": "#F59E0B",   # 앰버 옐로
    "의심 기사": "#F97316",   # 오렌지
    "신뢰 낮음": "#EF4444",   # 레드
    "분석 불가": "#9CA3AF",   # 그레이
}

def get_grade_color(grade):
    """등급 문자열에 따른 색상 반환"""
    return GRADE_COLORS.get(grade, "#9CA3AF")

def get_indicator_color(score):
    """개별 지표 점수에 따른 프로그레스 바 색상 (4단계)"""
    if score >= 75:
        return "#10B981"   # 초록
    elif score >= 50:
        return "#F59E0B"   # 노랑
    elif score >= 25:
        return "#F97316"   # 주황
    else:
        return "#EF4444"   # 빨강

def build_gauge_svg(score, grade, color):
    """SVG 원형 게이지 HTML 생성 (stroke-dasharray 기반)"""
    r = 50          # 반지름
    stroke = 8      # 테두리 두께
    size = (r + stroke) * 2
    cx = cy = r + stroke
    circumference = 2 * 3.14159 * r
    filled = circumference * score / 100
    gap = circumference - filled
    return f"""<div class="gauge-wrap">
<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">
<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#E2E8F0" stroke-width="{stroke}"/>
<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="{stroke}"
 stroke-dasharray="{filled:.1f} {gap:.1f}" stroke-linecap="round"
 transform="rotate(-90 {cx} {cy})" style="transition: stroke-dasharray 0.6s ease;"/>
<text x="{cx}" y="{cy - 4}" text-anchor="middle" class="gauge-score" fill="{color}">{score:.0f}</text>
<text x="{cx}" y="{cy + 14}" text-anchor="middle" class="gauge-unit">/ 100</text>
</svg>
<span class="gauge-grade" style="background:{color};">{grade}</span>
</div>"""

# ================================================================
# URL 입력 영역 — st.container + CSS 선택자로 카드 스타일 적용
# Streamlit은 별도 st.markdown의 열기/닫기 태그를 연결하지 않으므로
# [data-testid="stForm"] CSS 선택자로 form에 직접 스타일을 적용한다.
# ================================================================
with st.container():
    st.markdown("**🔗 뉴스 기사 분석 요청**")

    with st.form("analyze_form"):
        url_input = st.text_input(
            "분석할 뉴스 URL을 입력하세요",
            placeholder="https://n.news.naver.com/mnews/article/...",
            label_visibility="collapsed"
        )
        submitted = st.form_submit_button("🔍 분석 요청")

        if submitted:
            if not url_input or not url_input.startswith("http"):
                st.error("올바른 URL을 입력해주세요. (http:// 또는 https://로 시작)")
            else:
                try:
                    resp = requests.post(
                        f"{API_URL}/analyze",
                        json={"url": url_input, "user_label": USER_LABEL},
                        headers=auth_headers(),
                        timeout=5
                    )
                    if resp.status_code == 200:
                        result = resp.json()
                        # API에서 "이미 분석된 기사" 응답이 오면 다른 메시지 표시
                        if "이미 분석된" in result.get("message", ""):
                            st.info("ℹ️ 이미 분석된 기사입니다. 아래에서 결과를 확인하세요.")
                        else:
                            st.success("✅ 분석 요청 완료! 잠시 후 새로고침하면 결과가 표시됩니다.")
                    else:
                        st.error(f"요청 실패: {resp.text}")
                except Exception as e:
                    st.error(f"API 서버 연결 실패: {e}")

# ================================================================
# 대량 분석 입력 영역 — 여러 URL을 줄바꿈으로 입력
# ================================================================
with st.container():
    st.markdown("**📦 대량 뉴스 분석 요청**")

    with st.form("bulk_form"):
        bulk_input = st.text_area(
            "분석할 URL을 줄바꿈으로 입력하세요",
            placeholder="https://n.news.naver.com/article/001/...\nhttps://n.news.naver.com/article/002/...\nhttps://n.news.naver.com/article/003/...",
            height=150,
            label_visibility="collapsed"
        )
        bulk_submitted = st.form_submit_button("📦 대량 분석 요청")

        if bulk_submitted:
            # 줄바꿈으로 분리 후 빈 줄·공백 제거
            urls = [u.strip() for u in bulk_input.strip().splitlines() if u.strip()]
            # http로 시작하는 유효한 URL만 필터링
            valid_urls = [u for u in urls if u.startswith("http")]

            if not valid_urls:
                st.error("유효한 URL이 없습니다. http:// 또는 https://로 시작하는 URL을 입력해주세요.")
            else:
                try:
                    resp = requests.post(
                        f"{API_URL}/analyze/bulk",
                        json={"urls": valid_urls, "user_label": USER_LABEL},
                        headers=auth_headers(),
                        timeout=30
                    )
                    if resp.status_code == 200:
                        result = resp.json()
                        count = result.get("count", 0)
                        skipped = result.get("skipped", 0)
                        msg = f"✅ {count}개 분석 요청 완료!"
                        if skipped > 0:
                            msg += f" (중복 {skipped}건 건너뜀)"
                        st.success(msg)
                    else:
                        st.error(f"요청 실패: {resp.text}")
                except Exception as e:
                    st.error(f"API 서버 연결 실패: {e}")

# ── 대량 분석 진행 상황 표시 + 자동 새로고침 ──
auto_refresh = st.checkbox("🔄 5초마다 자동 새로고침", value=False)

# Phase G: 본인 분석 진행률을 API의 /jobs/summary로 조회 (사용자별 격리)
try:
    _resp = requests.get(f"{API_URL}/jobs/summary", headers=auth_headers(), timeout=5)
    _summary = _resp.json() if _resp.status_code == 200 else {}
    _done = _summary.get('done', 0)
    _fail = _summary.get('failed', 0)
    _pending = _summary.get('pending', 0)
    _processing = _summary.get('processing', 0)
    _total = _done + _fail + _pending + _processing
    if _total > 0:
        _processed = _done + _fail
        _progress = _processed / _total if _total > 0 else 0
        status_detail = f"전체: <b>{_total}건</b> &nbsp;|&nbsp; 완료: <b>{_done}건</b> &nbsp;|&nbsp; 실패: <b>{_fail}건</b>"
        if _pending > 0 or _processing > 0:
            status_detail += f" &nbsp;|&nbsp; 대기: <b>{_pending}건</b> &nbsp;|&nbsp; 처리중: <b>{_processing}건</b>"
        st.markdown(f"""<div style="background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 12px; padding: 16px 24px; margin-bottom: 24px;">
<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
<span style="font-size: 14px; font-weight: 600; color: #0F1B2D;">📊 분석 진행 상황</span>
<span style="font-size: 13px; color: #64748B;">{status_detail}</span>
</div>
<div style="background: #E2E8F0; border-radius: 8px; height: 12px; overflow: hidden;">
<div style="width: {_progress * 100:.1f}%; height: 100%; border-radius: 8px; background: linear-gradient(90deg, #0D9488, #14B8A6); transition: width 0.4s ease;"></div>
</div>
</div>""", unsafe_allow_html=True)
except Exception:
    pass

# 자동 새로고침 활성화 시 5초 후 페이지 재실행
if auto_refresh:
    time.sleep(5)
    st.rerun()

# ================================================================
# 데이터가 없으면 안내 메시지 표시 후 종료
# ================================================================
if df.empty:
    st.markdown("""<div style="text-align: center; padding: 60px 20px; color: #94A3B8;">
<p style="font-size: 48px; margin-bottom: 16px;">📭</p>
<p style="font-size: 18px; font-weight: 500;">아직 분석된 기사가 없습니다</p>
<p style="font-size: 14px;">위 입력창에서 뉴스 URL을 입력해보세요.</p>
</div>""", unsafe_allow_html=True)
    st.stop()

# ================================================================
# 사이드바 필터 적용 — 실패 기사 분리 후 3종 필터 결합
# ================================================================
failed_df = df[df['status'] == 'failed']
done_df_all = df[df['status'] == 'done'].copy()

# ── 프로필 필터 (E1) — "내 분석만 보기" 토글이 켜져 있으면 user_label로 필터 ──
if ONLY_MINE and 'user_label' in done_df_all.columns:
    done_df_all = done_df_all[done_df_all['user_label'].fillna('') == USER_LABEL]

# ── 사용자 가중치/임계치로 종합 점수·등급 재계산 (사이드바 슬라이더 반영) ──
# 필터/요약 카드/결과 카드가 모두 재계산된 값을 사용하도록 여기서 한 번에 적용.
if not done_df_all.empty:
    done_df_all['total_score'] = done_df_all.apply(
        lambda r: recompute_score(r.get('content_score'), r.get('provocative_score'), r.get('source_score')),
        axis=1,
    )
    done_df_all['grade'] = done_df_all['total_score'].apply(recompute_grade)

# 등급 필터
if grade_filter != "전체":
    filtered_df = done_df_all[done_df_all['grade'] == grade_filter]
else:
    filtered_df = done_df_all.copy()

# 날짜 범위 필터
if date_start is not None:
    filtered_df = filtered_df[
        pd.to_datetime(filtered_df['analyzed_at']).dt.date >= date_start
    ]
if date_end is not None:
    filtered_df = filtered_df[
        pd.to_datetime(filtered_df['analyzed_at']).dt.date <= date_end
    ]

# 키워드 검색 (제목)
if keyword_search:
    filtered_df = filtered_df[
        filtered_df['title'].str.contains(keyword_search, case=False, na=False)
    ]

# ================================================================
# 상단 요약 카드 4개 — 아이콘 + 숫자 + 라벨
# done 상태인 기사만 집계
# ================================================================
# done_df_all은 이미 사용자 재계산이 반영된 상태 (위 done_df_all 정의 직후 처리)
done_df = done_df_all

avg_score = done_df['total_score'].mean() if not done_df.empty else 0
reliable_count = len(done_df[done_df['total_score'] >= USER_TH_RELIABLE])
low_count = len(done_df[done_df['total_score'] < USER_TH_SUSPECT])

col1, col2, col3, col4 = st.columns(4)

# ── 카드 1: 분석된 기사 수 ──
with col1:
    st.markdown(f"""<div class="summary-card" style="background: linear-gradient(135deg, #EFF6FF, #DBEAFE);">
<div class="icon">📊</div>
<div class="value" style="color: #1E40AF;">{len(done_df)}</div>
<div class="label">분석된 기사</div>
</div>""", unsafe_allow_html=True)

# ── 카드 2: 평균 신뢰도 ──
with col2:
    st.markdown(f"""<div class="summary-card" style="background: linear-gradient(135deg, #F0FDFA, #CCFBF1);">
<div class="icon">📈</div>
<div class="value" style="color: #0D9488;">{avg_score:.1f}</div>
<div class="label">평균 신뢰도</div>
</div>""", unsafe_allow_html=True)

# ── 카드 3: 신뢰 가능 ──
with col3:
    st.markdown(f"""<div class="summary-card" style="background: linear-gradient(135deg, #F0FDF4, #DCFCE7);">
<div class="icon">✅</div>
<div class="value" style="color: #16A34A;">{reliable_count}</div>
<div class="label">신뢰 가능</div>
</div>""", unsafe_allow_html=True)

# ── 카드 4: 신뢰 낮음 ──
with col4:
    st.markdown(f"""<div class="summary-card" style="background: linear-gradient(135deg, #FEF2F2, #FECACA);">
<div class="icon">⚠️</div>
<div class="value" style="color: #DC2626;">{low_count}</div>
<div class="label">신뢰 낮음</div>
</div>""", unsafe_allow_html=True)

# ================================================================
# 등급별 분포 차트 — 색상 범례 + HTML 가로 막대
# ================================================================
st.markdown('<div class="section-title">📊 등급별 분포</div>', unsafe_allow_html=True)

grade_order = ["신뢰 가능", "주의 필요", "의심 기사", "신뢰 낮음"]
grade_counts = done_df['grade'].value_counts()
max_count = max([grade_counts.get(g, 0) for g in grade_order] + [1])

for g in grade_order:
    cnt = grade_counts.get(g, 0)
    color = GRADE_COLORS[g]
    # 막대 너비를 최대값 대비 비율로 계산 (최대 100%)
    bar_width = (cnt / max_count) * 100
    st.markdown(f"""<div style="display: flex; align-items: center; margin: 8px 0;">
<span style="width: 90px; font-size: 14px; font-weight: 600; color: #334155;">{g}</span>
<div style="flex: 1; background: #F1F5F9; border-radius: 6px; height: 28px; overflow: hidden; margin: 0 12px;">
<div style="width: {bar_width}%; background: {color}; height: 100%; border-radius: 6px; display: flex; align-items: center; padding-left: 10px; min-width: 30px;">
<span style="color: #fff; font-size: 13px; font-weight: 700;">{cnt}건</span>
</div></div></div>""", unsafe_allow_html=True)

# ================================================================
# 신뢰도 기준 설명
# ================================================================
st.markdown("""<div style="background: #F0FDFA; border: 1px solid #99F6E4; border-radius: 12px; padding: 16px 24px; margin-bottom: 24px;">
<p style="font-size: 14px; font-weight: 700; color: #0F766E; margin: 0 0 8px 0;">📐 신뢰도 점수 산출 기준</p>
<p style="font-size: 13px; color: #334155; margin: 0; line-height: 1.8;">
종합 점수 = <b>본문 일치도 (45%)</b> + <b>자극성 분석 (35%)</b> + <b>출처 신뢰도 (20%)</b><br>
규칙 기반 분석(키워드 매칭, 자극적 표현 사전, 출처 분류)과 AI 보조지표(KR-FinBert 감성분석)를 결합하여 뉴스 신뢰도를 점수화합니다.
</p>
</div>""", unsafe_allow_html=True)

# ================================================================
# Worker별 처리 현황 — 분산 병렬 처리 부하 분산 시각화
# 각 Worker가 몇 건의 기사를 분석했는지 막대 그래프로 표시한다.
# RabbitMQ의 Round-Robin 분배가 균등하게 이루어지는지 시각적으로 확인 가능.
# worker_id 컬럼이 없는 구버전 데이터는 "미분류"로 표시한다.
# ================================================================
st.markdown('<div class="section-title">🖥️ Worker별 처리 현황 (분산 병렬 처리)</div>', unsafe_allow_html=True)

# ── docker-compose.yml에 정의된 Worker 목록 (항상 표시할 Worker ID들) ──
# Worker-1, Worker-2, Worker-3은 처리 건수가 0이더라도 항상 그래프에 표시한다.
# 나중에 Worker를 추가하면 이 리스트만 확장하면 된다.
_known_workers = ['1', '2', '3']

# worker_id 컬럼이 있는지 확인 후 집계
if 'worker_id' in done_df.columns:
    # worker_id가 비어있거나 None인 경우 "미분류"로 대체
    _worker_col = done_df['worker_id'].fillna('미분류').replace('', '미분류')
    _worker_counts = _worker_col.value_counts()
else:
    # worker_id 컬럼 자체가 없는 경우 (마이그레이션 전 DB)
    _worker_counts = pd.Series({'미분류': len(done_df)} if len(done_df) > 0 else {})

# ── 알려진 Worker들을 0건으로 미리 채워 넣기 ──
# 처리 건수가 없는 Worker도 항상 그래프에 표시되도록 보장
for _kw in _known_workers:
    if _kw not in _worker_counts.index:
        _worker_counts[_kw] = 0
# 숫자 Worker ID 먼저(1,2,3 순), 그 외("미분류" 등)는 뒤에 정렬
_worker_counts = _worker_counts.reindex(
    sorted(_worker_counts.index, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 999))
)

# Worker가 하나도 없으면 안내 메시지 표시
if _worker_counts.empty or len(done_df) == 0:
    st.markdown("""<div style="text-align: center; padding: 24px; color: #94A3B8; font-size: 14px;">
아직 Worker가 처리한 기사가 없습니다.
</div>""", unsafe_allow_html=True)
else:
    # ── 막대 그래프 색상 (Worker별로 다른 색상) ──
    _worker_colors = {
        '1': '#0D9488',   # 틸 (Worker-1)
        '2': '#3B82F6',   # 블루 (Worker-2)
        '3': '#8B5CF6',   # 퍼플 (Worker-3)
        '4': '#F59E0B',   # 앰버 (Worker-4, 확장 대비)
        '5': '#EF4444',   # 레드 (Worker-5, 확장 대비)
    }
    # 최대 건수 (0이면 1로 설정하여 나눗셈 에러 방지)
    _max_worker_count = max(_worker_counts.max(), 1)
    # 전체 done 건수 (0이면 비율 계산 시 0%로 표시)
    _total_done = len(done_df) if len(done_df) > 0 else 1

    for _wid, _wcount in _worker_counts.items():
        _wid_str = str(_wid)
        # Worker ID에 맞는 색상 선택 (없으면 기본 회색)
        _wcolor = _worker_colors.get(_wid_str, '#94A3B8')
        _wbar_width = (_wcount / _max_worker_count) * 100
        # 전체 대비 이 Worker의 처리 비율 (%)
        _wpct = (_wcount / _total_done) * 100

        # Worker 이름 표시: 숫자면 "Worker-N", 아니면 그대로
        _wlabel = f"Worker-{_wid_str}" if _wid_str.isdigit() else _wid_str

        # 0건인 Worker는 막대 없이 텍스트만 회색으로 표시
        if _wcount == 0:
            st.markdown(f"""<div style="display: flex; align-items: center; margin: 10px 0;">
<span style="width: 100px; font-size: 14px; font-weight: 600; color: #334155;">{_wlabel}</span>
<div style="flex: 1; background: #F1F5F9; border-radius: 6px; height: 32px; overflow: hidden; margin: 0 12px; display: flex; align-items: center; padding-left: 12px;">
<span style="color: #94A3B8; font-size: 13px; font-weight: 600;">0건 (대기 중)</span>
</div></div>""", unsafe_allow_html=True)
        else:
            st.markdown(f"""<div style="display: flex; align-items: center; margin: 10px 0;">
<span style="width: 100px; font-size: 14px; font-weight: 600; color: #334155;">{_wlabel}</span>
<div style="flex: 1; background: #F1F5F9; border-radius: 6px; height: 32px; overflow: hidden; margin: 0 12px;">
<div style="width: {_wbar_width}%; background: {_wcolor}; height: 100%; border-radius: 6px; display: flex; align-items: center; padding-left: 12px; min-width: 50px;">
<span style="color: #fff; font-size: 13px; font-weight: 700;">{_wcount}건 ({_wpct:.1f}%)</span>
</div></div></div>""", unsafe_allow_html=True)

    # ── 부하 균등성 요약 메시지 ──
    # 표준편차가 작을수록 균등하게 분배된 것
    if len(_worker_counts) > 1:
        _mean_count = _worker_counts.mean()
        _std_count = _worker_counts.std()
        # 변동계수(CV)로 균등성 판단: 작을수록 균등
        _cv = (_std_count / _mean_count * 100) if _mean_count > 0 else 0
        if _cv < 15:
            _balance_msg = "✅ 부하가 매우 균등하게 분배되고 있습니다 (RabbitMQ Round-Robin)"
            _balance_color = "#0D9488"
        elif _cv < 30:
            _balance_msg = "⚠️ 부하가 비교적 균등하게 분배되고 있습니다"
            _balance_color = "#F59E0B"
        else:
            _balance_msg = "❌ 부하 분배가 불균등합니다 — Worker 상태를 확인하세요"
            _balance_color = "#EF4444"

        st.markdown(f"""<div style="background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px; padding: 12px 20px; margin: 8px 0 24px 0; font-size: 13px;">
<span style="color: {_balance_color}; font-weight: 600;">{_balance_msg}</span>
<span style="color: #94A3B8; margin-left: 12px;">평균 {_mean_count:.1f}건 · 표준편차 {_std_count:.1f}</span>
</div>""", unsafe_allow_html=True)

# ================================================================
# 성능 지표 섹션 — 처리 시간 기반 통계
# processing_time 컬럼에 기록된 기사별 처리 소요 시간(초)을 기반으로
# 평균 처리 시간, 총 처리 시간, 초당 처리 건수(throughput)를 계산한다.
# ================================================================
st.markdown('<div class="section-title">⚡ 처리 성능 지표</div>', unsafe_allow_html=True)

# processing_time 컬럼이 있고, 유효한 값이 1건 이상인 경우에만 표시
if 'processing_time' in done_df.columns:
    # None이나 NaN을 제외한 유효한 처리 시간만 추출
    _valid_times = done_df['processing_time'].dropna()
    _valid_times = _valid_times[_valid_times > 0]
else:
    _valid_times = pd.Series(dtype=float)

if len(_valid_times) > 0:
    _avg_time = _valid_times.mean()       # 평균 처리 시간 (초)
    _total_time = _valid_times.sum()      # 총 처리 시간 (초)
    _min_time = _valid_times.min()        # 최소 처리 시간 (초)
    _max_time = _valid_times.max()        # 최대 처리 시간 (초)
    _count = len(_valid_times)            # 처리 건수
    # throughput: 초당 처리 건수 = 총 건수 / 총 시간
    _throughput = _count / _total_time if _total_time > 0 else 0

    # 3개 카드로 핵심 지표 표시
    _pc1, _pc2, _pc3 = st.columns(3)
    with _pc1:
        st.markdown(f"""<div class="summary-card" style="background: linear-gradient(135deg, #FFF7ED, #FFEDD5);">
<div class="icon">⏱️</div>
<div class="value" style="color: #EA580C;">{_avg_time:.1f}초</div>
<div class="label">평균 처리 시간</div>
</div>""", unsafe_allow_html=True)
    with _pc2:
        # 총 처리 시간: 60초 이상이면 분:초로 표시
        if _total_time >= 60:
            _total_display = f"{int(_total_time // 60)}분 {_total_time % 60:.0f}초"
        else:
            _total_display = f"{_total_time:.1f}초"
        st.markdown(f"""<div class="summary-card" style="background: linear-gradient(135deg, #F5F3FF, #EDE9FE);">
<div class="icon">🕐</div>
<div class="value" style="color: #7C3AED;">{_total_display}</div>
<div class="label">총 처리 시간 ({_count}건)</div>
</div>""", unsafe_allow_html=True)
    with _pc3:
        st.markdown(f"""<div class="summary-card" style="background: linear-gradient(135deg, #ECFDF5, #D1FAE5);">
<div class="icon">🚀</div>
<div class="value" style="color: #059669;">{_throughput:.2f}</div>
<div class="label">초당 처리 건수 (throughput)</div>
</div>""", unsafe_allow_html=True)

    # 상세 수치 요약
    st.markdown(f"""<div style="background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px; padding: 12px 20px; margin: 8px 0 24px 0; font-size: 13px; color: #475569;">
최소 <b>{_min_time:.1f}초</b> · 최대 <b>{_max_time:.1f}초</b> · 평균 <b>{_avg_time:.1f}초</b> · Worker 3개 병렬 처리 기준
</div>""", unsafe_allow_html=True)
else:
    st.markdown("""<div style="text-align: center; padding: 24px; color: #94A3B8; font-size: 14px;">
처리 시간 데이터가 아직 없습니다. 기사를 분석하면 성능 지표가 표시됩니다.
</div>""", unsafe_allow_html=True)

# ================================================================
# 기사별 상세 결과 — 카드 형태 + 원형 게이지 + 프로그레스 바
# ================================================================
st.markdown('<div class="section-title">📋 기사별 분석 결과</div>', unsafe_allow_html=True)

if filtered_df.empty:
    st.markdown("""<div style="text-align: center; padding: 40px; color: #94A3B8;">
<p style="font-size: 16px;">조건에 맞는 기사가 없습니다.</p>
</div>""", unsafe_allow_html=True)
else:
    for _, row in filtered_df.iterrows():
        score = row['total_score']
        grade = row['grade']
        color = get_grade_color(grade)

        # ── 정상 기사 카드 ──
        content_s = row['content_score']
        provocative_s = row['provocative_score']
        source_s = row['source_score']

        # 본문 미리보기 (최대 150자, HTML 특수문자 이스케이프)
        body_text = str(row.get('body', ''))
        body_preview = body_text[:150] + "..." if len(body_text) > 150 else body_text
        body_preview = body_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # ── 분석 근거 JSON 파싱 (컬럼이 없거나 비어있으면 기본값 사용) ──
        try:
            kw_data = json.loads(row.get('matched_keywords', '') or '{}')
        except Exception:
            kw_data = {}
        try:
            prov_data = json.loads(row.get('detected_provocative', '') or '{}')
        except Exception:
            prov_data = {}
        try:
            ai_data = json.loads(row.get('ai_sentiment', '') or '{}')
        except Exception:
            ai_data = {}
        source_info = str(row.get('source_name', '') or '')

        # ── 섹션 1: 본문 일치도 근거 HTML 생성 (NLI + 코사인 + 키워드) ──
        all_kw = kw_data.get('keywords', [])
        matched_kw = kw_data.get('matched', [])
        negated_kw = kw_data.get('negated', [])  # 부정어로 매칭 취소된 키워드
        cosine_raw = kw_data.get('cosine_raw', 0)
        nli = kw_data.get('nli', {}) or {}
        nli_score_v = kw_data.get('nli_score', None)
        cosine_score_v = kw_data.get('cosine_score', None)
        keyword_score_v = kw_data.get('keyword_score', None)
        kw_tags = ""
        for kw in all_kw:
            # [P0-4 XSS] 제목에서 추출된 키워드도 외부 입력이므로 이스케이프
            _safe_kw = safe(kw)
            if kw in matched_kw:
                kw_tags += f'<span class="kw-tag matched">{_safe_kw} ✓</span>'
            elif kw in negated_kw:
                # 부정어로 매칭 취소된 키워드는 노란색으로 표시
                kw_tags += f'<span class="kw-tag" style="background:#FEF3C7;color:#92400E;">{_safe_kw} ⚠부정</span>'
            else:
                kw_tags += f'<span class="kw-tag missed">{_safe_kw} ✗</span>'

        # NLI 확률 시각화 (entailment/neutral/contradiction 막대)
        nli_html = ""
        if nli:
            ent = float(nli.get('entailment', 0)) * 100
            neu = float(nli.get('neutral', 0)) * 100
            con = float(nli.get('contradiction', 0)) * 100
            nli_html = f"""<div class="evidence-row" style="margin-top:6px;">
<b>🧠 NLI 함의 분석</b> — 제목이 본문 핵심을 논리적으로 포함하는지 판정
</div>
<div class="evidence-row" style="display:flex;gap:8px;align-items:center;font-size:11px;">
<span style="background:#D1FAE5;color:#065F46;padding:2px 8px;border-radius:4px;font-weight:600;">함의 {ent:.1f}%</span>
<span style="background:#FEF3C7;color:#92400E;padding:2px 8px;border-radius:4px;font-weight:600;">중립 {neu:.1f}%</span>
<span style="background:#FEE2E2;color:#991B1B;padding:2px 8px;border-radius:4px;font-weight:600;">모순 {con:.1f}%</span>
</div>"""

        # 보조 신호 점수 분해 (NLI 60% + 코사인 20% + 키워드 20%)
        breakdown_html = ""
        if nli_score_v is not None and cosine_score_v is not None and keyword_score_v is not None:
            breakdown_html = f"""<div class="evidence-row" style="font-size:11px;color:#64748B;margin-top:4px;">
세부 점수 — NLI <b>{nli_score_v}</b>(×0.6) + 코사인 <b>{cosine_score_v}</b>(×0.2) + 키워드 <b>{keyword_score_v}</b>(×0.2) = <b>{content_s:.1f}점</b>
</div>"""

        evidence1 = ""
        if all_kw or nli:
            match_pct = round(len(matched_kw) / len(all_kw) * 100, 1) if all_kw else 0
            negated_info = f" (부정어 근접으로 {len(negated_kw)}개 취소)" if negated_kw else ""
            keywords_row = f'<div class="evidence-row">제목 키워드: {kw_tags}</div><div class="evidence-row">본문에서 발견된 키워드: <b>{len(matched_kw)}개</b> / 전체 {len(all_kw)}개 (매칭률 <b>{match_pct}%</b>){negated_info}</div>' if all_kw else ''
            evidence1 = f"""<div class="evidence-section">
<div class="evidence-title">📝 본문 일치도 분석 근거</div>
{nli_html}
{keywords_row}
<div class="evidence-row">제목-본문 코사인 유사도: <b>{cosine_raw}</b></div>
{breakdown_html}
</div>"""

        # ── 섹션 2: 자극성 분석 근거 HTML 생성 ──
        # 사용자 사전(E3) 적용:
        # - USER_EXEMPT_WORDS에 든 단어는 시스템 검출이 있어도 "면제" 회색 표시
        # - USER_EXTRA_WORDS는 본문/제목에 등장 여부를 직접 검사하여 별도 표시
        detected = prov_data.get('detected', {})
        prov_ratio = prov_data.get('ratio', 0)
        prov_tags = ""
        for cat, words in detected.items():
            for w in words:
                if w in USER_EXEMPT_WORDS:
                    # 사용자 면제 — 회색 + 취소선
                    prov_tags += f'<span class="cat-label">{safe(cat)}</span><span class="prov-tag" style="background:#E2E8F0;color:#64748B;text-decoration:line-through;">{safe(w)} (면제)</span> '
                else:
                    prov_tags += f'<span class="cat-label">{safe(cat)}</span><span class="prov-tag">{safe(w)}</span> '

        # 사용자 추가 단어 — 본문/제목에 매칭되는지 직접 확인
        user_hit_tags = ""
        if USER_EXTRA_WORDS:
            body_text = (row.get('title') or '') + ' ' + (row.get('body') or '')
            for w in USER_EXTRA_WORDS:
                if w and w in body_text:
                    user_hit_tags += f'<span class="cat-label" style="background:#FEE2E2;color:#991B1B;">사용자</span><span class="prov-tag" style="background:#FEE2E2;color:#991B1B;border:1px solid #F87171;">{w}</span> '

        user_dict_row = ""
        if USER_EXTRA_WORDS or USER_EXEMPT_WORDS:
            user_dict_row = f"""<div class="evidence-row" style="font-size:11px;color:#64748B;margin-top:4px;">
사용자 사전: 추가 <b>{len(USER_EXTRA_WORDS)}개</b> · 면제 <b>{len(USER_EXEMPT_WORDS)}개</b>
{f'<div style="margin-top:4px;">사용자 추가 단어 검출: {user_hit_tags}</div>' if user_hit_tags else ''}
</div>"""

        ai_neutral = ai_data.get('neutral', 0)
        ai_positive = ai_data.get('positive', 0)
        ai_negative = ai_data.get('negative', 0)

        # F4: 점수 분해 (단어 기반 + AI 결합) — 산출식 가시화
        word_s = prov_data.get('word_score')
        ai_s = prov_data.get('ai_score')
        breakdown_prov = ""
        if word_s is not None and ai_s is not None:
            breakdown_prov = f"""<div class="evidence-row" style="font-size:11px;color:#64748B;margin-top:4px;">
세부 점수 — 단어사전 <b>{word_s}</b>(×0.5) + AI 논조 <b>{ai_s}</b>(×0.5) = <b>{provocative_s:.1f}점</b>
</div>"""

        evidence2 = f"""<div class="evidence-section">
<div class="evidence-title">⚡ 자극성 분석 근거</div>
<div class="evidence-row">감지된 자극적 표현: {prov_tags if prov_tags else '<span style="color:#94A3B8;">없음</span>'}</div>
{user_dict_row}
<div class="evidence-row">자극적 표현 비율: <b>{prov_ratio}%</b> (본문 대비)</div>
<div class="evidence-row">논조 분석 보조지표 (KR-FinBert): 중립 <b>{ai_neutral}%</b> / 부정 <b>{ai_negative}%</b> / 긍정 <b>{ai_positive}%</b></div>
{breakdown_prov}
<div class="evidence-row">최종 자극성 점수: <b>{provocative_s:.1f}점</b></div>
</div>"""

        # ── 섹션 3: 출처 신뢰도 근거 HTML 생성 ──
        src_parts = source_info.split('|') if source_info else ['', '']
        src_name = src_parts[0] if len(src_parts) > 0 else ''
        src_class = src_parts[1] if len(src_parts) > 1 else ''

        # [P0-4 XSS] 크롤링된 언론사명/분류도 이스케이프
        evidence3 = f"""<div class="evidence-section">
<div class="evidence-title">🏢 출처 신뢰도 근거</div>
<div class="evidence-row">원본 언론사: <b>{safe(src_name) if src_name else '확인 불가'}</b></div>
<div class="evidence-row">분류: <b>{safe(src_class) if src_class else '미분류'}</b></div>
<div class="evidence-row">최종 출처 점수: <b>{source_s:.1f}점</b></div>
</div>"""

        # ── 보조 신호: 외부 팩트체크 DB 유사도 (논문 2.2.2) ──
        # 점수 합산 X. 별도 신호로 표시.
        fact_html = ""
        try:
            fact_data = json.loads(row.get('factcheck_match', '') or 'null') if row.get('factcheck_match') else None
        except Exception:
            fact_data = None
        if fact_data and fact_data.get('similarity', 0) > 0:
            sim_pct = fact_data['similarity'] * 100
            mt = safe(fact_data.get('matched_title', ''))[:80]
            mu = safe(fact_data.get('matched_url', ''))
            has_false = fact_data.get('has_false_signal', False)
            if has_false:
                bg, border, icon, label = "#FEE2E2", "#DC2626", "🚨", "거짓 판정 사례와 유사"
            else:
                bg, border, icon, label = "#FEF3C7", "#D97706", "🔍", "팩트체크 검증 대상 주제와 유사"
            fact_html = f"""<div class="evidence-section" style="background:{bg};border-left:4px solid {border};">
<div class="evidence-title">{icon} 외부 팩트체크 보조 신호</div>
<div class="evidence-row"><b>{label}</b> — 유사도 <b>{sim_pct:.0f}%</b></div>
<div class="evidence-row" style="font-size:12px;color:#4B5563;">참고 케이스: <a href="{mu}" target="_blank" style="color:#1D4ED8;text-decoration:underline;">{mt}</a></div>
<div class="evidence-row" style="font-size:11px;color:#6B7280;">※ 점수에 합산되지 않는 별도 신호입니다.</div>
</div>"""

        # ── 처리한 Worker 정보 표시용 ──
        # worker_id 컬럼에서 해당 기사를 분석한 Worker 번호를 가져온다
        _card_wid = str(row.get('worker_id', '') or '')
        _card_worker_label = f"Worker-{_card_wid}" if _card_wid and _card_wid != 'nan' else ""
        # Worker 라벨이 있으면 카드 메타 라인에 표시
        _card_worker_html = f'&nbsp;&nbsp;|&nbsp;&nbsp;<span style="background:#EFF6FF;color:#1E40AF;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600;">{_card_worker_label} 처리</span>' if _card_worker_label else ""

        # [P0-4 XSS 방어] HTML 삽입 전 외부 데이터 이스케이프
        _safe_title = safe(row.get('title', ''))
        _safe_url = safe(row.get('url', ''))
        _safe_analyzed_at = safe(row.get('analyzed_at', ''))
        _safe_grade = safe(grade)

        # ── 기사 카드 HTML 조립 — 왼쪽 정렬 필수 (Markdown 코드 블록 방지) ──
        card_html = f"""<div class="article-card">
<div class="grade-bar" style="background: {color};"></div>
<div class="card-body">
<div class="card-title">{_safe_title}</div>
<div class="card-meta">{_safe_url}&nbsp;&nbsp;|&nbsp;&nbsp;{_safe_analyzed_at}&nbsp;&nbsp;|&nbsp;&nbsp;<span class="grade-badge" style="background: {color};">{_safe_grade}</span>{_card_worker_html}</div>
<div style="display: flex; gap: 32px; align-items: center; margin-top: 12px;">
<div style="flex-shrink: 0; min-width: 130px;">
{build_gauge_svg(score, grade, color)}</div>
<div style="flex: 1;">
<div class="indicator-row">
<span class="indicator-label">본문 일치도 (45%)</span>
<div class="indicator-track"><div class="indicator-fill" style="width: {content_s}%; background: {get_indicator_color(content_s)};"></div></div>
<span class="indicator-value">{content_s:.1f}</span>
</div>
<div class="indicator-row">
<span class="indicator-label">자극성 분석 (35%)</span>
<div class="indicator-track"><div class="indicator-fill" style="width: {provocative_s}%; background: {get_indicator_color(provocative_s)};"></div></div>
<span class="indicator-value">{provocative_s:.1f}</span>
</div>
<div class="indicator-row">
<span class="indicator-label">출처 신뢰도 (20%)</span>
<div class="indicator-track"><div class="indicator-fill" style="width: {source_s}%; background: {get_indicator_color(source_s)};"></div></div>
<span class="indicator-value">{source_s:.1f}</span>
</div>
</div></div>
{evidence1}
{evidence2}
{evidence3}
{fact_html}
<p style="margin: 16px 0 0 0; font-size: 13px; color: #64748B; line-height: 1.6;">{body_preview}</p>
</div></div>"""

        st.markdown(card_html, unsafe_allow_html=True)

        # ─────────── M1 피드백 UI (회원만, 본인 결과만) ───────────
        if auth_user_info():
            result_id = int(row['id'])
            with st.expander(f"💬 이 분석에 피드백 남기기 (#{result_id})", expanded=False):
                # 기존 피드백 불러오기 (재방문 시)
                existing_fb = {}
                try:
                    _r = requests.get(
                        f"{API_URL}/results/{result_id}/feedback",
                        headers=auth_headers(), timeout=5
                    )
                    if _r.status_code == 200:
                        existing_fb = _r.json() or {}
                except Exception:
                    pass

                col_r, col_c = st.columns([1, 2])
                with col_r:
                    rating = st.radio(
                        "정확도 평가 (이 분석에 동의?)",
                        options=[5, 4, 3, 2, 1],
                        format_func=lambda x: {
                            5: "⭐⭐⭐⭐⭐ 완벽 일치",
                            4: "⭐⭐⭐⭐ 대체로 일치",
                            3: "⭐⭐⭐ 보통",
                            2: "⭐⭐ 어긋남",
                            1: "⭐ 완전 어긋남",
                        }[x],
                        index=([5, 4, 3, 2, 1].index(existing_fb["rating"])
                               if existing_fb.get("rating") in [5, 4, 3, 2, 1]
                               else 2),
                        key=f"fb_rating_{result_id}",
                    )
                with col_c:
                    category_options = {
                        "": "── 카테고리 선택 (선택) ──",
                        "accurate": "✅ 분석 정확함",
                        "false_positive": "🟡 정상인데 의심으로 분류 (False Positive)",
                        "false_negative": "🔴 자극적인데 신뢰로 분류 (False Negative)",
                        "keyword_miss": "📝 키워드가 본문에 있는데 X로 표시됨",
                        "source_unknown": "📌 큰 매체인데 출처 불명(35점)",
                        "provocative_miss": "⚡ 자극적 단어가 사전에 누락됨",
                        "ux_issue": "🖥 UX/UI 문제",
                        "other": "❓ 기타",
                    }
                    cat_keys = list(category_options.keys())
                    category = st.selectbox(
                        "오류 카테고리 (있으면 선택)",
                        options=cat_keys,
                        format_func=lambda k: category_options[k],
                        index=(cat_keys.index(existing_fb["category"])
                               if existing_fb.get("category") in cat_keys
                               else 0),
                        key=f"fb_cat_{result_id}",
                    )
                    comment = st.text_area(
                        "추가 코멘트 (예: '이 매체는 큰 매체인데 출처 불명으로 나옴')",
                        value=existing_fb.get("comment", ""),
                        key=f"fb_cmt_{result_id}",
                        height=70,
                    )

                col_submit, col_status = st.columns([1, 2])
                with col_submit:
                    if st.button("📤 피드백 제출", key=f"fb_submit_{result_id}"):
                        try:
                            _r = requests.post(
                                f"{API_URL}/results/{result_id}/feedback",
                                json={
                                    "rating": rating,
                                    "category": category,
                                    "comment": comment,
                                },
                                headers=auth_headers(),
                                timeout=5,
                            )
                            if _r.status_code == 200:
                                st.success("✅ 피드백 제출 완료. 감사합니다!")
                            else:
                                st.error(f"❌ {_r.json().get('error', '제출 실패')}")
                        except Exception as _e:
                            st.error(f"❌ API 실패: {_e}")
                with col_status:
                    if existing_fb.get("created_at"):
                        st.caption(f"이전 제출: {existing_fb['created_at']} — 수정 후 다시 제출 가능")

# ================================================================
# 분석 실패 기사 — 별도 섹션
# ================================================================
if not failed_df.empty:
    st.markdown(f'<div class="section-title">❌ 분석 실패 ({len(failed_df)}건)</div>', unsafe_allow_html=True)
    st.markdown("""<div style="background: #FEF2F2; border: 1px solid #FECACA; border-radius: 10px; padding: 12px 18px; margin-bottom: 16px; font-size: 13px; color: #991B1B;">
실패한 기사는 아래 <b>재시도</b> 버튼으로 다시 분석을 요청할 수 있습니다. (failed 상태는 중복으로 간주하지 않습니다.)
</div>""", unsafe_allow_html=True)
    for idx, row in failed_df.iterrows():
        col_card, col_btn = st.columns([6, 1])
        with col_card:
            # [P0-4 XSS] 실패 카드도 외부 데이터 이스케이프
            st.markdown(f"""<div class="article-card failed">
<div class="grade-bar" style="background: #9CA3AF;"></div>
<div class="card-body">
<div class="card-title" style="color: #94A3B8;">❌ {safe(row.get('title',''))}</div>
<div class="card-meta">{safe(row.get('url',''))}&nbsp;&nbsp;|&nbsp;&nbsp;{safe(row.get('analyzed_at',''))}&nbsp;&nbsp;|&nbsp;&nbsp;<span class="grade-badge" style="background: #9CA3AF;">분석 실패</span></div>
<p style="color: #94A3B8; font-size: 14px; margin: 0;">크롤링에 실패하여 분석할 수 없습니다. URL을 확인해주세요.</p>
</div></div>""", unsafe_allow_html=True)
        with col_btn:
            if st.button("🔄 재시도", key=f"retry_{idx}"):
                try:
                    resp = requests.post(
                        f"{API_URL}/analyze",
                        json={"url": row['url'], "user_label": USER_LABEL},
                        headers=auth_headers(),
                        timeout=5
                    )
                    if resp.status_code == 200:
                        st.success("재시도 요청 완료!")
                    else:
                        st.error("재시도 실패")
                except Exception as e:
                    st.error(f"API 연결 실패: {e}")

# ================================================================
# 페이지 하단 푸터
# ================================================================
st.markdown("""<div style="text-align: center; padding: 32px 0 16px 0; color: #94A3B8; font-size: 13px;">
SSAK3 — 규칙 기반 분석 + AI 보조지표 결합 뉴스 신뢰도 점수화 시스템
</div>""", unsafe_allow_html=True)
