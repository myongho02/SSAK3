FROM python:3.11-slim

WORKDIR /app

# requests: 대시보드에서 Flask API로 분석 요청을 보내기 위해 필요
RUN pip install streamlit pandas requests

COPY app.py .

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
