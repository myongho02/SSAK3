# GHCR 이미지 Public 전환 (1회 작업)

> GitHub Actions가 빌드한 이미지가 기본적으로 **비공개**라서, 팀원이 `docker pull` 시 인증 에러가 납니다.
> 이를 한 번만 public으로 전환하면 팀원이 인증 없이 이미지를 받을 수 있게 됩니다.

---

## 절차 (총 2~3분)

### 1단계 — GitHub Actions 빌드 완료 대기

🔗 https://github.com/myongho02/SSAK3/actions

`Release (GHCR 이미지 자동 푸시)` workflow가 ✅ 모두 성공해야 합니다 (3개 이미지 = 3개 작업).
빌드 시간: 첫 빌드 5~10분, 이후 캐시로 2~3분.

### 2단계 — Packages 페이지로 이동

🔗 https://github.com/myongho02?tab=packages

다음 3개 패키지가 보여야 합니다:
- `ssak3-api`
- `ssak3-worker`
- `ssak3-dashboard`

### 3단계 — 각 패키지를 Public으로 전환 (3번 반복)

**`ssak3-api` 클릭 → "Package settings"** (오른쪽 사이드바 또는 우측 상단 ⚙️) → 페이지 맨 아래로 스크롤 → **"Change visibility"** → **"Public"** 선택 → 패키지명 입력해서 확인.

같은 작업을 `ssak3-worker`, `ssak3-dashboard`에도 반복.

### 4단계 — 검증

본인 노트북에서:
```bash
# 토큰 없이 pull 가능하면 OK
docker pull ghcr.io/myongho02/ssak3-api:latest
docker pull ghcr.io/myongho02/ssak3-worker:latest
docker pull ghcr.io/myongho02/ssak3-dashboard:latest
```

---

## 팀원에게 보낼 메시지 템플릿

```
SSAK3 프로토타입 배포했어. 다음 절차로 본인 노트북에서 띄울 수 있어:

1. Docker Desktop 켜기
2. git clone https://github.com/myongho02/SSAK3.git
3. cd SSAK3
4. echo "GHCR_OWNER=myongho02" > .env
5. docker compose -f docker-compose.prod.yml pull
6. docker compose -f docker-compose.prod.yml up -d
7. http://localhost:8501

처음 pull은 1~2분 걸려. 자세한 가이드는 docs/팀원_배포_가이드.md 참고.

문제 생기면 docker compose -f docker-compose.prod.yml ps랑 logs api 결과 보여줘.
```

---

## 보안 주의

- Public으로 전환하면 **누구나 이미지를 받아서 실행 가능**합니다.
- 다만 SSAK3 이미지에는 **민감 정보가 들어있지 않습니다**:
  - 코드: 이미 GitHub public repo
  - 모델: HuggingFace에서 받은 공개 모델
  - DB: 사용자가 별도 셋업하므로 이미지에 포함 X
  - 비밀번호/토큰: `.env`에 분리되어 있고 `.gitignore` 등록됨
- 따라서 **이미지 public 전환은 안전**합니다.

운영 시 만약 비공개로 유지하고 싶으면, 팀원에게 GitHub Personal Access Token(PAT)을 받게 해서 `docker login ghcr.io` 후 사용하게 하면 됩니다.

---

## 자동화하고 싶으면 (선택)

매번 새 패키지가 생길 때마다 자동으로 public 전환하려면 GitHub API + GH_TOKEN 사용:

```bash
gh auth login   # 1회
for pkg in ssak3-api ssak3-worker ssak3-dashboard; do
  gh api -X PATCH "/user/packages/container/$pkg/visibility" -f visibility=public
done
```

(현재는 패키지가 3개라 수동이 더 빠름)
