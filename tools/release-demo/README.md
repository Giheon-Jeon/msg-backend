# 릴리스 데모 하네스

문제 릴리스 버전 관리 기능을 로컬에서 눈으로 확인하는 도구.
실백엔드 + 가짜 스케줄러 + 가짜 GitHub Actions artifact 저장소를 붙여
출제자 push부터 버전 축적, 전환, 인스턴스 배포까지 전체 흐름을 재현한다.

문제 목록은 2026_MSG_CTF의 info.yaml 구조를 그대로 담은 catalog.json 기준이며
플래그 값은 자리표시자로 바꿔 두었다.

## 실행 순서

저장소 루트에서 터미널 3개로 실행한다. 의존성은 requirements.txt 그대로다.

1 시드 (마이그레이션 포함, 계정 root와 player, 비밀번호 pw1234)

```bash
python tools/release-demo/seed_demo.py
```

2 가짜 스케줄러 (터미널 1)

```bash
python tools/release-demo/fake_scheduler.py
```

3 백엔드 (터미널 2)

PowerShell

```powershell
$env:PYTHONPATH = "tools/release-demo"; $env:DJANGO_SETTINGS_MODULE = "demo_settings"; python manage.py runserver 127.0.0.1:8010 --noreload
```

bash

```bash
PYTHONPATH=tools/release-demo DJANGO_SETTINGS_MODULE=demo_settings python manage.py runserver 127.0.0.1:8010 --noreload
```

4 대시보드 (터미널 3)

```bash
python tools/release-demo/demo_server.py
```

브라우저에서 http://127.0.0.1:8020 을 연다.

## 선택: 자동 폴러까지 붙이기

push 시뮬레이션이 발행한 bundle을 폴러가 수집해 자동 등록하는 흐름을 보려면
터미널을 하나 더 열어 실행한다.

PowerShell

```powershell
$env:PYTHONPATH = "tools/release-demo"; $env:DJANGO_SETTINGS_MODULE = "demo_settings"; $env:RELEASE_POLL_API_BASE = "http://127.0.0.1:8020"; $env:RELEASE_POLL_GITHUB_TOKEN = "demo"; python manage.py poll_releases --interval 4
```

bash

```bash
PYTHONPATH=tools/release-demo DJANGO_SETTINGS_MODULE=demo_settings RELEASE_POLL_API_BASE=http://127.0.0.1:8020 RELEASE_POLL_GITHUB_TOKEN=demo python manage.py poll_releases --interval 4
```

폴러 없이 쓰려면 push 시뮬레이션 대신 등록 API를 직접 호출해도 된다.

## 화면 사용법

- 문제 행을 누르면 그 문제의 플래그와 버전 이력이 열린다
- 출제자 push 시뮬레이션: 공급망이 새 digest bundle을 발행한 상황을 재현한다 (폴러가 있어야 이력에 자동 등장)
- 전환: 그 버전이 이후 인스턴스에 배포된다. 옛 버전 전환이 곧 롤백이다
- 참가자 인스턴스 생성: 우측 패널에서 스케줄러가 실제로 받은 이미지 digest를 확인한다
- 멀티 컨테이너 릴리스는 Scheduler 계약 확장 전까지 전환이 거절된다 (RELEASE_NOT_DEPLOYABLE)

## 초기화

.data 폴더를 지우고 시드부터 다시 실행한다.
