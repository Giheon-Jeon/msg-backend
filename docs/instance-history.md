# 종료된 인스턴스 이력 갱신

생성·실행 중인 이력도 조회해 TTL·하드타임아웃 종료를 반영합니다
새 인스턴스가 실행 중이어도 이전 STOPPING·CLEANUP_PENDING 기록을 따로 조회합니다
사용자가 화면을 닫거나 현재 실행이 없어져도 주기 작업이 갱신을 이어갑니다
일반 삭제도 같은 방식으로 처리합니다

스케줄러가 반환한 인스턴스·팀·문제·릴리스가 저장된 값과 일치할 때만 상태를 반영합니다
현재 단건 응답에 없는 user_id는 응답에 포함된 경우에만 대조합니다
404나 통신 오류만으로 CLEANED를 기록하지 않습니다
조회 중 변경된 행은 건너뛰고 다음 주기에 다시 확인합니다
실행 설정과 이미 저장된 삭제 사유는 보존합니다
비어 있는 삭제 사유는 스케줄러의 delete_reason을 사용합니다
TTL_EXPIRED·IDLE_EXPIRED는 같은 값으로, HARD_TIMEOUT_EXPIRED는 백엔드 HARD_TIMEOUT으로 저장합니다
CREATE_FAILED_CLEANUP은 생성 실패의 정리 사유이므로 참가자 삭제 사유를 만들지 않습니다
기존 스케줄러가 사유를 생략하면 시간을 비교해 추측하지 않습니다
종료 사유까지 반영하려면 스케줄러의 delete_reason 응답을 먼저 배포해야 합니다
참가자 조회로 먼저 종료 상태를 확인한 경우에도 같은 사유 매핑을 사용합니다

한 주기는 기본 100건·20초이며, 이미 진행 중인 HTTP 요청 한 건은 설정된 timeout까지 기다릴 수 있습니다
조회 중에는 DB 행 잠금을 유지하지 않습니다
PostgreSQL advisory lock으로 중복 실행을 막고 Redis에 확인 순서를 저장해 오래된 오류 기록이 뒤의 기록을 막지 않게 합니다
조회 한 건이 실패해도 다음 주기에 다시 시도하며 다른 기록의 갱신은 유지합니다
새 테이블이나 마이그레이션은 필요하지 않습니다

## 운영 VM 적용

수정된 백엔드 이미지를 배포한 뒤 이 저장소의 deploy 디렉터리에서 한 번 설치합니다
기존 자동 배포는 API 컨테이너만 교체하므로 최초 timer 등록이 필요합니다
등록 이후에는 배포된 backend 컨테이너의 명령을 실행하므로 이미지가 바뀌어도 같은 timer를 사용합니다

    sudo python3 install_instance_reconciler.py --check
    sudo python3 install_instance_reconciler.py
    systemctl status msg-instance-reconcile.timer
    journalctl -u msg-instance-reconcile.service

timer는 부팅 30초 뒤 시작하고, 이전 실행이 끝난 뒤 15초 간격으로 실행합니다
동기화 오류가 있으면 종료 코드 1과 건수를 남기며 다음 주기에 다시 실행합니다
명령이나 DB 준비가 안 된 구형 배포에는 설치하지 않습니다

한 번만 실행하거나 특정 시험 인스턴스를 확인할 수도 있습니다

    python manage.py reconcile_instances
    python manage.py reconcile_instances --interval 15
    python manage.py reconcile_instances --instance-id INSTANCE_UUID

이미 종료된 과거 이력의 사유가 비어 있다면 해당 instance-id를 지정해 보충할 수 있습니다
기본 주기 작업은 이미 종료된 이력을 반복 조회하지 않습니다

중지할 때는 sudo systemctl disable --now msg-instance-reconcile.timer를 실행합니다
