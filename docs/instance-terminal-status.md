# 문제 화면의 마지막 인스턴스 상태

GET /api/v1/teams/me/instances에 challenge_id를 보내면 활성 인스턴스가 없을 때 해당 문제의 마지막 본인 이력을 반환합니다
같은 팀의 다른 사용자나 이전 소속 팀의 이력은 포함하지 않습니다
challenge_id를 생략하면 기존처럼 활성 인스턴스 또는 null을 반환합니다
다른 문제의 활성 인스턴스가 있으면 그 인스턴스를 먼저 반환해 기존 전환 안내를 유지합니다

마지막 이력에는 replaced_instance_id와 delete_reason을 포함합니다
FAILED이며 replaced_instance_id가 있고 delete_reason이 TTL_EXPIRED이면 초기화 중 사용 시간이 만료된 경우입니다
사유가 null이면 프론트는 원인을 추측하지 않고 일반 실패 안내를 표시합니다
Scheduler가 TTL_EXPIRED를 저장하는 변경이 먼저 배포돼야 만료 원인을 구분할 수 있습니다

새 요청이 STOPPING인 동안 이전 FAILED 이력을 다시 보여주지 않도록 마지막 이력은 상태에 관계없이 생성 시각순으로 선택합니다
RUNNING이 아닌 응답에서는 기존 접속 주소와 포트를 숨깁니다
