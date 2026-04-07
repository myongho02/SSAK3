import streamlit as st
import sqlite3
import pandas as pd
import requests
import os

# ========== 기본 설정 ==========
DB_PATH = "/app/data/results.db"
# Flask API 서버 주소 (docker-compose 내부 네트워크에서 서비스명으로 접근)
API_URL = "http://api:5000"

st.set_page_config(page_title="뉴스 신뢰도 분석", page_icon="📰", layout="wide")

st.title("📰 뉴스 신뢰도 분석 대시보드")
st.markdown("AI 기반 분산 처리 시스템으로 분석된 뉴스 신뢰도 결과")

# ========== 뉴스 URL 입력창 ==========
# 사용자가 웹에서 직접 뉴스 URL을 입력하여 분석 요청을 보낼 수 있는 영역
st.subheader("🔗 뉴스 기사 분석 요청")
with st.form("analyze_form"):
    # URL 입력 필드
    url_input = st.text_input(
        "분석할 뉴스 URL을 입력하세요",
        placeholder="https://n.news.naver.com/mnews/article/..."
    )
    submitted = st.form_submit_button("분석 요청")

    if submitted:
        if not url_input or not url_input.startswith("http"):
            st.error("올바른 URL을 입력해주세요. (http:// 또는 https://로 시작)")
        else:
            # Flask API의 /analyze 엔드포인트에 POST 요청
            try:
                resp = requests.post(
                    f"{API_URL}/analyze",
                    json={"url": url_input},
                    timeout=5
                )
                if resp.status_code == 200:
                    st.success(f"분석 요청 완료! Worker가 처리 중입니다. 잠시 후 새로고침 해주세요.")
                else:
                    st.error(f"요청 실패: {resp.text}")
            except Exception as e:
                st.error(f"API 서버 연결 실패: {e}")

st.markdown("---")

# ========== DB에서 분석 결과 읽기 ==========
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
    except:
        return pd.DataFrame()

# ========== 등급별 색상 매핑 함수 ==========
def get_grade_color(score):
    """종합 점수에 따라 등급별 색상 반환
    - 80점 이상: 초록 (신뢰 가능)
    - 60~79점: 주황 (주의 필요)
    - 40~59점: 빨강 계열 (의심 기사)
    - 40점 미만: 진한 빨강 (신뢰 낮음)
    """
    if score >= 80:
        return "#2ecc71"   # 초록
    elif score >= 60:
        return "#f39c12"   # 주황
    elif score >= 40:
        return "#e74c3c"   # 빨강
    else:
        return "#8b0000"   # 진한 빨강

def get_grade_emoji(score):
    """종합 점수에 따라 등급 이모지 반환"""
    if score >= 80:
        return "🟢"
    elif score >= 60:
        return "🟡"
    elif score >= 40:
        return "🟠"
    else:
        return "🔴"

# 새로고침 버튼
if st.button("🔄 결과 새로고침"):
    st.rerun()

df = load_data()

if df.empty:
    st.info("아직 분석된 기사가 없습니다. 위 입력창에서 뉴스 URL을 입력하거나, Worker가 분석을 완료하면 결과가 표시됩니다.")
else:
    # ========== 상단 요약 카드 ==========
    # 전체 분석 현황을 한눈에 볼 수 있는 지표 카드 4개
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("분석된 기사 수", f"{len(df)}건")
    with col2:
        avg_score = df['total_score'].mean()
        st.metric("평균 신뢰도", f"{avg_score:.1f}점")
    with col3:
        reliable = len(df[df['total_score'] >= 80])
        st.metric("신뢰 가능", f"{reliable}건")
    with col4:
        suspicious = len(df[df['total_score'] < 40])
        st.metric("신뢰 낮음", f"{suspicious}건")

    st.markdown("---")

    # ========== 등급별 분포 현황 ==========
    st.subheader("📊 등급별 분포")
    # 등급별 기사 수를 가로 막대 그래프로 표시
    grade_order = ["신뢰 가능", "주의 필요", "의심 기사", "신뢰 낮음"]
    # 등급별 건수 집계 (없는 등급도 0으로 표시)
    grade_counts = df['grade'].value_counts()
    grade_data = pd.DataFrame({
        "등급": grade_order,
        "건수": [grade_counts.get(g, 0) for g in grade_order]
    })

    # Streamlit 기본 bar_chart로 등급별 분포 표시
    st.bar_chart(grade_data.set_index("등급"))

    st.markdown("---")

    # ========== 기사별 상세 결과 ==========
    st.subheader("📋 기사별 분석 결과")

    for _, row in df.iterrows():
        score = row['total_score']
        grade = row['grade']
        emoji = get_grade_emoji(score)
        color = get_grade_color(score)

        # 등급 색상이 적용된 제목 표시
        with st.expander(f"{emoji} [{grade}] {row['title']} — {score}점"):
            # 기본 정보
            st.write(f"**URL:** {row['url']}")
            st.write(f"**분석 일시:** {row['analyzed_at']}")

            # 종합 점수를 색상 바로 시각화 (HTML 렌더링)
            st.markdown(
                f"""
                <div style="background: linear-gradient(to right, {color} {score}%, #eeeeee {score}%);
                     padding: 8px 16px; border-radius: 8px; margin: 8px 0;">
                    <strong style="color: white; text-shadow: 1px 1px 2px rgba(0,0,0,0.5);">
                        종합 {score}점 — {grade}
                    </strong>
                </div>
                """,
                unsafe_allow_html=True
            )

            st.write("")  # 여백

            # ── 3가지 지표 막대 그래프 ──
            # 각 지표 점수를 가로 막대로 비교할 수 있도록 시각화
            indicator_data = pd.DataFrame({
                "지표": ["본문 일치도 (45%)", "자극성 분석 (35%)", "출처 신뢰도 (20%)"],
                "점수": [
                    row['content_score'],
                    row['provocative_score'],
                    row['source_score']
                ]
            })

            # 각 지표를 색상 바로 개별 렌더링 (지표별 직관적 비교)
            for _, ind in indicator_data.iterrows():
                ind_score = ind['점수']
                # 점수에 따라 바 색상 결정
                if ind_score >= 70:
                    bar_color = "#2ecc71"
                elif ind_score >= 40:
                    bar_color = "#f39c12"
                else:
                    bar_color = "#e74c3c"

                st.markdown(
                    f"""
                    <div style="margin: 4px 0;">
                        <span style="display: inline-block; width: 160px; font-size: 14px;">
                            {ind['지표']}
                        </span>
                        <span style="display: inline-block; width: 50px; font-weight: bold; font-size: 14px;">
                            {ind_score:.1f}
                        </span>
                        <div style="display: inline-block; width: 60%; background: #eeeeee;
                             border-radius: 4px; height: 18px; vertical-align: middle;">
                            <div style="width: {ind_score}%; background: {bar_color};
                                 height: 100%; border-radius: 4px;"></div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            st.write("")  # 여백

            # 본문 미리보기 (최대 300자)
            body_preview = row['body'][:300] if len(row['body']) > 300 else row['body']
            st.markdown(f"**본문 미리보기:** {body_preview}...")
