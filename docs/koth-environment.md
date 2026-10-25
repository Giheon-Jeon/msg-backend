# KOTH 환경변수 운영

관리자 KOTH 상세의 환경변수 탭에서 컨테이너별 값을 확인하고 새 버전을 저장한다
저장만으로 VM을 재시작하지 않는다
적용 요청 뒤 VM의 실제 값 대조가 끝나야 적용 확인으로 표시한다

## 최초 연결

운영자가 실행 중인 이미지 digest와 컨테이너별 환경변수를 대조한 뒤 아래 명령으로 기준을 등록한다
입력 JSON은 표준 입력으로 전달하고 비밀값을 명령행 인자나 로그에 남기지 않는다

    python manage.py configure_koth_environment --actor operator < /root/reviewed-koth-baseline.json

입력에는 challenge_id, source_revision, target_id, agent_token_hash, settings를 담는다
target_id는 gcp:VM인스턴스ID 형식이며 source_revision은 KothChallenge에 연결된 실행 이미지의 소스 커밋과 같아야 한다
agent_token_hash는 문제별 에이전트 전용 토큰의 SHA-256이며 게임 인증 토큰과 구분한다
settings는 버전 계약의 containers와 entries 형식을 따른다
등록 직후에는 적용 대기 상태로 두며 에이전트가 현재 값을 대조하면 재시작 없이 적용을 확인한다
이미 등록된 다른 설정은 이 명령으로 덮어쓰지 않는다

## 저장과 적용

- GET /api/v1/admin/koth/challenges/{challenge_id}/environment
- POST /api/v1/admin/koth/challenges/{challenge_id}/environment/preview
- POST /api/v1/admin/koth/challenges/{challenge_id}/environment/versions
- POST /api/v1/admin/koth/challenges/{challenge_id}/environment/versions/{version_id}/apply

저장에는 base_version_id, container, method, request_id를 사용한다
method가 file이면 content와 선택적인 rules를 추가하고 repository이면 실행 이미지와 같은 커밋의 env 파일을 읽는다
다른 관리자가 저장한 뒤에는 최신 버전을 선택해 다시 검토한다
일반 값과 비밀값 모두 암호화된 버전에 저장하고 관리자 응답에는 비밀값 원문을 반환하지 않는다
빈 값과 설정하지 않음은 구분하며 필수 항목과 보호된 연동값은 해제할 수 없다
저장소 가져오기는 운영에서 별도로 정한 값과 연동 토큰을 유지한다

적용에는 별도의 request_id를 전달한다
동일 요청은 동일 적용 기록으로 처리하며 이전 적용의 재시도가 다음 적용을 덮어쓰지 않는다
과거 버전으로 복구할 때도 해당 버전에 새 적용 요청을 보낸다
진행 중인 경기는 새 적용을 차단하고 적용 중인 작업이 2분 이상 확인되지 않을 때만 새 요청으로 복구할 수 있다

## 전용 VM 에이전트

- GET /internal/v1/koth_environments/{challenge_id}?known_application_id=UUID
- POST /internal/v1/koth_environments/{challenge_id}/status

문제별 전용 Bearer 토큰으로 자기 문제만 조회한다
변경 없는 조회에는 비밀값을 다시 복호화하거나 전송하지 않는다
결과에는 application_id, version_id, state와 선택적인 error_code만 전달한다
state는 APPLYING, APPLIED, FAILED이며 이전 적용의 결과는 409로 거절한다
에러에는 사전에 정한 코드만 받아 비밀값과 원격 명령 출력을 기록하지 않는다

에이전트의 고정 VM·namespace·Deployment·이미지·연동값 목록도 같은 실행 기준으로 등록해야 한다
이미지나 소스 커밋을 교체할 때는 운영자가 새 실행 기준을 검증하고 에이전트 연결도 함께 갱신한다
일반 관리자 환경변수 수정으로 이미지·네트워크·자원·PVC·게임 코드를 바꾸지 않는다
