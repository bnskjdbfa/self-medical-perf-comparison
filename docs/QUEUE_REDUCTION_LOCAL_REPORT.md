# 요청 큐 병목 후속 변경과 로컬 검증 — 2026-10-20

## 확인된 원인과 남은 불확실성

실제 Render 2×2에서 헬스체크 큐 대기 최대16.687초, 5초 초과9건을 확인했다.
CSS 처리 자체는 약1~2.5ms였다. 일반 요청도 같은 큐에서 수초 대기했다.
성공 identity POST19건의 앱 중앙값2721.850ms, SQL 누적 중앙값2402.464ms로
DB 실행/통신 대기가 슬롯 점유의 큰 부분이다. SQL 누적은 순수 DB CPU 또는 순수 네트워크 RTT가 아니다.
CPU/메모리 그래프는 고갈 증거가 없었다. 첫 실패 실행은 health timeout Instance failed와
실제 worker 종료가 관측됐으나 진단 실행은 40×502에도 종료 이벤트/로그가 없었다.
따라서 진단 실행의 502 발생 주체와 Render 라우팅 중단 여부는 아직 미확정이다.

## 변경

- config/session_backend.py: Django와 동일한 CSPRNG/32문자/36자 alphabet으로 후보를 생성한다.
  사전 exists 조회를 생략한다. 기존 원자적 INSERT/고유 제약/credential 등록/가족 세대 전환을 유지하고
  충돌 시 전체 트랜잭션 롤백과409/no cookie change를 유지한다. 충돌 시 타 세션 덮어쓰기/키 재사용 없음.
- config/session_middleware.py: 세션 관련 쿠키가 전혀 없으면 DB 조회용 트랜잭션을 생략한다.
  scope/token prefix가 있으면 malformed 입력도 기존 제한된 조회 경로를 유지한다.
  CSRF 쿠키만 있는 요청도 무세션 경로를 쓴다. 새 세션 저장은 기존 bounded_atomic으로 수행한다.
- 인증·본인확인·동의·접근 제한·URL·관리자·프론트·모델·migration은 변경하지 않았다.
- tests/load/server_profile.py: local-gthread-2x4 후보만 추가. 배포 설정 변경 없음.
  로컬 PG 회귀 실행기에 키 충돌/SQL 회귀 모듈을 추가했다.

## 정확한 비교 기준

이전 빌드24680c4는 이미 세션 INSERT 최적화가 적용된 빌드다. 현재 실제 진단 스테이징236f24d는
c89c79e 기반31SQL 빌드다. 이번 로컬26→25SQL 비교를 실제 진단 스테이징의 전후로 혼동하지 않는다.
독립 로컬 PostgreSQL17.6/max_connections20, LocMem, localhost, Python3.14.3/Gunicorn23.0.0.
계측/fixture를 동일하게 사용하고 성능 실행은 순차 진행했다. 다른 회귀와 겹친 중간 측정은 제외했다.
새 identity 전체 POST(CSRF 강제)26→25SQL, session_saves1 유지.
로컬 flow tool 최초 code GET은7→5SQL. SET LOCAL/키 사전 조회 제거 외 commit도 생략되지만
SQL wrapper 밖 commit 시간까지 SQL 감소 수에 포함하지 않았다.

## 로컬 성능 결과

단위ms. 각 후보1회이며 시간대/머신 변동이 있다. 통계적 유의성 또는 Render 개선을 주장하지 않는다.
flow의 시간은 순차 HTTP/redirect 합계로 사용자 생각 시간/브라우저 자원 로딩을 포함하지 않는다.

| 구성 | 단일10명 순차 흐름 p50/p95 | 42 동시성 흐름 p50/p95 | 전체126흐름 벽시계 |
|---|---:|---:|---:|
| 이전24680c4 2×2 |384.380 /430.316 |2308.626 /2699.465 |7.250초|
| 최종 변경 2×2 |367.172 /439.784 |2250.680 /2756.174 |7.090초|
| 최종 변경 로컬2×4 |252.674 /306.091 |1935.197 /2162.162 |5.991초|

concurrent42 모드는42개의 동시 슬롯으로 서로 다른126명의 흐름을 실행한다.42명 한 배치가 아니다.
각 실행은 warmup3+single10+126으로 잠긴 제출139건을 검증했다. 전 후보 flow HTTP오류0.
10ms 주기 observer의 lock wait sample0은 짧은 경합까지 없었다는 증명이 아니다.
2×2 p95는 오히려 소폭 증가했다. SQL 감소만 확인됐고 전체 체감 개선은 아직 입증되지 않았다.
2×4는 CPU형 작업/지역 간 RTT/풀러 연결 상한이 다른 Render에 그대로 외삽할 수 없다.
최대 지속 DB연결도 일반적으로4→8로 늘 수 있어 풀러/프로젝트 한도와 다른 작업 연결 예산 확인이 필요하다.

## 회귀

- 최종 PostgreSQL 인증·동의·SQL·충돌 회귀113개 통과.
- 2×2 워커별 스레드[2,2], 코드/identity 경쟁40회씩, 동시 세션 증가60회 검증.
- 로그아웃/초기화/지연 쿠키/세대 전환 순서7경로 통과.
- 로컬 정상42명/사용자별3중복: 최종42명 인증 유지, 동의126/설문42건 저장.
  중복 모드는 재시도한다. raw503:code14,identity16,documents_finalize21,health_submit2;
  raw409:identity84,documents_finalize13. 원시 오류율0으로 보고하지 않는다.
- 느린 네트워크/클릭·Enter/실패 후 재시도/pageshow 브라우저 회귀 통과.
- timeout/DB오류/연결 고갈 시 쿠키 보존·후속 인증 회복, 남은 worker transaction0.
- 전체 SQLite413개:408통과, PostgreSQL 전용5개 skip. 이 skip은 PG113 회귀와 구분한다.
- 개인정보·SQL 원문·세션 원문을 보고서에 저장하지 않았다.

## 실제 적용 전 남은 조건

실제 Render/Supabase 최종 빌드42명 정상·중복, health queue/502, CSRF·쿠키·인증 유지·사후 DB 집계는 미실행.
현재 실패 자료/DB는 보존하고 외부 부하는 실행하지 않았다.2×4는 로컬 후보일 뿐 배포 권고 아님.
실제 테스트 전 테스트 DB대상/빌드/worker 초기상태/fixture를 재확인한다.
운영 배포/운영 설정 변경/운영 DB 조회·부하는 하지 않았다.
