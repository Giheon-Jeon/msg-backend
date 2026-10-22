# 관리자 노드별 문제 예약 조회

GET /api/v1/admin/resources는 기존 관리자 인증을 유지하면서 accounts[].nodes[].resource_allocation을 전달한다
브로커 GET /v1/inventory/resource-targets에 include_allocations=true를 추가하고 기존 500개 페이지 조회를 유지한다

- 예약의 기준은 브로커 원장이며 로컬 Instance 상태만으로 차감하거나 해제하지 않음
- 문제명, 팀명, 인스턴스 상태는 UUID를 모아 일괄 조회하며 노드 1,000개에서도 최대 네 번 조회
- 인스턴스의 팀·문제 UUID가 예약과 일치할 때만 로컬 상태를 사용
- 관리자 배포 테스트는 요청자에서 파생한 테스트 팀 UUID를 검증한 뒤 관리자 배포 테스트로 표시
- 누락된 로컬 인스턴스나 과거 식별자에도 예약 수치를 유지
- 이전 브로커가 선택 필드를 반환하지 않으면 배치 미수집 상태를 보존
- 응답은 no-store이며 프론트는 보이는 탭에서 5초 간격으로 조회

브로커 resource_allocation.allocatable은 Agent의 node_allocatable 원본이다
기존 allocatable_capacity와 remaining_capacity는 실제 사용과 실행 요청을 차감한 스케줄링 가용량이므로 예약 비율 계산에 사용하지 않는다
브로커 migration 20261022_0001과 앱 배포 후 첫 관측 전에는 총용량이 null일 수 있으며 예약량은 그대로 전달한다

배포 순서는 브로커 migration·앱 → 백엔드 → 프론트이며 백엔드 DB migration은 추가하지 않는다
검증은 AdminResourcesTests와 NodeAllocationTests에서 인증, 페이지 조회, 캐시 차단, 상태 정합성, 누락된 식별자, 조회 횟수를 포함한다
