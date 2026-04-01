import streamlit as st
import sqlite3
import pandas as pd
import os

DB_PATH = "/app/data/results.db"

st.set_page_config(page_title="뉴스 신뢰도 분석", page_icon="📰", layout="wide")

st.title("📰 뉴스 신뢰도 분석 대시보드")
st.markdown("AI 기반 분산 처리 시스템으로 분석된 뉴스 신뢰도 결과")

# DB에서 데이터 읽기
def load_data():
    try:
        if not os.path.exists(DB_PATH):
            return pd.DataFrame()
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT * FROM analysis_results ORDER BY analyzed_at DESC", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame()

# 새로고침 버튼
if st.button("🔄 결과 새로고침"):
    st.rerun()

df = load_data()

if df.empty:
    st.info("아직 분석된 기사가 없습니다. Worker가 분석을 완료하면 여기에 결과가 표시됩니다.")
else:
    # 상단 요약 카드
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
        st.metric("신뢰 어려움", f"{suspicious}건")
    
    st.markdown("---")
    
    # 신뢰도 점수 분포 차트
    st.subheader("📊 신뢰도 점수 분포")
    st.bar_chart(df['total_score'])
    
    st.markdown("---")
    
    # 기사별 상세 결과
    st.subheader("📋 기사별 분석 결과")
    for _, row in df.iterrows():
        score = row['total_score']
        grade = row['grade']
        
        if score >= 80:
            color = "🟢"
        elif score >= 60:
            color = "🟡"
        elif score >= 40:
            color = "🟠"
        else:
            color = "🔴"
        
        with st.expander(f"{color} [{grade}] {row['title']} — {score}점"):
            st.write(f"**URL:** {row['url']}")
            st.write(f"**분석 일시:** {row['analyzed_at']}")
            
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("감성분석", f"{row['sentiment_score']:.1f}")
            with col2:
                st.metric("제목-본문 유사도", f"{row['tfidf_score']:.1f}")
            with col3:
                st.metric("출처 신뢰도", f"{row['source_score']:.1f}")
            
            st.write(f"**본문 미리보기:** {row['body'][:200]}...")
            