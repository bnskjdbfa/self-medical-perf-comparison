"""Local PostgreSQL only; reproducible actual HTTP flow, no retries or production DSN.

Use --root for a git archive snapshot, same interpreter/server/instrumentation.
Raw credentials stay in memory or a temporary browser input deleted on exit.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, HTTPRedirectHandler, build_opener
from http.cookies import SimpleCookie
import uuid
from server_profile import arguments as server_arguments, profile

SUPPORT = Path(__file__).resolve().parent
PYTHON = Path(sys.executable)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html.decode())
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') in {'survey_form_id', 'survey_form_version', 'survey_form_signature'}:
            self.values[attrs['name']] = attrs.get('value', '')


def summarize(rows):
    result = {'requests': len(rows), 'statuses': dict(Counter(str(row['status']) for row in rows)),
              'error_rate': sum(row['status'] >= 400 for row in rows) / len(rows)}
    for key in ('ttfb_ms', 'total_ms', 'server_ms', 'queries', 'db_ms', 'session_ms', 'session_saves', 'redirects'):
        values = sorted(row[key] for row in rows)
        result[key] = {'p50': round(values[len(values)//2], 3),
                       'p95': round(values[math.ceil(len(values)*.95)-1], 3)}
    result['table_queries_per_request'] = {
        key: round(sum(row['tables'].get(key, 0) for row in rows) / len(rows), 2)
        for key in sorted({key for row in rows for key in row['tables']})}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--postgres-port', type=int, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--server-profile', choices=['sync3', 'production-gthread', 'local-gthread-2x4'], default='sync3')
    args = parser.parse_args()
    root = args.root.resolve()
    import psycopg
    from psycopg import sql
    admin = psycopg.connect(host='127.0.0.1', port=args.postgres_port, user='postgres', dbname='postgres',
                           password='', sslmode='disable', connect_timeout=3, autocommit=True)
    db_name = 'login_predeploy_perf_' + uuid.uuid4().hex
    admin.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(db_name)))
    env = {**os.environ, 'APP_ENV': 'test', 'DB_ENGINE': 'django.db.backends.postgresql',
           'DB_HOST': '127.0.0.1', 'DB_PORT': str(args.postgres_port), 'DB_NAME': db_name,
           'DB_USER': 'postgres', 'DB_PASSWORD': '', 'DB_SSLMODE': 'disable', 'DB_IS_PRODUCTION': 'false',
           'ALLOW_DATABASE_ENV_MISMATCH': 'false', 'DJANGO_SETTINGS_MODULE': 'config.settings',
           'DJANGO_SECRET_KEY': 'isolated-flow-performance-not-deployment-key', 'FLOW_PERF_TEST': '1',
           'DJANGO_DEBUG': 'false', 'DJANGO_ALLOWED_HOSTS': '127.0.0.1,localhost',
           'DJANGO_SESSION_COOKIE_SECURE': 'false', 'DJANGO_CSRF_COOKIE_SECURE': 'false',
           'DJANGO_SECURE_SSL_REDIRECT': 'false', 'ADMIN_2FA_REQUIRED': 'false',
           'ENABLE_PERF_LOG': 'false', 'PYTHONPATH': str(root) + os.pathsep + str(SUPPORT)}
    env['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ.update(env)
    sys.path.insert(0, str(root))
    sys.argv.append('test')
    import django
    django.setup()
    from django.core.management import call_command
    from django.db import connections
    from django.utils import timezone
    from teams.models import Team, Participant
    from surveys.models import TeamFeatureSetting, SurveyForm, SurveySection, SurveyQuestion, SurveySubmission
    from surveys.services import normalize_name
    server = None
    stop = threading.Event()
    waits = Counter()
    def observe():
        with psycopg.connect(host='127.0.0.1', port=args.postgres_port, user='postgres', dbname=db_name,
                             password='', sslmode='disable', autocommit=True) as observer:
            while not stop.wait(.01):
                for route, kind, event, count in observer.execute(
                    "SELECT split_part(application_name, ':', 2), wait_event_type, wait_event, count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock' "
                    "GROUP BY 1,2,3").fetchall():
                    waits[f'{route} {kind}:{event}'] += count
    try:
        call_command('show_db_target')
        call_command('migrate', verbosity=0)
        team = Team.objects.create(name='Synthetic performance team', code='PERF-FIXTURE',
                                   organization_name='Fixture', organization_name_en='Fixture',
                                   start_date=timezone.localdate(), max_day_no=3)
        TeamFeatureSetting.objects.create(team=team, enable_privacy_consent=True,
                                          enable_third_party_consent=True, enable_safety_pledge=True)
        form = SurveyForm.objects.create(team=team, day_no=1, title='Performance fixture', version=1)
        section = SurveySection.objects.create(form=form, title='Fixture', section_type='physical')
        questions = [SurveyQuestion.objects.create(section=section, key=f'q{i}', label=f'Fixture {i}',
                     question_type=SurveyQuestion.QuestionType.SINGLE, options_json=['yes', 'no'], is_required=True) for i in range(20)]
        indices = [*range(3), *range(100,110), *range(1000,1126), *range(5000,5003)]
        Participant.objects.bulk_create([Participant(team=team, name=f'Performance Fixture {index:04d}',
            normalized_name=normalize_name(f'Performance Fixture {index:04d}'),
            birth_date=date(1990,1,1), gender='M', is_active=True) for index in indices])
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        with tempfile.TemporaryDirectory(prefix='flow-perf-') as tmp:
            with open(Path(tmp)/'server.log', 'w') as log:
                server = subprocess.Popen([str(PYTHON), '-m', 'gunicorn', 'flow_perf_wsgi:application',
                    '--chdir', str(root), '--bind', f'127.0.0.1:{port}', *server_arguments(args.server_profile)],
                    cwd=root, env=env, stdout=log, stderr=log)
                opener = build_opener(NoRedirect)
                def send(path, cookies, payload=None):
                    headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in cookies.items())}
                    if payload is not None:
                        payload = {**payload, 'csrfmiddlewaretoken': cookies.get('csrftoken', '')}
                    start = time.perf_counter()
                    req = Request(urljoin(base, path), data=urlencode(payload).encode() if payload is not None else None, headers=headers)
                    try:
                        response = opener.open(req, timeout=30)
                    except HTTPError as exc:
                        response = exc
                    first = time.perf_counter()
                    body = response.read()
                    end = time.perf_counter()
                    for header in response.headers.get_all('Set-Cookie', []):
                        parsed = SimpleCookie(header)
                        for name, value in parsed.items():
                            if value['max-age'] == '0':
                                cookies.pop(name, None)
                            else:
                                cookies[name] = value.value
                    stats = {'status': response.code, 'ttfb_ms': (first-start)*1000,
                             'total_ms': (end-start)*1000, 'redirects': int(300 <= response.code < 400)}
                    for key in ('server_ms', 'queries', 'db_ms', 'session_ms', 'session_saves'):
                        stats[key] = float(response.headers.get('X-Perf-' + key.replace('_', '-'), 0))
                    stats['tables'] = {key: int(value) for pair in response.headers.get('X-Perf-Tables', '').split(',') if pair
                                       for key, value in [pair.split(':')]}
                    return stats, body, response.headers.get('Location')
                for _ in range(100):
                    try:
                        send('/surveys/preview/v2/code/', {})
                        break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError('Isolated performance server exited; inspect local test log')
                        time.sleep(.05)
                observer = threading.Thread(target=observe, daemon=True)
                observer.start()
                def flow(index):
                    name = f'Performance Fixture {index:04d}'
                    cookies, rows = {}, defaultdict(list)
                    prefix = '/surveys/preview/v2/'
                    def hit(phase, path, payload=None, follow=True):
                        stats, body, location = send(path, cookies, payload)
                        rows[phase].append(stats)
                        assert stats['status'] < 400, (phase, stats['status'])
                        count = 0
                        while follow and location:
                            stats, body, location = send(location, cookies)
                            rows[phase].append(stats)
                            assert stats['status'] < 400, (phase, stats['status'])
                            count += 1
                            assert count < 8, 'redirect loop'
                        return body
                    hit('code_get', prefix+'code/')
                    hit('code_post', prefix+'code/', {'team_code': team.code})
                    hit('privacy_post', prefix+'privacy-consent/', {'agreed': 'on'})
                    hit('identity_get', prefix+'identity/')
                    hit('identity_post', prefix+'identity/', {'name':name, 'birth_date':'900101', 'gender':'M'})
                    doc = prefix+'documents/third_party_consent/'
                    hit('documents_get', doc)
                    hit('documents_record', doc, {'action':'record_consent','document_type':'third_party_consent','agreed':'on'}, False)
                    hit('documents_finalize', doc, {'action':'finalize','document_type':'safety_pledge','agreed':'on'})
                    hit('home', prefix+'home/')
                    hit('home_repeat', prefix+'home/?lang=ko')
                    hit('home_repeat', prefix+'home/?lang=ko')
                    hit('survey_start', prefix+'health/start/')
                    day = '/surveys/day/1/?source=v2'
                    body = hit('survey', day)
                    values = Inputs(body).values
                    answers = {str(q.pk): 'yes' for q in questions}
                    hit('survey_submit', day, {**values, **{f'q_{q.pk}':'yes' for q in questions},
                                              'submit':'1', 'answers_payload':json.dumps(answers)})
                    hit('result', '/surveys/day/1/result/?source=user&return_to='+prefix+'home/')
                    retained = hit('retention', prefix+'home/')
                    assert name.encode() in retained and b'data-card=' in retained, 'authentication retention failed'
                    return rows
                output = {'label': args.label, 'workers': profile(args.server_profile)['workers'],
                          'server_profile': profile(args.server_profile), 'postgres_version': admin.info.server_version,
                          'max_connections': int(admin.execute('SHOW max_connections').fetchone()[0]),
                          'fixture_questions': 20, 'modes': {}}
                # Warm each worker/cache before measurements. Fresh identities for every flow.
                for i in range(3):
                    flow(i)
                for mode, count, width, start_index in [('single',10,1,100), ('concurrent42',126,42,1000)]:
                    started = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=width) as pool:
                        values = list(pool.map(flow, range(start_index,start_index+count)))
                    phases = defaultdict(list)
                    transitions = defaultdict(list)
                    for value in values:
                        for phase, requests in value.items():
                            phases[phase].extend(requests)
                            transitions[phase].append({'total_ms': sum(r['total_ms'] for r in requests),
                                                       'redirects':sum(r['redirects'] for r in requests)})
                    output['modes'][mode] = {'users':count,'concurrency':width,'wall_seconds':round(time.perf_counter()-started,3),
                        'journey_ms': {'p50': round(sorted(sum(r['total_ms'] for requests in value.values() for r in requests) for value in values)[count//2],3),
                                       'p95': round(sorted(sum(r['total_ms'] for requests in value.values() for r in requests) for value in values)[math.ceil(count*.95)-1],3)},
                        'phases':{phase:summarize(rows) for phase, rows in phases.items()},
                        'transitions':{phase:{'p50_ms':round(sorted(r['total_ms'] for r in rows)[len(rows)//2],3),
                                             'p95_ms':round(sorted(r['total_ms'] for r in rows)[math.ceil(len(rows)*.95)-1],3),
                                             'max_redirects':max(r['redirects'] for r in rows)} for phase, rows in transitions.items()}}
                if args.browser:
                    source = Path(tmp)/'browser-input.json'
                    source.write_text(json.dumps({'base':base,'team_code':team.code,
                                                  'questions':[q.pk for q in questions]}))
                    browser_out = Path(tmp)/'browser-results.json'
                    subprocess.run(['node',str(SUPPORT/'flow_perf_browser.cjs'),str(source),str(browser_out)],check=True,env=env)
                    output['browser'] = json.loads(browser_out.read_text())
                output['stored_submissions'] = SurveySubmission.objects.filter(team=team).count()
                assert output['stored_submissions'] == 139 + (3 if args.browser else 0)
                stop.set()
                observer.join(timeout=3)
                output['lock_samples_10ms'] = dict(waits)
                args.report.write_text(json.dumps(output,indent=2)+'\n')
                print(json.dumps({'label':args.label,'status':'pass','report':str(args.report)}))
    finally:
        stop.set()
        if server:
            server.terminate()
            server.wait(timeout=10)
        connections.close_all()
        admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(db_name)))
        admin.close()


if __name__ == '__main__':
    main()
