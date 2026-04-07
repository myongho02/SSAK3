import streamlit as st
import sqlite3
import pandas as pd
import requests
import os
import json

# ========== 기본 설정 ==========
DB_PATH = "/app/data/results.db"
# Flask API 서버 주소 (docker-compose 내부 네트워크에서 서비스명으로 접근)
API_URL = "http://api:5000"

st.set_page_config(page_title="뉴스 신뢰도 분석", page_icon="📰", layout="wide")

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
.score-circle {
    width: 100px; height: 100px; border-radius: 50%;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    color: #fff; font-weight: 700; margin: 0 auto;
    box-shadow: 0 4px 14px rgba(0,0,0,0.15);
}
.score-circle .number { font-size: 28px; line-height: 1.1; }
.score-circle .unit { font-size: 12px; font-weight: 400; opacity: 0.9; }
.indicator-row { display: flex; align-items: center; margin: 8px 0; }
.indicator-label { width: 150px; font-size: 13px; font-weight: 500; color: #475569; flex-shrink: 0; }
.indicator-track { flex: 1; background: #F1F5F9; border-radius: 6px; height: 12px; overflow: hidden; }
.indicator-fill { height: 100%; border-radius: 6px; transition: width 0.4s ease; }
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
<p style="color: #94A3B8; margin: 8px 0 0 0; font-size: 16px; font-weight: 300;">AI 기반 분산 처리 시스템 &nbsp;|&nbsp; 팀 싹쓰리</p>
</div>""", unsafe_allow_html=True)

# ================================================================
# 사이드바 — 등급 필터 + 시스템 상태
# ================================================================

# ── 사이드바 로고/타이틀 ──
st.sidebar.markdown("""<div style="text-align:center; padding: 16px 0 8px 0;">
<span style="font-size: 36px;">📰</span>
<h3 style="margin: 4px 0 0 0; font-weight: 700; letter-spacing: -0.3px;">SSAK3</h3>
<p style="font-size: 12px; opacity: 0.6; margin: 0;">News Credibility Analyzer</p>
</div>""", unsafe_allow_html=True)
st.sidebar.markdown("---")

# ── 등급 필터 (라디오 버튼) ──
st.sidebar.markdown("**🔍 등급 필터**")
grade_filter = st.sidebar.radio(
    "표시할 등급 선택",
    ["전체", "신뢰 가능", "주의 필요", "의심 기사", "신뢰 낮음", "분석 실패"],
    label_visibility="collapsed"
)

st.sidebar.markdown("---")

# ================================================================
# DB에서 분석 결과 로드
# ================================================================
def load_data():
    """SQLite DB에서 분석 결과를 DataFrame으로 읽어오기"""
    try:
        if not os.path.exists(DB_PATH):
            return pd.DataFrame()
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(
            "SELECT * FROM analysis_results ORDER BY analyzed_at DESC",
            conn
        )
        conn.close()
        return df
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
❌ 실패: <b>{failed_count}건</b><br>
🤖 Worker: <b>1대</b>
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
    """개별 지표 점수에 따른 프로그레스 바 색상"""
    if score >= 70:
        return "#10B981"
    elif score >= 40:
        return "#F59E0B"
    else:
        return "#EF4444"

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
                        json={"url": url_input},
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
# 사이드바 필터 적용
# ================================================================
if grade_filter == "분석 실패":
    filtered_df = df[df['status'] == 'failed']
elif grade_filter != "전체":
    filtered_df = df[(df['grade'] == grade_filter) & (df['status'] == 'done')]
else:
    filtered_df = df

# ================================================================
# 상단 요약 카드 4개 — 아이콘 + 숫자 + 라벨
# done 상태인 기사만 집계
# ================================================================
done_df = df[df['status'] == 'done']

avg_score = done_df['total_score'].mean() if not done_df.empty else 0
reliable_count = len(done_df[done_df['total_score'] >= 80])
low_count = len(done_df[done_df['total_score'] < 40])

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
# 기사별 상세 결과 — 카드 형태 + 원형 게이지 + 프로그레스 바
# ================================================================
st.markdown('<div class="section-title">📋 기사별 분석 결과</div>', unsafe_allow_html=True)

if filtered_df.empty:
    st.markdown("""<div style="text-align: center; padding: 40px; color: #94A3B8;">
<p style="font-size: 16px;">해당 등급의 기사가 없습니다.</p>
</div>""", unsafe_allow_html=True)
else:
    for _, row in filtered_df.iterrows():
        score = row['total_score']
        grade = row['grade']
        status = row.get('status', 'done')
        color = get_grade_color(grade)
        is_failed = (status == 'failed')

        # ── 실패한 기사: 회색 카드 ──
        if is_failed:
            st.markdown(f"""<div class="article-card failed">
<div class="grade-bar" style="background: #9CA3AF;"></div>
<div class="card-body">
<div class="card-title" style="color: #94A3B8;">❌ {row['title']}</div>
<div class="card-meta">{row['url']}&nbsp;&nbsp;|&nbsp;&nbsp;{row['analyzed_at']}&nbsp;&nbsp;|&nbsp;&nbsp;<span class="grade-badge" style="background: #9CA3AF;">분석 실패</span></div>
<p style="color: #94A3B8; font-size: 14px; margin: 0;">크롤링에 실패하여 분석할 수 없습니다. URL을 확인해주세요.</p>
</div></div>""", unsafe_allow_html=True)
            continue

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

        # ── 섹션 1: 본문 일치도 근거 HTML 생성 ──
        all_kw = kw_data.get('keywords', [])
        matched_kw = kw_data.get('matched', [])
        cosine_raw = kw_data.get('cosine_raw', 0)
        kw_tags = ""
        for kw in all_kw:
            if kw in matched_kw:
                kw_tags += f'<span class="kw-tag matched">{kw} ✓</span>'
            else:
                kw_tags += f'<span class="kw-tag missed">{kw} ✗</span>'

        evidence1 = ""
        if all_kw:
            match_pct = round(len(matched_kw) / len(all_kw) * 100, 1) if all_kw else 0
            evidence1 = f"""<div class="evidence-section">
<div class="evidence-title">📝 본문 일치도 분석 근거</div>
<div class="evidence-row">제목 키워드: {kw_tags}</div>
<div class="evidence-row">본문에서 발견된 키워드: <b>{len(matched_kw)}개</b> / 전체 {len(all_kw)}개 (매칭률 <b>{match_pct}%</b>)</div>
<div class="evidence-row">제목-본문 코사인 유사도: <b>{cosine_raw}</b></div>
<div class="evidence-row">최종 본문 일치도: <b>{content_s:.1f}점</b></div>
</div>"""

        # ── 섹션 2: 자극성 분석 근거 HTML 생성 ──
        detected = prov_data.get('detected', {})
        prov_ratio = prov_data.get('ratio', 0)
        prov_tags = ""
        for cat, words in detected.items():
            for w in words:
                prov_tags += f'<span class="cat-label">{cat}</span><span class="prov-tag">{w}</span> '

        ai_neutral = ai_data.get('neutral', 0)
        ai_positive = ai_data.get('positive', 0)
        ai_negative = ai_data.get('negative', 0)

        evidence2 = f"""<div class="evidence-section">
<div class="evidence-title">⚡ 자극성 분석 근거</div>
<div class="evidence-row">감지된 자극적 표현: {prov_tags if prov_tags else '<span style="color:#94A3B8;">없음</span>'}</div>
<div class="evidence-row">자극적 표현 비율: <b>{prov_ratio}%</b> (본문 대비)</div>
<div class="evidence-row">AI 감성분석 결과: 중립 <b>{ai_neutral}%</b> / 부정 <b>{ai_negative}%</b> / 긍정 <b>{ai_positive}%</b></div>
<div class="evidence-row">최종 자극성 점수: <b>{provocative_s:.1f}점</b></div>
</div>"""

        # ── 섹션 3: 출처 신뢰도 근거 HTML 생성 ──
        src_parts = source_info.split('|') if source_info else ['', '']
        src_name = src_parts[0] if len(src_parts) > 0 else ''
        src_class = src_parts[1] if len(src_parts) > 1 else ''

        evidence3 = f"""<div class="evidence-section">
<div class="evidence-title">🏢 출처 신뢰도 근거</div>
<div class="evidence-row">원본 언론사: <b>{src_name if src_name else '확인 불가'}</b></div>
<div class="evidence-row">분류: <b>{src_class if src_class else '미분류'}</b></div>
<div class="evidence-row">최종 출처 점수: <b>{source_s:.1f}점</b></div>
</div>"""

        # ── 기사 카드 HTML 조립 — 왼쪽 정렬 필수 (Markdown 코드 블록 방지) ──
        card_html = f"""<div class="article-card">
<div class="grade-bar" style="background: {color};"></div>
<div class="card-body">
<div class="card-title">{row['title']}</div>
<div class="card-meta">{row['url']}&nbsp;&nbsp;|&nbsp;&nbsp;{row['analyzed_at']}&nbsp;&nbsp;|&nbsp;&nbsp;<span class="grade-badge" style="background: {color};">{grade}</span></div>
<div style="display: flex; gap: 32px; align-items: center; margin-top: 12px;">
<div style="flex-shrink: 0;">
<div class="score-circle" style="background: {color};">
<span class="number">{score:.0f}</span>
<span class="unit">/ 100</span>
</div></div>
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
<p style="margin: 16px 0 0 0; font-size: 13px; color: #64748B; line-height: 1.6;">{body_preview}</p>
</div></div>"""

        st.markdown(card_html, unsafe_allow_html=True)

# ================================================================
# 페이지 하단 푸터
# ================================================================
st.markdown("""<div style="text-align: center; padding: 32px 0 16px 0; color: #94A3B8; font-size: 13px;">
SSAK3 — AI 기반 뉴스 신뢰도 분석 시스템 &nbsp;|&nbsp; Streamlit + Flask + RabbitMQ + KR-FinBert
</div>""", unsafe_allow_html=True)
