FROM python:3.11-slim

WORKDIR /app

# requests, beautifulsoup4: 네이버 뉴스 본문 직접 파싱용
# transformers, torch: HuggingFace KR-FinBert-SC 감성분석 모델 실행용
RUN pip install pika newspaper3k lxml_html_clean scikit-learn numpy requests beautifulsoup4 transformers torch

# ── 모델 사전 다운로드 (시연 안정성 + 기동 시간 단축) ──
# 컨테이너 첫 기동 시 KR-FinBert-SC(~205MB) + NLI(~110MB)을 다운로드하면
# 학회 시연 환경 네트워크 사고 시 Worker가 안 켜지는 위험이 있다.
# 이미지 빌드 시 미리 다운로드해서 이미지에 포함 → 기동 시간 ~60초 → ~5초.
# HuggingFace 캐시 위치: /root/.cache/huggingface
RUN python -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; \
    AutoTokenizer.from_pretrained('snunlp/KR-FinBert-SC'); \
    AutoModelForSequenceClassification.from_pretrained('snunlp/KR-FinBert-SC'); \
    AutoTokenizer.from_pretrained('MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli'); \
    AutoModelForSequenceClassification.from_pretrained('MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli'); \
    print('모델 사전 다운로드 완료')"

COPY worker.py .

CMD ["python", "-u", "worker.py"]
