# SSAK3 - 뉴스 신뢰도 점수화 시스템

뉴스 기사의 URL을 입력하면, **규칙 기반 분석과 AI 보조지표를 결합**하여 뉴스 신뢰도를 점수화하는 분산 처리 시스템입니다.

> **주의:** 이 시스템은 뉴스의 진위(참/거짓)를 판별하는 것이 아닙니다.  
> 기사의 형식적 신뢰도(제목-본문 일치도, 자극적 표현 정도, 출처 공신력)를 정량화하여 점수로 제공합니다.

---

## 1. 신뢰도 분석 기준

종합 신뢰도 점수는 **3가지 지표의 가중 평균**으로 계산됩니다.

```
종합 점수 = 본문 일치도 × 0.45 + 자극성 점수 × 0.35 + 출처 점수 × 0.20
```

### 본문 일치도 (45%)

| 항목 | 내용 |
|------|------|
| **무엇을** | 기사 제목과 본문 내용이 얼마나 일치하는지 측정 |
| **왜** | 낚시성 기사는 자극적인 제목을 달지만, 본문에는 관련 내용이 없는 경우가 많음 |
| **어떻게** | 1) **코사인 유사도** — 제목과 본문 첫 3문장의 TF-IDF 유사도 계산. 2) **키워드 매칭** — 제목 핵심 명사가 본문에 등장하는 비율. 두 점수를 50:50으로 결합 |

### 자극성 분석 (35%)

| 항목 | 내용 |
|------|------|
| **무엇을** | 자극적이거나 선정적인 표현이 얼마나 사용되었는지 측정 |
| **왜** | 신뢰할 수 있는 기사는 객관적이고 중립적인 표현을 사용 |
| **어떻게** | 1) **자극적 단어 감지 (규칙 기반)** — 4가지 카테고리(과장, 혐오, 선정, 공포)의 단어 사전으로 감지. 제목 출현 시 가중치 2배. 2) **감성분석 보조지표 (AI)** — KR-FinBert-SC 모델이 기사의 논조(긍정/부정/중립)를 분류. 중립적일수록 높은 점수. 두 점수를 50:50으로 결합 |

### 출처 신뢰도 (20%)

| 항목 | 내용 |
|------|------|
| **무엇을** | 기사를 작성한 언론사의 공신력을 평가 (규칙 기반, AI 미사용) |
| **왜** | 같은 내용이라도 검증된 언론사의 기사가 더 신뢰할 수 있음 |
| **어떻게** | 크롤링 시 추출한 원본 언론사명을 사전 정의된 3단계 목록과 대조: **주요 언론사(85점)** — 통신사, 지상파, 종합일간지 등. **등록 매체(65점)** — 인터넷 신문, 경제지 등. **출처 불명(35점)** — 확인 불가 |

### 등급 기준

| 등급 | 점수 범위 | 의미 |
|------|----------|------|
| 신뢰 가능 | 80점 이상 | 제목-본문 일치, 중립적 표현, 검증된 출처 |
| 주의 필요 | 60~79점 | 일부 자극적 표현 또는 출처 확인 필요 |
| 의심 기사 | 40~59점 | 낚시성 제목이나 자극적 내용 포함 가능성 |
| 신뢰 낮음 | 40점 미만 | 제목-본문 불일치, 자극적 표현 다수, 출처 불명 |

### 교차검증을 제외한 이유

초기 설계에서는 여러 언론사의 보도를 교차 비교하는 "교차검증" 지표를 고려했으나,  
외부 뉴스 검색 API 의존성, 실시간 매칭의 구현 난이도, MVP 범위 초과 등의 이유로 현재 버전에서는 제외하였습니다.  
향후 뉴스 검색 API가 확보되면 4번째 지표로 추가를 검토할 수 있습니다.

---

## 2. 시스템 구조

```
사용자 (브라우저)
    │
    ▼
┌──────────┐    URL 전달     ┌──────────┐    메시지 큐    ┌──────────┐
│ Dashboard │ ──────────────▶ │   API    │ ─────────────▶ │ RabbitMQ │
│ :8501    │                 │  :5001   │                │  :5672   │
└──────────┘                 └──────────┘                └────┬─────┘
    ▲                                                         │
    │ 결과 조회                                          큐에서 꺼냄
    │                                                         │
    │                        ┌──────────┐                     ▼
    │                        │  SQLite  │              ┌──────────┐
    └────────────────────────│   DB     │◀─────────────│  Worker  │
                             └──────────┘   분석 결과   │(규칙+AI) │
                                            저장       └──────────┘
```

### 구성 요소

| 서비스 | 역할 | 포트 |
|--------|------|------|
| **Dashboard** | Streamlit 기반 분석 결과 시각화 | 8501 |
| **API** | Flask REST API — 분석 요청 접수, jobs 관리 | 5001 |
| **RabbitMQ** | 메시지 큐 — 분석 요청을 Worker에 분배 | 5672 (관리 UI: 15672) |
| **Worker** | 크롤링 + 규칙 기반/AI 보조 3대 지표 분석 + DB 저장 | - |

### DB 테이블

- **analysis_results** — 분석 결과 (점수, 등급, 상세 근거 데이터)
- **jobs** — 분석 요청 생명주기 (pending → processing → done/failed)

### 데이터 흐름

1. 사용자가 대시보드에서 뉴스 URL을 입력
2. 대시보드 → API 서버로 분석 요청
3. API가 jobs 테이블에 pending job 생성 + RabbitMQ 큐에 job_id/URL 전달
4. Worker가 큐에서 꺼내서 job 상태를 processing으로 갱신
5. Worker가 기사 크롤링 → 3대 지표 분석 → 결과를 DB에 저장 → job 상태를 done으로 갱신
6. 대시보드가 DB에서 결과를 읽어서 화면에 표시

---

## 3. 사전 준비

Docker Desktop과 Git만 있으면 됩니다. Python 등을 따로 설치할 필요 없습니다.

### Docker Desktop 설치

<details>
<summary><b>Mac</b></summary>

```bash
# Homebrew로 설치 (권장)
brew install --cask docker
```

또는 [https://www.docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) 에서 다운로드.

</details>

<details>
<summary><b>Windows</b></summary>

1. PowerShell을 **관리자 권한**으로 열고 `wsl --install` 실행 후 재부팅
2. [https://www.docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) 에서 다운로드 설치
3. 설치 시 "Use WSL 2 instead of Hyper-V" 옵션 확인

</details>

```bash
# 설치 확인
docker --version
```

### Git 설치

Mac은 기본 내장. Windows는 [https://git-scm.com/download/win](https://git-scm.com/download/win) 에서 설치.

---

## 4. 실행 방법

```bash
# 프로젝트 다운로드
git clone https://github.com/your-team/SSAK3.git
cd SSAK3

# 전체 서비스 시작 (첫 실행 시 AI 모델 다운로드로 5~10분 소요)
docker compose up --build
```

### 실행 완료 확인

터미널에 아래 메시지가 나타나면 준비 완료:

```
worker-1  | [Worker] AI 보조지표 모델 로딩 완료!
worker-1  | [Worker] 대기 중... 큐에서 기사를 기다리고 있습니다.
```

브라우저에서 **http://localhost:8501** 접속.

---

## 5. 사용 방법

### 단건 분석

1. 상단 입력창에 네이버 뉴스 URL 입력 (예: `https://n.news.naver.com/mnews/article/001/...`)
2. "분석 요청" 클릭
3. 5~10초 후 결과 표시 (자동 새로고침 체크 가능)

### 대량 분석

1. "대량 뉴스 분석 요청" 영역에 여러 URL을 한 줄에 하나씩 입력
2. "대량 분석 요청" 클릭
3. "5초마다 자동 새로고침" 체크박스로 진행 상황 실시간 확인

### 결과 보기

- **상단 요약 카드** — 전체 기사 수, 평균 신뢰도, 신뢰/위험 기사 수
- **등급별 분포** — 신뢰 가능 / 주의 필요 / 의심 기사 / 신뢰 낮음 비율
- **기사별 상세 결과** — 원형 게이지(종합 점수) + 3가지 지표 프로그레스 바 + 분석 근거
- **분석 근거** — 키워드 매칭 결과, 감지된 자극적 표현, AI 논조 분석, 출처 분류
- **사이드바 필터** — 등급별, 날짜별, 키워드 검색

### 실패 기사 재시도

분석에 실패한 기사는 대시보드 하단에 별도로 표시됩니다.  
"재시도" 버튼을 누르면 같은 URL을 다시 분석 요청할 수 있습니다.  
(failed 상태는 중복으로 간주하지 않으므로 재시도 가능)

---

## 6. Worker 스케일링

Worker를 늘리면 여러 기사를 동시에 분석할 수 있습니다.

```bash
# Worker 3개로 실행
docker compose up --build --scale worker=3

# 백그라운드(detached) 모드로 Worker 3개 실행
docker compose up --scale worker=3 -d

# Worker 5개로 실행
docker compose up --build --scale worker=5
```

| Worker 수 | 동작 방식 | 예상 효과 |
|-----------|----------|----------|
| 1개 (기본) | 기사를 1개씩 순서대로 분석 | 기사당 약 5~10초 |
| 3개 | 3개의 기사를 동시에 분석 | 처리 속도 약 3배 향상 |
| 5개 | 5개의 기사를 동시에 분석 | 처리 속도 약 5배 향상 |

> **주의:** Worker 1개당 약 1~2GB RAM 사용 (AI 모델 메모리).  
> Worker 5개 = 약 5~10GB RAM 필요. Docker Desktop 설정에서 메모리 할당량 확인.  
> (Docker Desktop → Settings → Resources → Memory)

### 분산 처리 구조

현재 구조는 **단일 호스트(single-host) scale-out** 방식입니다.  
한 대의 컴퓨터에서 Docker Compose로 Worker 컨테이너를 여러 개 띄워 병렬 처리합니다.

- RabbitMQ가 메시지를 Worker들에게 라운드-로빈으로 분배
- 각 Worker는 `prefetch_count=1`로 한 번에 1건씩 처리
- SQLite WAL 모드로 동시 읽기/쓰기 지원, 쓰기 충돌 시 자동 재시도 (최대 3회)

---

## 7. 성능 벤치마크

`benchmark.py`로 Worker 수별 처리 성능을 측정할 수 있습니다.

### 실행 방법

```bash
# 사전 조건: RabbitMQ, API, Dashboard가 실행 중이어야 함
docker compose up -d rabbitmq api dashboard

# 벤치마크 실행 (Worker 1/3/5개로 자동 테스트)
python3 benchmark.py
```

### 동작 흐름

1. 네이버 뉴스 5개 섹션(정치/경제/사회/생활/IT)에서 최신 기사 URL 20개 수집
2. Worker 1개 → 3개 → 5개 순서로 각각 전체 기사 분석 + 시간 측정
3. 결과를 `benchmark_results.json`에 저장
4. `docker compose cp` 명령으로 대시보드 컨테이너에 복사

### 결과 확인

대시보드 사이드바에서 **"성능 측정"** 메뉴를 선택하면  
Worker별 총 처리 시간, 기사당 평균, 1000개 예상 시간, 속도 향상 배율을 시각화합니다.

`benchmark_results.json` 예시 구조:
```json
{
  "timestamp": "2026-04-07 15:30:00",
  "num_articles": 20,
  "results": [
    {"workers": 1, "total_time": 120.5, "avg_time": 6.0, "est_1000_min": 100.4},
    {"workers": 3, "total_time": 42.3, "avg_time": 2.1, "est_1000_min": 35.3},
    {"workers": 5, "total_time": 26.1, "avg_time": 1.3, "est_1000_min": 21.8}
  ]
}
```

---

## 8. 프로젝트 구조

```
SSAK3/
├── docker-compose.yml     # 전체 서비스 구성
├── api/
│   ├── Dockerfile
│   └── api_server.py       # Flask REST API (분석 요청 접수, jobs 관리)
├── worker/
│   ├── Dockerfile
│   └── worker.py           # 크롤링 + 규칙 기반/AI 보조 3대 지표 분석 + DB 저장
├── dashboard/
│   ├── Dockerfile
│   └── app.py              # Streamlit 대시보드 (결과 시각화, 진행률 표시)
├── benchmark.py            # Worker 스케일링 성능 벤치마크 스크립트
├── requirements.txt        # 전체 Python 패키지 목록
└── README.md               # 이 문서
```

---

## 9. AI 모델 설명

### KR-FinBert-SC

- **모델**: `snunlp/KR-FinBert-SC` (HuggingFace)
- **원래 용도**: 한국어 금융 뉴스 감성분석 (긍정/부정/중립 3-class 분류)
- **이 프로젝트에서의 역할**: **자극성 분석의 보조지표**
  - 기사의 논조(중립/긍정/부정)를 분류하여 자극성 점수 산출에 보조적으로 활용
  - 규칙 기반 자극적 단어 감지와 50:50으로 결합
  - 뉴스의 진위를 직접 판정하는 모델이 **아님**
- **로딩 방식**: `AutoTokenizer` + `AutoModelForSequenceClassification` 직접 로딩
  - `pipeline()` 대비 메모리 효율적이고 배치 추론 가능
  - `torch.no_grad()`로 그래디언트 비활성화 → 메모리 40%↓, 속도 20~30%↑

### 배치 추론 (analyze_ai_sentiment_batch)

현재 Worker는 기사를 1건씩 큐에서 꺼내 처리하므로, 배치 추론 함수(`analyze_ai_sentiment_batch`)는 실제로 호출되지 않습니다.  
Worker가 큐에서 여러 메시지를 모아 배치 처리하는 구조로 전환하면 이 함수를 활용할 수 있으며, 향후 개선 대상입니다.

---

## 10. 종료 방법

```bash
# Ctrl + C로 실행 중인 컨테이너 정지 후:
docker compose down

# 데이터까지 완전히 삭제:
docker compose down -v
```

---

## 11. 문제 해결

### "port 5001 already in use"

- **Mac**: 시스템 설정 → 일반 → AirDrop 및 Handoff → AirPlay 수신 모드 끔
- **Windows**: `netstat -ano | findstr :5001`로 프로세스 확인 후 종료
- **공통**: `docker-compose.yml`에서 API 포트를 `"5002:5000"` 등으로 변경

### "database is locked" (Worker 로그)

여러 Worker가 동시에 DB에 쓰려고 할 때 발생할 수 있습니다.  
자동으로 3회까지 재시도하므로 대부분 자동 해결됩니다.

### AI 모델 로딩이 오래 걸림

처음 실행 시 AI 모델(약 400MB)을 다운로드합니다.  
인터넷 속도에 따라 5~10분 걸릴 수 있으며, 이후에는 캐시되어 빠르게 시작됩니다.

### 크롤링 실패

- 네이버 뉴스 URL(`https://n.news.naver.com/...`) 형식이 가장 안정적
- 네이버 외 사이트도 지원하지만 일부 사이트는 크롤링이 차단될 수 있음
- 대시보드에서 실패 건의 "재시도" 버튼을 눌러 다시 시도 가능

---

## 12. 한계와 향후 개선

### 현재 한계

- **SQLite 동시성**: 파일 기반 DB라 다중 Worker 환경에서 쓰기 잠금 발생 가능 (WAL + 재시도로 완화)
- **단일 호스트**: 현재 Docker Compose 기반으로 한 대의 컴퓨터에서만 실행
- **AI 모델 한계**: KR-FinBert-SC는 금융 뉴스 기반 학습이라, 일반 뉴스의 논조를 100% 정확히 분류하지 못할 수 있음
- **출처 목록 하드코딩**: 언론사 분류 목록이 코드에 직접 포함되어 있어 유지보수가 번거로움

### 향후 개선 방향

- **PostgreSQL 전환**: 다중 Worker/다중 호스트에서 안정적인 동시 쓰기 지원
- **Multi-host 분산 배포**: Docker Swarm / Kubernetes로 여러 컴퓨터에 Worker 분산
- **교차검증 지표 추가**: 뉴스 검색 API 확보 시 4번째 지표로 다른 언론사 보도와 비교
- **더 적합한 AI 모델**: 뉴스 전용 한국어 감성분석 모델로 교체하여 보조지표 정확도 향상
- **출처 목록 외부화**: JSON/YAML 설정 파일로 분리하여 코드 수정 없이 언론사 목록 관리
- **배치 추론 활용**: Worker가 여러 기사를 모아 한번에 AI 추론하여 처리 효율 향상
