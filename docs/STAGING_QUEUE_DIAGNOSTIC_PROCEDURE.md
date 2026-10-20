# 스테이징 요청 대기 계측

기준 A 빌드 c89c79eed32b83c3b98ff9fd7c470aa1f5f7f295 위에 계측 파일만 추가한다.
현재 운영 및 스테이징 설정/데이터는 아직 변경하지 않았다. 기존 실패 결과를 덮어쓰지 않는다.

## 측정 범위와 한계

- `queue_ms`: Gunicorn enqueue_req에서 스레드 풀에 submit하기 직전부터 해당 작업 실행 시작까지.
- `worker_handle_ms`: 실행 시작부터 Gunicorn handle 종료까지. HTTP 파싱/WSGI/응답 쓰기가 포함된다.
- `start_epoch_ms`: worker 작업 실행 시작 UTC epoch ms. 접수 시각은 여기서 queue_ms를 뺀 근사값.
- 소켓 accept 전, 프록시 대기, 네트워크 왕복은 측정하지 않는다. 앱 계측과 단순 합산하지 않는다.
- health_static 경로도 같은 worker 큐로 처리한다. 우선순위/동시성/timeout/인증/세션 로직을 바꾸지 않는다.
- allowlist route, GET/POST/HEAD/other, 고정 build, 랜덤 request_id, PID, 숫자 시간/status만 기록한다.
  원문 URL/쿼리 문자열/쿠키/토큰/IP/사용자 데이터/SQL/예외 메시지는 수집하지 않는다.
- 응답의 X-Queue-Request-ID로 서버 로그와 연결한다. Server-Timing worker_queue는 인증 정보가 없는 숫자이다.
- Gunicorn 23.0.0 구현 기준. Render 설치 버전 확인 후 적용한다. worker override가 upstream enqueue_req를
  복제하므로 버전이 다르면 해당 버전 소스를 검토하고 테스트한 뒤 사용한다.
- 기존 안전한 staging WSGI entrypoint가 staging/비운영 PostgreSQL/토큰 설정을 검증한다.
- 로컬 단위 테스트는 큐 시각, 예외 정리, future 추적, 비대상 제외, 환경 차단, WSGI 헤더를 검증한다.
  실제 Render 큐 지연 재현 및 42명 검증은 아직 미실행이다.

## 게시와 적용 순서

1. 별도 로컬 비교 저장소 브랜치 perf-before-queue-diag를 검토/게시한다. 운영 저장소는 변경하지 않는다.
2. 현재 실패 보고서/DB 집계/Render 이벤트를 보존한다. 배포는 worker/cache 상태를 바꾸므로
   기존 실패 상태의 추가 조사가 끝난 후에만 진행한다.
3. 스테이징 Shell에서 `python -c "import gunicorn; print(gunicorn.__version__)"`로 버전 확인.
4. 별도 Render 비교 서비스에서만 진단 브랜치를 선택. Start Command의 모듈과 worker-class만 아래처럼 교체.
   나머지 사양/환경/2워커/2스레드/timeout60/keepalive5/maxrequests/jitter는 유지.

```text
gunicorn staging_queue_wsgi:application --bind 0.0.0.0:$PORT --workers 2 --worker-class staging_queue_worker.StagingQueueWorker --threads 2 --timeout 60 --keep-alive 5 --max-requests 2000 --max-requests-jitter 500 --access-logfile - --error-logfile - --access-logformat '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'
```

5. 새 commit/build 확인 및 health_static 이벤트와 응답 헤더를 단일 요청으로 먼저 검증.
6. 이후 별도 안내로 SAFE/테스트 프로젝트 확인→초기화→42명 실행을 진행한다. 기존 데이터 그대로 재실행 금지.
   새 결과 파일명/build를 사용. 외부 health probe와 부하 요청 모두 UTC 시간·queue ID를 보존해야 한다.
7. health queue가 5000ms 이상인지, app/query span과 차이, 이벤트/502 시각을 대조한다.
   새로운 부하 없이 코드 검토만으로 실제 원인 확정이나 개선 통과를 선언하지 않는다.

롤백은 스테이징에만 이전 브랜치와 staging_identity_diagnostic_wsgi:application,
worker-class gthread를 복원한다. 운영 변경/운영 DB 부하/운영 배포는 금지한다.
