FROM python:3.11-slim

WORKDIR /app

# ── 이미지 크기 최적화 (운영 배포용) ──
# 기본 `pip install torch`는 CUDA 라이브러리 포함 ~2.5GB.
# Worker는 GPU 추론 안 함(CPU만) → CPU 전용 PyTorch 휠로 교체 → ~200MB.
# --no-cache-dir로 pip 캐시 제외 → layer 추가 절감.
# 결과: 이미지 ~14GB → ~1.5GB (팀원 다운로드 시간 30분 → 3분)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# requests, beautifulsoup4: 네이버 뉴스 본문 직접 파싱용
# transformers: HuggingFace KR-FinBert-SC 감성분석 모델 실행용
RUN pip install --no-cache-dir pika newspaper3k lxml_html_clean scikit-learn numpy requests beautifulsoup4 transformers

# ── 모델 사전 다운로드 (옵션) ──
# 로컬 개발: 시연 안정성 위해 빌드 시 미리 다운로드 (~315MB, 빌드 시간 +2~3분)
# CI/Release: false로 빌드 → 이미지 작아짐(~1.5GB), 팀원 첫 가동 시 컨테이너 내부에서 자동 다운
# 빌드 명령:
#   로컬: docker compose build (PREDOWNLOAD_MODELS 미지정 → 기본 true)
#   GitHub Actions Release: --build-arg PREDOWNLOAD_MODELS=false
ARG PREDOWNLOAD_MODELS=true
RUN if [ "$PREDOWNLOAD_MODELS" = "true" ]; then \
      python -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; \
        AutoTokenizer.from_pretrained('snunlp/KR-FinBert-SC'); \
        AutoModelForSequenceClassification.from_pretrained('snunlp/KR-FinBert-SC'); \
        AutoTokenizer.from_pretrained('MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli'); \
        AutoModelForSequenceClassification.from_pretrained('MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli'); \
        print('모델 사전 다운로드 완료')"; \
    else \
      echo "모델 사전 다운로드 생략 — 첫 컨테이너 기동 시 자동 다운로드됨"; \
    fi

COPY worker.py .
COPY factcheck_db.json /app/factcheck_db.json

CMD ["python", "-u", "worker.py"]
