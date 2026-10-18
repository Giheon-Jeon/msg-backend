# 제한된 SSH 백엔드 배포

GCP의 기존 WIF provider가 새 중앙 워크플로 인증을 attribute condition에서 거절해 서버 변경 전에 자동 배포가 중단됐다
백엔드 배포는 이미 관리 접속이 허용된 서버에 전용 제한 계정을 두고 고정 helper를 호출한다
GCP IAM 조건이나 서버 방화벽을 변경하지 않는다

## 배포 경계

- GitHub development 환경 Secret: BACKEND_DEPLOY_HOST, BACKEND_DEPLOY_SSH_KEY, BACKEND_DEPLOY_KNOWN_HOSTS
- 서버 계정: msg-backend-deploy, Docker 그룹과 일반 sudo 권한 없음
- root 소유 authorized_keys: restrict와 고정 forced command 적용
- 허용 sudo 명령: /usr/bin/python3 /usr/local/lib/msgctf-backend-deploy/backend_release.py
- 요청은 action, source_sha, deployment_id, pipeline_revision 네 항목의 작은 JSON으로 제한
- 서버는 현재 MSG-CTF/msg-backend main SHA만 배포하고 임의 저장소·명령·파일 경로·helper를 받지 않음
- 서버의 root 소유 trusted-central.json은 중앙 배포 SHA와 shell·프록시 helper checksum을 고정

중앙 배포 버전은 0a9fc98c36cb8b4ce5a7a8ea55280bbf24d2a0f9다
검토한 deploy-development-vm.sh와 backend_proxy_bridge.py를 서버 고정 경로에 설치한 뒤 사용한다
CI에서 배포 코드를 업로드하지 않으며 고정된 root 진입점에 배포 식별자만 보낸다
서버의 앱 환경변수와 DB·Redis 인증 정보는 기존 서버 설정에 유지한다

CI와 배포 진입점 검사가 성공한 main push만 배포 job을 실행한다
main push는 진행 중인 배포를 취소하지 않고 호스트의 기존 flock으로 프론트 배포와도 직렬화한다
실제 배포는 검증된 후보로 전환하고 운영 백엔드를 교체하며 실패 시 이전 이미지 복구를 검증한다
배포 로그는 root 전용 restricted-deploy-실행ID.log에 남기고 GitHub에는 검증 결과만 전달한다

## 설치와 확인

관리자는 검토·CI가 끝난 같은 SHA의 backend_release.py와 검증된 중앙 파일 두 개를 고정 경로에 설치한다
설치 디렉터리와 파일은 root 소유로 두고 그룹·다른 사용자의 쓰기를 허용하지 않는다
전용 계정의 홈과 SSH 설정도 root 소유로 두어 authorized_keys 교체를 막는다
sudoers는 visudo 검사 후 고정 Python 진입점만 허용한다
서버 host key는 기존 관리 접속에서 확인한 값으로 등록하며 최초 연결 자동 수락이나 keyscan 결과를 신뢰하지 않는다

workflow_dispatch의 verify_deploy=true는 고정 설치와 실제 DB·Redis healthz만 검사한다
이 동작은 운영 컨테이너를 교체하지 않는다
검토한 브랜치에서 이 조회를 통과한 후 main으로 머지하고 실제 배포의 외부 HTTPS·인증 경계·SLA 보고·후보 정리를 확인한다
표준 SSH 키로 일반 명령을 요청해도 forced command가 JSON 형식과 허용 동작을 검사한다

중앙 helper나 배포 프로토콜을 바꿀 때는 CI와 검토 후 관리 접속으로 고정 설치를 갱신한다
GitHub key 자체가 임의 root 코드 업로드나 대화형 셸 권한을 갖도록 바꾸지 않는다
