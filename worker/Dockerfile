FROM python:3.11-slim

WORKDIR /app

# requests, beautifulsoup4: 네이버 뉴스 본문 직접 파싱용
# transformers, torch: HuggingFace KR-FinBert-SC 감성분석 모델 실행용
RUN pip install pika newspaper3k lxml_html_clean scikit-learn numpy requests beautifulsoup4 transformers torch

COPY worker.py .

CMD ["python", "-u", "worker.py"]
