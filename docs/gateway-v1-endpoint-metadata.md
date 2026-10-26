# Gateway v1 endpoint 전달

추적: [Backend #147](https://github.com/MSG-CTF/msg-backend/issues/147),
[Runtime 설계 #74](https://github.com/MSG-CTF/secure-provisioner/pull/74),
[Runtime 구현 #73](https://github.com/MSG-CTF/secure-provisioner/issues/73).

Gateway의 v1 계약은 보조 컨테이너를 유지하고 공개 endpoint 한 개를 NodePort upstream에
연결한다. Backend의 기존 Scheduler 호출 버전이나 기존 endpoint 배열 계약은 변경하지 않는다.
실제 Gateway 등록, TLS 설정, NodePort 접근 정책과 운영 배포는 Runtime/Gateway 작업 범위다.

## Backend 동작

`normalize_scheduler_endpoints()`는 Scheduler의 기존 필수 필드를 계속 검증한다.
추가 연결 필드 네 개만 허용 목록으로 보존한다. `Instance.endpoints`와
`DeploymentTest.endpoints`는 기존 JSONField를 사용하므로 migration이 필요 없다.
참가자 생성·상태 갱신·조회와 관리자 배포시험 응답이 같은 정규화를 사용한다.
release artifact poller는 endpoint를 소비하지 않는다.

| 필드 | 의미 | null이 아닌 값의 검증 |
| --- | --- | --- |
| `container_name` | 기존 컨테이너 이름 | 비어 있지 않은 문자열, 필수 |
| `port` | 기존 컨테이너 내부 포트 | boolean을 제외한 정수 1–65535, 필수 |
| `protocol` | 애플리케이션 프로토콜 | `HTTP` 또는 `TCP`, 필수 |
| `service_url` | 참가자에게 전달할 공개 주소 | 비어 있지 않은 문자열, 필수 |
| `hostname` | 공개 연결 hostname | 비어 있지 않은 문자열, 선택 |
| `connection_port` | 공개 연결 포트 | boolean을 제외한 정수 1–65535, 선택 |
| `tls` | 공개 연결의 TLS 사용 여부 | boolean, 선택 |
| `server_name` | TLS SNI 이름 | 비어 있지 않은 문자열, 선택 |

선택 필드의 누락은 누락으로, 명시적 `null`은 `null`로, `tls=false`는 `false`로
저장하고 반환한다. URL에서 기본값을 추론하거나 새로운 키를 합성하지 않는다.
추가 필드 타입이 잘못되면 기존 잘못된 Scheduler 응답과 같이 503으로 처리한다.
기존 주소의 URL scheme을 바꾸거나 endpoint 이외의 알 수 없는 필드를 저장하지 않는다.
실행 중이 아닌 인스턴스와 관리자 테스트는 기존과 같이 endpoint를 응답에서 숨긴다.

예시 TCP 공개 연결은 다음과 같다. 내부 `port=31337`은 공개 `connection_port=443`으로
대체하지 않는다. TCP 애플리케이션의 `protocol`은 TLS를 사용해도 `TCP`다.

```json
{
  "container_name": "pwn",
  "port": 31337,
  "protocol": "TCP",
  "service_url": "tls://i-example.ctf.example:443",
  "hostname": "i-example.ctf.example",
  "connection_port": 443,
  "tls": true,
  "server_name": "i-example.ctf.example"
}
```

HTTP Gateway 주소는 `https://i-example.ctf.example`처럼 전달한다. Backend는 이 주소로
접속하거나 TLS 연결을 검증하지 않는다. 기존 `host` 응답도 이전과 같이 `service_url`을
사용하며, 새 `hostname` 필드와 혼동하지 않는다.

## 클라이언트 후속 작업

Frontend 변경은 포함하지 않는다. Backend #147 및 해당 PR을 기준으로 별도 작업에서
공개 주소·연결 포트·TLS 여부·SNI를 표시하고, TCP 접속 명령이 `tls=true`일 때 TLS/SNI를
사용하도록 해야 한다. `port`를 공개 NodePort로 해석하거나 `tls://`를 일반 브라우저
링크로 표시하지 않는다. 선택 필드가 누락된 기존 endpoint는 기존 표시·연결 방식을 유지한다.
`false`나 `null`을 누락값으로 바꾸지 않고, SNI 필드가 없다는 이유로 임의 이름을 생성하지 않는다.

## 검증 범위

`apps.instances.tests_gateway_endpoints`는 HTTP/TCP 연결 필드, 누락/null/false, 잘못된 타입과
포트 범위, 참가자 생성·갱신 및 관리자 배포시험의 실제 PostgreSQL 저장과 응답 직렬화를
검사한다. Scheduler의 경계 응답은 가상 값이며 실제 서비스나 운영 토큰을 사용하지 않는다.
이 검증은 Runtime→Gateway 등록이나 실제 TLS/SNI 연결의 종단 간 검증을 대신하지 않는다.

로컬 검증은 CPython 3.12와 별도 PostgreSQL 16 테스트 DB에서 집중 8개와 전체 app 테스트
1,114개가 통과했다(전체 605.318초). Django check, migration drift 검사와 `git diff --check`도
통과했다. 테스트 전용 설정과 DB는 운영 설정 및 Runtime 부하 테스트 DB와 분리했다.
