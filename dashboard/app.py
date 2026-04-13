import streamlit as st
import sqlite3
import pandas as pd
import requests
import os
import json
import time

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
st.sidebar.markdown("---")

# ── 페이지 네비게이션 ──
page_mode = st.sidebar.radio(
    "페이지 선택",
    ["📊 분석 대시보드", "🏎️ 성능 측정"],
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
❌ 실패: <b>{failed_count}건</b>
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
                        json={"urls": valid_urls},
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

# DB에서 jobs 기반 진행률 조회 (jobs 테이블이 없으면 analysis_results로 폴백)
try:
    if os.path.exists(DB_PATH):
        _conn = sqlite3.connect(DB_PATH, timeout=5)
        _cur = _conn.cursor()
        # jobs 테이블 기반 진행률
        try:
            _cur.execute("SELECT COUNT(*) FROM jobs")
            _total = _cur.fetchone()[0]
            _cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'done'")
            _done = _cur.fetchone()[0]
            _cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'failed'")
            _fail = _cur.fetchone()[0]
            _cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
            _pending = _cur.fetchone()[0]
            _cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'processing'")
            _processing = _cur.fetchone()[0]
        except Exception:
            # jobs 테이블이 아직 없으면 analysis_results로 폴백
            _cur.execute("SELECT COUNT(*) FROM analysis_results")
            _total = _cur.fetchone()[0]
            _cur.execute("SELECT COUNT(*) FROM analysis_results WHERE status = 'done'")
            _done = _cur.fetchone()[0]
            _cur.execute("SELECT COUNT(*) FROM analysis_results WHERE status = 'failed'")
            _fail = _cur.fetchone()[0]
            _pending = 0
            _processing = 0
        _conn.close()

        # 진행 상황 바 (완료+실패 / 전체)
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
done_df_all = df[df['status'] == 'done']

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
done_df = done_df_all

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

        # ── 섹션 1: 본문 일치도 근거 HTML 생성 ──
        all_kw = kw_data.get('keywords', [])
        matched_kw = kw_data.get('matched', [])
        negated_kw = kw_data.get('negated', [])  # 부정어로 매칭 취소된 키워드
        cosine_raw = kw_data.get('cosine_raw', 0)
        kw_tags = ""
        for kw in all_kw:
            if kw in matched_kw:
                kw_tags += f'<span class="kw-tag matched">{kw} ✓</span>'
            elif kw in negated_kw:
                # 부정어로 매칭 취소된 키워드는 노란색으로 표시
                kw_tags += f'<span class="kw-tag" style="background:#FEF3C7;color:#92400E;">{kw} ⚠부정</span>'
            else:
                kw_tags += f'<span class="kw-tag missed">{kw} ✗</span>'

        evidence1 = ""
        if all_kw:
            match_pct = round(len(matched_kw) / len(all_kw) * 100, 1) if all_kw else 0
            negated_info = f" (부정어 근접으로 {len(negated_kw)}개 취소)" if negated_kw else ""
            evidence1 = f"""<div class="evidence-section">
<div class="evidence-title">📝 본문 일치도 분석 근거</div>
<div class="evidence-row">제목 키워드: {kw_tags}</div>
<div class="evidence-row">본문에서 발견된 키워드: <b>{len(matched_kw)}개</b> / 전체 {len(all_kw)}개 (매칭률 <b>{match_pct}%</b>){negated_info}</div>
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
<div class="evidence-row">논조 분석 보조지표 (KR-FinBert): 중립 <b>{ai_neutral}%</b> / 부정 <b>{ai_negative}%</b> / 긍정 <b>{ai_positive}%</b></div>
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

        # ── 처리한 Worker 정보 표시용 ──
        # worker_id 컬럼에서 해당 기사를 분석한 Worker 번호를 가져온다
        _card_wid = str(row.get('worker_id', '') or '')
        _card_worker_label = f"Worker-{_card_wid}" if _card_wid and _card_wid != 'nan' else ""
        # Worker 라벨이 있으면 카드 메타 라인에 표시
        _card_worker_html = f'&nbsp;&nbsp;|&nbsp;&nbsp;<span style="background:#EFF6FF;color:#1E40AF;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600;">{_card_worker_label} 처리</span>' if _card_worker_label else ""

        # ── 기사 카드 HTML 조립 — 왼쪽 정렬 필수 (Markdown 코드 블록 방지) ──
        card_html = f"""<div class="article-card">
<div class="grade-bar" style="background: {color};"></div>
<div class="card-body">
<div class="card-title">{row['title']}</div>
<div class="card-meta">{row['url']}&nbsp;&nbsp;|&nbsp;&nbsp;{row['analyzed_at']}&nbsp;&nbsp;|&nbsp;&nbsp;<span class="grade-badge" style="background: {color};">{grade}</span>{_card_worker_html}</div>
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
<p style="margin: 16px 0 0 0; font-size: 13px; color: #64748B; line-height: 1.6;">{body_preview}</p>
</div></div>"""

        st.markdown(card_html, unsafe_allow_html=True)

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
            st.markdown(f"""<div class="article-card failed">
<div class="grade-bar" style="background: #9CA3AF;"></div>
<div class="card-body">
<div class="card-title" style="color: #94A3B8;">❌ {row['title']}</div>
<div class="card-meta">{row['url']}&nbsp;&nbsp;|&nbsp;&nbsp;{row['analyzed_at']}&nbsp;&nbsp;|&nbsp;&nbsp;<span class="grade-badge" style="background: #9CA3AF;">분석 실패</span></div>
<p style="color: #94A3B8; font-size: 14px; margin: 0;">크롤링에 실패하여 분석할 수 없습니다. URL을 확인해주세요.</p>
</div></div>""", unsafe_allow_html=True)
        with col_btn:
            if st.button("🔄 재시도", key=f"retry_{idx}"):
                try:
                    resp = requests.post(
                        f"{API_URL}/analyze",
                        json={"url": row['url']},
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
