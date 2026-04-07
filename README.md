# SSAK3 - AI 기반 뉴스 신뢰도 분석 시스템

뉴스 기사의 URL을 입력하면, AI가 자동으로 기사를 수집하고 신뢰도를 분석해주는 시스템입니다.

---

## 1. 사전 준비

이 프로젝트는 Docker로 실행되기 때문에 **Python, Node.js 등을 따로 설치할 필요가 없습니다.**  
아래 2가지만 준비하면 됩니다.

### Docker Desktop 설치

<details>
<summary><b>Mac</b></summary>

**방법 1) Homebrew로 설치 (권장)**

```bash
# Homebrew가 설치되어 있다면 한 줄로 끝
brew install --cask docker
```

**방법 2) 공식 사이트에서 다운로드**

1. [https://www.docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) 에 접속합니다.
2. **Download for Mac** 버튼을 클릭합니다.
   - Apple Silicon (M1/M2/M3/M4) → **Mac with Apple chip** 선택
   - Intel Mac → **Mac with Intel chip** 선택
3. 다운로드된 `.dmg` 파일을 열고, Docker 아이콘을 Applications 폴더로 드래그합니다.
4. Applications에서 Docker를 실행합니다. (처음 실행 시 권한 허용 필요)
5. 상단 메뉴바에 고래 아이콘이 나타나면 설치 완료입니다.

</details>

<details>
<summary><b>Windows</b></summary>

**1단계) WSL2 설치 (필수 선행 작업)**

Docker Desktop은 Windows에서 WSL2(Windows Subsystem for Linux 2)를 백엔드로 사용합니다.  
PowerShell을 **관리자 권한**으로 열고 아래 명령어를 실행합니다:

```powershell
wsl --install
```

실행 후 **컴퓨터를 재부팅**합니다.  
재부팅 후 Ubuntu 터미널이 자동으로 열리면 사용자 이름과 비밀번호를 설정합니다.

> **"가상화가 비활성화되어 있습니다" 에러가 나오면:**  
> 컴퓨터를 재부팅하고 BIOS 설정에 진입합니다 (부팅 시 F2, F10, 또는 DEL 키).  
> **Intel:** VT-x 또는 Intel Virtualization Technology → **Enabled**  
> **AMD:** SVM Mode → **Enabled**  
> 설정 변경 후 저장하고 재부팅합니다.

**2단계) Docker Desktop 설치**

1. [https://www.docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) 에 접속합니다.
2. **Download for Windows** 버튼을 클릭하여 설치 파일을 다운로드합니다.
3. 다운로드된 `Docker Desktop Installer.exe`를 실행합니다.
4. 설치 중 **"Use WSL 2 instead of Hyper-V"** 옵션이 체크되어 있는지 확인합니다.
5. 설치 완료 후 Docker Desktop을 실행합니다.
6. 작업 표시줄 트레이에 고래 아이콘이 나타나고 **"Docker Desktop is running"** 상태이면 완료입니다.

</details>

**설치 확인 (Mac/Windows 동일):**

```bash
docker --version
# Docker version 27.x.x 같은 출력이 나오면 성공
```

### Git 설치

<details>
<summary><b>Mac</b></summary>

Mac에는 기본적으로 Git이 설치되어 있습니다. 터미널에서 확인해보세요:

```bash
git --version
```

만약 설치되어 있지 않다면 아래 두 가지 방법 중 하나를 선택하세요:

```bash
# 방법 1) Xcode Command Line Tools (팝업이 뜨면 "설치" 클릭)
xcode-select --install

# 방법 2) Homebrew
brew install git
```

</details>

<details>
<summary><b>Windows</b></summary>

1. [https://git-scm.com/download/win](https://git-scm.com/download/win) 에 접속합니다.
2. **"Click here to download"** 링크를 클릭하여 설치 파일을 다운로드합니다.
3. 설치 파일을 실행하고 모든 옵션을 **기본값 그대로** 두고 Next를 눌러 설치합니다.
4. 설치 완료 후 PowerShell 또는 명령 프롬프트를 **새로 열고** 확인합니다:

```bash
git --version
```

> **참고:** 설치 시 "Adjusting your PATH environment" 단계에서  
> **"Git from the command line and also from 3rd-party software"** 가 선택되어 있는지 확인하세요.

</details>

---

## 2. 프로젝트 다운로드

터미널을 열고 아래 명령어를 순서대로 입력합니다.

**터미널 여는 방법:**
- **Mac:** Spotlight(Cmd + Space) → "Terminal" 검색 → 실행
- **Windows:** 시작 메뉴 → "PowerShell" 검색 → 실행 (또는 "명령 프롬프트")

```bash
# 프로젝트 다운로드
git clone https://github.com/your-team/SSAK3.git

# 프로젝트 폴더로 이동
cd SSAK3
```

> **참고:** `your-team` 부분은 실제 GitHub 저장소 주소로 바꿔주세요.

---

## 3. 실행 방법

### 프로젝트 시작 (Mac/Windows 동일)

```bash
docker compose up --build
```

이 명령어 하나로 4개의 서비스가 자동으로 실행됩니다:
- **RabbitMQ** — 메시지 큐 (기사 분석 요청을 전달하는 우체통 역할)
- **API 서버** — 사용자의 분석 요청을 받는 서버
- **Worker** — 실제로 기사를 크롤링하고 AI로 분석하는 서버
- **대시보드** — 분석 결과를 보여주는 웹 화면

### 처음 실행 시 주의사항

**처음 실행하면 AI 모델 다운로드 때문에 5~10분 정도 걸릴 수 있습니다.**  
이건 최초 1회만 오래 걸리고, 그 다음부터는 30초 내로 실행됩니다.

### 실행 완료 확인

터미널 로그에 아래 메시지가 나타나면 모든 준비가 완료된 것입니다:

```
worker-1  | [Worker] AI 모델 로딩 완료!
worker-1  | [Worker] 대기 중... 큐에서 기사를 기다리고 있습니다.
```

이 메시지가 보이면 브라우저를 열고 다음 단계로 넘어가세요.

---

## 4. 사용 방법

### 대시보드 접속

브라우저에서 아래 주소로 접속합니다:

```
http://localhost:8501
```

### 단건 분석

1. 상단의 **"뉴스 기사 분석 요청"** 입력창에 네이버 뉴스 URL을 붙여넣습니다.
   - 예: `https://n.news.naver.com/mnews/article/001/0014xxxxx`
2. **"분석 요청"** 버튼을 클릭합니다.
3. 잠시 기다린 후 (보통 5~10초) **페이지를 새로고침** 하면 결과가 나타납니다.
   - Mac: `Cmd + R`
   - Windows: `F5` 또는 `Ctrl + R`

### 대량 분석

1. **"대량 뉴스 분석 요청"** 텍스트 영역에 여러 URL을 한 줄에 하나씩 입력합니다:
   ```
   https://n.news.naver.com/mnews/article/001/...
   https://n.news.naver.com/mnews/article/002/...
   https://n.news.naver.com/mnews/article/003/...
   ```
2. **"대량 분석 요청"** 버튼을 클릭합니다.
3. **"5초마다 자동 새로고침"** 체크박스를 켜면 진행 상황이 자동으로 갱신됩니다.

### 결과 보기

- **상단 요약 카드** — 전체 기사 수, 평균 신뢰도, 신뢰/위험 기사 수
- **등급별 분포** — 신뢰 가능 / 주의 필요 / 의심 기사 / 신뢰 낮음 비율
- **기사별 상세 결과** — 원형 게이지(종합 점수) + 3가지 지표 프로그레스 바 + 분석 근거
- **사이드바 필터** — 등급별, 날짜별, 키워드 검색으로 결과를 필터링할 수 있습니다

---

## 5. Worker 수 조절 방법

Worker는 기사를 분석하는 일꾼입니다. Worker를 늘리면 여러 기사를 동시에 분석할 수 있습니다.

### Worker 3개로 실행 (Mac/Windows 동일)

```bash
docker compose up --build --scale worker=3
```

### Worker 5개로 실행

```bash
docker compose up --build --scale worker=5
```

### Worker를 늘리면 뭐가 달라지나요?

| Worker 수 | 동작 방식 | 예상 효과 |
|-----------|----------|----------|
| 1개 (기본) | 기사를 1개씩 순서대로 분석 | 기사당 약 5~10초 |
| 3개 | 3개의 기사를 동시에 분석 | 처리 속도 약 3배 향상 |
| 5개 | 5개의 기사를 동시에 분석 | 처리 속도 약 5배 향상 |

> **주의:** Worker마다 AI 모델을 메모리에 로딩하므로, Worker 1개당 약 1~2GB의 RAM을 사용합니다.  
> Worker 5개 = 약 5~10GB RAM 필요. Docker Desktop 설정에서 메모리 할당량을 확인하세요.  
> (Docker Desktop → Settings → Resources → Memory)

---

## 6. 종료 방법

### 터미널에서 종료 (Mac/Windows 동일)

```bash
# Ctrl + C 를 눌러서 실행 중인 컨테이너를 정지한 뒤:
docker compose down
```

`docker compose down`은 모든 컨테이너를 정지하고 삭제합니다.  
분석 결과 데이터는 Docker 볼륨에 보존되므로, 다음에 다시 실행하면 이전 결과가 그대로 남아있습니다.

### 데이터까지 완전히 삭제하고 싶다면

```bash
docker compose down -v
```

`-v` 옵션을 추가하면 볼륨(DB 데이터)까지 삭제합니다.

### Docker Desktop에서 확인

Docker Desktop 앱을 열면 **Containers** 탭에서 실행 중인 컨테이너 목록을 볼 수 있습니다.  
SSAK3 관련 컨테이너가 모두 **Exited** 상태이면 정상 종료된 것입니다.

---

## 7. 문제 해결

### "port 5001 already in use" 에러

<details>
<summary><b>Mac인 경우</b></summary>

Mac의 AirPlay 수신 기능이 5000번 포트를 사용하고 있을 수 있습니다.

**해결 방법:**
1. **시스템 설정** → **일반** → **AirDrop 및 Handoff** 로 이동
2. **AirPlay 수신 모드** 를 **끔** 으로 변경

</details>

<details>
<summary><b>Windows인 경우</b></summary>

다른 프로그램이 5001번 포트를 사용하고 있을 수 있습니다.

**1단계) 어떤 프로그램이 포트를 사용 중인지 확인:**

```powershell
netstat -ano | findstr :5001
```

출력 예시:
```
TCP    0.0.0.0:5001    0.0.0.0:0    LISTENING    12345
```

맨 끝의 숫자(12345)가 해당 프로세스의 PID입니다.

**2단계) 해당 프로세스 종료:**

```powershell
taskkill /PID 12345 /F
```

</details>

**공통 해결 방법:** `docker-compose.yml`의 API 포트를 다른 번호로 변경해도 됩니다:
```yaml
# docker-compose.yml에서 이 부분을 수정
ports:
  - "5002:5000"  # 5001 대신 5002 사용
```

### "database is locked" 에러 (Worker 로그)

여러 Worker가 동시에 DB에 쓰려고 할 때 발생할 수 있습니다.  
**자동으로 3회까지 재시도** 하도록 구현되어 있으므로, 대부분 기다리면 해결됩니다.  
로그에 `DB 잠금 감지, 1/3 재시도` 메시지가 나오더라도 정상 동작입니다.

### 크롤링 실패 (분석 결과가 "분석 실패"로 표시)

- **네이버 뉴스 URL인지 확인하세요.** `https://n.news.naver.com/...` 형식이 가장 잘 동작합니다.
- 네이버 외 사이트도 지원하지만, 일부 사이트는 크롤링이 차단될 수 있습니다.
- URL을 브라우저에서 직접 열어보고, 기사가 정상적으로 보이는지 확인해보세요.

### Worker 로그에서 AI 모델 로딩이 멈춘 것 같을 때

처음 실행 시 AI 모델(약 400MB)을 다운로드합니다. 인터넷 속도에 따라 5~10분 걸릴 수 있습니다.  
`[Worker] AI 모델 로딩 중...` 메시지가 나온 후 오래 걸리더라도 기다려주세요.

### Docker Desktop이 실행되지 않을 때 (Windows)

<details>
<summary><b>증상별 해결 방법</b></summary>

**"WSL 2 installation is incomplete" 에러:**

PowerShell을 **관리자 권한**으로 열고 실행합니다:
```powershell
wsl --update
```
실행 후 Docker Desktop을 다시 시작합니다.

**"Hardware assisted virtualization and data execution protection must be enabled in the BIOS" 에러:**

1. 컴퓨터를 재부팅합니다.
2. 부팅 시 BIOS 진입 키를 누릅니다 (제조사별로 다름):
   - **삼성/LG:** F2
   - **HP:** F10
   - **Dell/Lenovo:** F2 또는 DEL
3. BIOS 설정에서 가상화 옵션을 찾아 활성화합니다:
   - **Intel CPU:** `Intel Virtualization Technology (VT-x)` → **Enabled**
   - **AMD CPU:** `SVM Mode` → **Enabled**
4. 설정을 저장하고 재부팅합니다 (보통 F10 → Save & Exit).

**"Docker Desktop - Unexpected WSL error" 에러:**

PowerShell을 **관리자 권한**으로 열고 순서대로 실행합니다:
```powershell
wsl --shutdown
wsl --update
```
실행 후 Docker Desktop을 다시 시작합니다.

</details>

---

## 8. 프로젝트 구조

```
SSAK3/
├── docker-compose.yml     # 전체 서비스 구성 (이 파일 하나로 4개 서비스 실행)
├── api/
│   ├── Dockerfile          # API 서버 Docker 이미지 설정
│   └── api_server.py       # Flask REST API (분석 요청 접수 → RabbitMQ 전달)
├── worker/
│   ├── Dockerfile          # Worker Docker 이미지 설정 (AI 모델 포함)
│   └── worker.py           # 크롤링 + 3가지 지표 분석 + DB 저장
├── dashboard/
│   ├── Dockerfile          # 대시보드 Docker 이미지 설정
│   └── app.py              # Streamlit 대시보드 (결과 시각화)
├── benchmark.py            # Worker 스케일링 성능 벤치마크 스크립트
└── README.md               # 이 문서
```

### 데이터 흐름

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
                             └──────────┘   분석 결과   │ (AI 분석) │
                                            저장       └──────────┘
```

**단계별 설명:**

1. **사용자**가 대시보드(localhost:8501)에서 뉴스 URL을 입력합니다.
2. **대시보드**가 API 서버(localhost:5001)로 분석 요청을 보냅니다.
3. **API 서버**가 URL을 RabbitMQ 메시지 큐에 넣습니다.
4. **Worker**가 큐에서 URL을 꺼내서:
   - 기사를 크롤링하고 (제목, 본문, 언론사 추출)
   - AI 모델로 3가지 지표를 분석하고
   - 결과를 SQLite DB에 저장합니다.
5. **대시보드**가 DB에서 결과를 읽어서 화면에 표시합니다.

---

## 9. 신뢰도 분석 기준

종합 신뢰도 점수는 3가지 지표의 가중 평균으로 계산됩니다.

### 본문 일치도 (45%)

| 항목 | 내용 |
|------|------|
| **무엇을** | 기사 제목과 본문 내용이 얼마나 일치하는지 측정합니다. |
| **왜** | 낚시성 기사는 자극적인 제목을 달지만, 본문에는 관련 내용이 없는 경우가 많습니다. 제목과 본문이 일치할수록 신뢰할 수 있는 기사입니다. |
| **어떻게** | 1) **코사인 유사도** — 제목과 본문 첫 3문장(리드)의 텍스트 유사도를 TF-IDF로 계산합니다. 2) **키워드 매칭** — 제목에서 추출한 핵심 명사가 본문에 실제로 등장하는 비율을 계산합니다. 두 점수를 50:50으로 결합합니다. |

### 자극성 분석 (35%)

| 항목 | 내용 |
|------|------|
| **무엇을** | 기사에 자극적이거나 선정적인 표현이 얼마나 사용되었는지 측정합니다. |
| **왜** | 신뢰할 수 있는 기사는 객관적이고 중립적인 표현을 사용합니다. "충격", "경악", "역대급" 같은 표현이 많을수록 클릭을 유도하기 위한 저품질 기사일 가능성이 높습니다. |
| **어떻게** | 1) **자극적 단어 감지** — 4가지 카테고리(과장, 혐오, 선정, 공포)의 단어 사전으로 자극적 표현을 찾습니다. 제목에 나타나면 가중치 2배를 적용합니다. 2) **AI 감성분석** — KR-FinBert-SC 모델이 기사의 감성(긍정/부정/중립)을 판별합니다. 중립적일수록 높은 점수를 받습니다. 두 점수를 50:50으로 결합합니다. |

### 출처 신뢰도 (20%)

| 항목 | 내용 |
|------|------|
| **무엇을** | 기사를 작성한 언론사의 공신력을 평가합니다. |
| **왜** | 같은 내용이라도 검증된 언론사의 기사가 더 신뢰할 수 있습니다. 출처가 불명확한 기사는 오보나 가짜뉴스일 위험이 높습니다. |
| **어떻게** | 크롤링 시 추출한 원본 언론사명을 3단계로 분류합니다: **주요 언론사(85점)** — 통신사(연합뉴스), 지상파(KBS, MBC, SBS), 종합일간지 등. **등록 매체(65점)** — 인터넷 신문, 경제지, IT 전문지 등. **출처 불명(35점)** — 언론사를 확인할 수 없는 경우. |

### 종합 점수 계산

```
종합 점수 = 본문 일치도 × 0.45 + 자극성 점수 × 0.35 + 출처 점수 × 0.20
```

### 등급 기준

| 등급 | 점수 범위 | 의미 |
|------|----------|------|
| 신뢰 가능 | 80점 이상 | 제목-본문 일치, 중립적 표현, 검증된 출처 |
| 주의 필요 | 60~79점 | 일부 자극적 표현 또는 출처 확인 필요 |
| 의심 기사 | 40~59점 | 낚시성 제목이나 자극적 내용 포함 가능성 |
| 신뢰 낮음 | 40점 미만 | 제목-본문 불일치, 자극적 표현 다수, 출처 불명 |
