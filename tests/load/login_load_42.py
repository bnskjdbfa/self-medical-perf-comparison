"""42 synthetic participants, one team, duplicate login/doc/health submissions."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html.parser import HTMLParser
import json
import math
import threading
import time

from django.conf import settings
from django.utils import timezone


class HiddenInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href', '').startswith('/surveys/day/'):
            self.links.append(attrs['href'])
        if tag == 'input' and attrs.get('name') in {'survey_form_id', 'survey_form_version', 'survey_form_signature'}:
            self.values[attrs['name']] = attrs.get('value', '')


def run(request):
    assert settings.APP_ENV == 'test'
    assert str(settings.DATABASES['default']['NAME']).startswith('login_predeploy_')
    from teams.models import Team, Participant, Respondent
    from surveys.models import TeamFeatureSetting, SurveyForm, SurveySection, SurveyQuestion, SurveySubmission, DocumentSubmission
    from surveys.services import normalize_name
    team = Team.objects.create(name='42 synthetic participants', code='LOGIN-42',
                               organization_name='Fixture', organization_name_en='Fixture',
                               start_date=timezone.localdate(), max_day_no=3)
    TeamFeatureSetting.objects.create(team=team, enable_privacy_consent=True,
                                      enable_third_party_consent=True, enable_safety_pledge=True)
    names = [f'Load Fixture {index:02d}' for index in range(42)]
    Participant.objects.bulk_create([
        Participant(team=team, name=name, normalized_name=normalize_name(name),
                    birth_date=date(1990, 1, 1), gender='M', is_active=True) for name in names
    ])
    form = SurveyForm.objects.create(team=team, day_no=1, title='Fixture', version=1)
    section = SurveySection.objects.create(form=form, title='Fixture', section_type='physical')
    question = SurveyQuestion.objects.create(section=section, key='fixture_question', label='Fixture?',
                                             question_type='single', options_json=['yes', 'no'], is_required=True)
    metrics = defaultdict(list)
    lock = threading.Lock()

    def timed(phase, path, cookies=None, payload=None, **extra):
        begin = time.monotonic()
        response = request(path, cookies, payload, **extra)
        with lock:
            metrics[phase].append((response[0], time.monotonic() - begin))
        return response

    def retry(phase, path, cookies=None, payload=None, *, conflicts=False, **extra):
        for attempt in range(12):
            response = timed(phase, path, cookies, payload, **extra)
            if response[0] != 503 and not (conflicts and response[0] == 409):
                return response
            time.sleep(min(0.01 * (attempt + 1), 0.1))
        return response

    states = [{} for _ in names]
    code = '/surveys/preview/v2/code/'
    identity = '/surveys/preview/v2/identity/'
    privacy = '/surveys/preview/v2/privacy-consent/'
    doc = '/surveys/preview/v2/documents/third_party_consent/'
    home = '/surveys/preview/v2/home/'
    with ThreadPoolExecutor(max_workers=42) as pool:
        # Separate the ordinary 42-person login wave from the deliberately
        # overlapping per-person duplicates. Report both, never hide retries.
        def single(index):
            state = timed('single_bootstrap', code)[2]
            response = timed('single_code', code, state, {'team_code': team.code})
            assert response[0] == 302
            state.update(response[2])
            response = timed('single_privacy', privacy, state, {'agreed': 'on'})
            assert response[0] == 302
            state.update(response[2])
            response = timed('single_identity', identity, state, {
                'name': names[index], 'birth_date': '900101', 'gender': 'M',
            })
            assert response[0] == 302, ('single login status', response[0])
            state.update(response[2])
            return state
        single_begin = time.monotonic()
        singles = list(pool.map(single, range(42)))
        single_wall = time.monotonic() - single_begin
        ids = dict(Respondent.objects.filter(team=team).values_list('normalized_name', 'id'))
        proofs = list(pool.map(lambda index: timed('single_retention', '/_test/session-proof/', singles[index], {},
            headers_extra={'X-Test-Expected-Respondent': str(ids[normalize_name(names[index])])}), range(42)))
        assert all(response[0] == 200 and json.loads(response[3])['matches'] for response in proofs)
        pages = list(pool.map(lambda _: retry('bootstrap', code), names))
        assert all(response[0] == 200 for response in pages)
        for state, response in zip(states, pages):
            state.update(response[2])
        jobs = [(index, repeat) for index in range(42) for repeat in range(3)]
        codes = list(pool.map(lambda job: retry('code', code, states[job[0]], {'team_code': team.code}), jobs))
        assert all(response[0] == 302 for response in codes), '42-user code confirmation'
        for job, response in zip(jobs, codes):
            states[job[0]].update(response[2])
        consents = list(pool.map(lambda index: retry('privacy', privacy, states[index], {'agreed': 'on'}), range(42)))
        assert all(response[0] == 302 for response in consents), '42-user privacy consent'
        for state, response in zip(states, consents):
            state.update(response[2])
        before = [dict(state) for state in states]
        begin = time.monotonic()
        identities = list(pool.map(lambda job: retry('identity', identity, before[job[0]], {
            'name': names[job[0]], 'birth_date': '900101', 'gender': 'M',
        }), jobs))
        login_wall = time.monotonic() - begin
        for index in range(42):
            responses = [response for job, response in zip(jobs, identities) if job[0] == index]
            assert sorted(response[0] for response in responses) == [302, 409, 409], '42-user duplicate identity'
            states[index].update(next(response[2] for response in responses if response[0] == 302))
        respondent_ids = dict(Respondent.objects.filter(team=team).values_list('normalized_name', 'id'))
        assert len(respondent_ids) == 42

        def retained(index, phase):
            expected = respondent_ids[normalize_name(names[index])]
            response = retry(phase, '/_test/session-proof/', states[index], {},
                             headers_extra={'X-Test-Expected-Respondent': str(expected)})
            return response[0] == 200 and json.loads(response[3])['matches']

        assert all(pool.map(lambda i: retained(i, 'retention_after_login'), range(42)))
        gated = list(pool.map(lambda i: retry('required_gate', home, states[i]), range(42)))
        assert all(response[0] == 302 and '/documents/' in response[1]['Location'] for response in gated)
        pages = list(pool.map(lambda i: retry('document_page', doc, states[i]), range(42)))
        assert all(response[0] == 200 for response in pages)
        third = list(pool.map(lambda i: retry('document_record', doc, states[i], {
            'action': 'record_consent', 'document_type': 'third_party_consent', 'agreed': 'on',
        }), range(42)))
        assert all(response[0] == 200 for response in third)
        finals = list(pool.map(lambda job: retry('document_finalize', doc, states[job[0]], {
            'action': 'finalize', 'document_type': 'safety_pledge', 'agreed': 'on',
        }, conflicts=True), jobs))
        assert all(response[0] in (200, 302) for response in finals), ('42-user duplicate document finalization', dict(Counter(r[0] for r in finals)))
        assert DocumentSubmission.objects.filter(team=team).count() == 42 * 3
        paths = list(pool.map(lambda i: retry('health_start', '/surveys/preview/v2/health/start/', states[i]), range(42)))
        assert all(response[0] == 200 for response in paths), '42-user health start page'
        day_paths = []
        for response in paths:
            parser = HiddenInputs()
            parser.feed(response[3].decode())
            assert parser.links
            day_paths.append(parser.links[0])
        surveys = list(pool.map(lambda i: retry('health_page', day_paths[i], states[i]), range(42)))
        payloads = []
        for response in surveys:
            assert response[0] == 200
            parser = HiddenInputs()
            parser.feed(response[3].decode())
            payloads.append({**parser.values, f'q_{question.pk}': 'yes', 'submit': '1',
                             'answers_payload': json.dumps({str(question.pk): 'yes'})})
        submitted = list(pool.map(lambda job: retry('health_submit', day_paths[job[0]],
                                                    states[job[0]], payloads[job[0]]), jobs))
        assert all(response[0] == 302 and '/completed/health/' in response[1]['Location'] for response in submitted), '42-user duplicate health submit'
        assert SurveySubmission.objects.filter(team=team).count() == 42
        assert not SurveySubmission.objects.filter(team=team, is_locked=False).exists()
        assert all(pool.map(lambda i: retained(i, 'retention_after_submission'), range(42)))
        homes = list(pool.map(lambda i: retry('home_after_submission', home, states[i]), range(42)))
        assert all(response[0] == 200 for response in homes)

    summary = {}
    for phase, values in metrics.items():
        durations = sorted(seconds for status, seconds in values)
        summary[phase] = {
            'requests': len(values), 'statuses': dict(Counter(str(status) for status, _ in values)),
            'p50_ms': round(durations[len(durations) // 2] * 1000, 1),
            'p95_ms': round(durations[math.ceil(len(durations) * 0.95) - 1] * 1000, 1),
            'max_ms': round(max(durations) * 1000, 1),
            'raw_5xx_rate': round(sum(status >= 500 for status, _ in values) / len(values), 4),
        }
    report = {'users': 42, 'duplicates_per_user': 3, 'logical_failures': 0,
              'single_login_wall_seconds': round(single_wall, 3), 'single_login_retained_users': 42,
              'login_wall_seconds': round(login_wall, 3), 'retained_users': 42,
              'document_rows': 126, 'health_submission_rows': 42, 'phases': summary, 'status': 'pass'}
    print('LOAD42 ' + json.dumps(report, sort_keys=True))
    return report
