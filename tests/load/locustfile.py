from __future__ import annotations

import csv
import itertools
import os
import random
import re
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from locust import HttpUser, between, events, task
from locust.exception import StopUser


LOGIN_PATH = '/surveys/prototype/login/'
RESPONDENT_PATH = '/surveys/respondent/'
STATUS_PATH = '/surveys/status/'
RESULT_PATH_RE = re.compile(r'^/surveys/day/\d+/result/$')
DETAIL_PATH_RE = re.compile(r'^/surveys/day/\d+/result/detail/$')
DAY_PATH_RE = re.compile(r'^/surveys/day/\d+/$')
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class TestIdentity:
    name: str
    birth_date: str
    phone: str
    gender: str = ''


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self.links: list[str] = []
        self._form: dict | None = None
        self._select: dict | None = None
        self._textarea: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = dict(attrs)
        if tag == 'a':
            href = attrs.get('href')
            if href:
                self.links.append(href)
            return

        if tag == 'form':
            self._form = {
                'method': attrs.get('method', 'get').lower(),
                'action': attrs.get('action', ''),
                'controls': [],
            }
            return

        if self._form is None:
            return

        if tag == 'input':
            name = attrs.get('name')
            if not name:
                return
            input_type = attrs.get('type', 'text').lower()
            if input_type in {'submit', 'button', 'image', 'file'}:
                return
            self._form['controls'].append(
                {
                    'tag': 'input',
                    'type': input_type,
                    'name': name,
                    'value': attrs.get('value', ''),
                    'checked': 'checked' in attrs,
                    'required': 'required' in attrs,
                }
            )

        if tag == 'textarea':
            name = attrs.get('name')
            if name:
                self._textarea = {
                    'tag': 'textarea',
                    'type': 'textarea',
                    'name': name,
                    'value': '',
                    'required': 'required' in attrs,
                }

        if tag == 'select':
            name = attrs.get('name')
            if name:
                self._select = {
                    'tag': 'select',
                    'type': 'select',
                    'name': name,
                    'required': 'required' in attrs,
                    'options': [],
                }

        if tag == 'option' and self._select is not None:
            self._select['options'].append(
                {
                    'value': attrs.get('value', ''),
                    'selected': 'selected' in attrs,
                    'disabled': 'disabled' in attrs,
                }
            )

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea['value'] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == 'textarea' and self._textarea is not None and self._form is not None:
            self._form['controls'].append(self._textarea)
            self._textarea = None
        if tag == 'select' and self._select is not None and self._form is not None:
            self._form['controls'].append(self._select)
            self._select = None
        if tag == 'form' and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, '').split(',') if item.strip()]


def parse_html(html: str) -> FormParser:
    parser = FormParser()
    parser.feed(html)
    return parser


def first_post_form(html: str) -> dict | None:
    for form in parse_html(html).forms:
        if form['method'] == 'post':
            return form
    return None


def final_path(response) -> str:
    parsed = urlparse(response.url)
    return parsed.path


def has_marker(response, markers: list[str]) -> bool:
    return not markers or any(marker in response.text for marker in markers)


def is_login_bounce(response) -> bool:
    return final_path(response) in {LOGIN_PATH, RESPONDENT_PATH}


def add_payload_value(payload: dict, name: str, value: str) -> None:
    if name in payload:
        current = payload[name]
        if isinstance(current, list):
            current.append(value)
        else:
            payload[name] = [current, value]
    else:
        payload[name] = value


def default_answer_for(name: str, current: str = '') -> str:
    if current:
        return current
    lowered = name.lower()
    if 'csrf' in lowered:
        return current
    return os.getenv('LOAD_TEST_TEXT_ANSWER', '부하테스트')


def build_form_payload(form: dict, overrides: dict | None = None, *, q_only: bool = False) -> dict:
    payload: dict = {}
    controls_by_name: dict[str, list[dict]] = {}
    for control in form.get('controls', []):
        name = control['name']
        if q_only and not name.startswith('q_') and control['type'] != 'hidden':
            continue
        controls_by_name.setdefault(name, []).append(control)

    for name, controls in controls_by_name.items():
        first = controls[0]
        control_type = first['type']

        if control_type == 'radio':
            selected = next((control for control in controls if control.get('checked')), controls[0])
            add_payload_value(payload, name, default_answer_for(name, selected.get('value', '')))
            continue

        if control_type == 'checkbox':
            selected_controls = [control for control in controls if control.get('checked')]
            if not selected_controls:
                selected_controls = [controls[0]]
            for control in selected_controls:
                add_payload_value(payload, name, default_answer_for(name, control.get('value', 'on')))
            continue

        if control_type == 'select':
            options = [option for option in first.get('options', []) if not option.get('disabled')]
            selected = next((option for option in options if option.get('selected')), None)
            if selected is None:
                selected = next((option for option in options if option.get('value')), options[0] if options else None)
            if selected is not None:
                add_payload_value(payload, name, default_answer_for(name, selected.get('value', '')))
            continue

        if control_type == 'hidden':
            add_payload_value(payload, name, first.get('value', ''))
        else:
            add_payload_value(payload, name, default_answer_for(name, first.get('value', '')))

    if overrides:
        payload.update(overrides)
    return payload


def load_identities() -> list[TestIdentity]:
    csv_path = os.getenv('LOAD_TEST_USERS_CSV', '').strip()
    if not csv_path:
        raise RuntimeError('LOAD_TEST_USERS_CSV is required. Use a disposable load-test team and participant CSV.')

    path = Path(csv_path)
    if not path.exists():
        raise RuntimeError(f'LOAD_TEST_USERS_CSV does not exist: {path}')

    identities: list[TestIdentity] = []
    with path.open(newline='', encoding='utf-8') as fp:
        reader = csv.DictReader(fp)
        required = {'name', 'birth_date', 'phone'}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f'LOAD_TEST_USERS_CSV is missing columns: {", ".join(sorted(missing))}')
        for row in reader:
            name = (row.get('name') or '').strip()
            birth_date = (row.get('birth_date') or '').strip()
            phone = (row.get('phone') or '').strip()
            if not name or not birth_date or not phone:
                continue
            identities.append(TestIdentity(name, birth_date, phone, (row.get('gender') or '').strip()))

    if not identities:
        raise RuntimeError('LOAD_TEST_USERS_CSV has no usable identities.')
    return identities


IDENTITIES = load_identities()
IDENTITY_CYCLE = itertools.cycle(IDENTITIES)
IDENTITY_LOCK = threading.Lock()
IDENTITY_INDEX = 0


def next_identity() -> TestIdentity:
    global IDENTITY_INDEX
    with IDENTITY_LOCK:
        if env_bool('LOAD_TEST_SUBMIT', False):
            if IDENTITY_INDEX >= len(IDENTITIES):
                raise StopUser('No unused LOAD_TEST_USERS_CSV identity remains in submit mode.')
            identity = IDENTITIES[IDENTITY_INDEX]
            IDENTITY_INDEX += 1
            return identity
        return next(IDENTITY_CYCLE)


class PublicSurveyUser(HttpUser):
    wait_time = between(
        float(os.getenv('LOAD_TEST_WAIT_MIN', '0.7')),
        float(os.getenv('LOAD_TEST_WAIT_MAX', '2.0')),
    )

    team_code = os.getenv('LOAD_TEST_TEAM_CODE', '').strip()
    submit_enabled = env_bool('LOAD_TEST_SUBMIT', False)
    max_days = int(os.getenv('LOAD_TEST_MAX_DAYS', '3'))
    marker_login = env_list('LOAD_EXPECT_LOGIN_MARKERS')
    marker_respondent = env_list('LOAD_EXPECT_RESPONDENT_MARKERS')
    marker_status = env_list('LOAD_EXPECT_STATUS_MARKERS')
    marker_result = env_list('LOAD_EXPECT_RESULT_MARKERS')

    def form_headers(self, referer_path: str, csrf_token: str | None = None) -> dict[str, str]:
        base = (self.environment.host or '').rstrip('/')
        headers = {
            'Referer': f'{base}{referer_path}',
            'Origin': base,
        }
        if csrf_token:
            headers['X-CSRFToken'] = csrf_token
        return headers

    def on_start(self) -> None:
        if not self.team_code:
            raise StopUser('LOAD_TEST_TEAM_CODE is required.')
        self.identity = next_identity()
        self.result_day = 1
        self.login_and_identify()

    @task
    def user_flow(self) -> None:
        self.open_status()
        self.switch_status_days()
        if self.submit_enabled:
            self.submit_if_available()
        self.open_result_pages()

    def validate(self, response, label: str, expected, markers: list[str] | None = None, *, allow_login_bounce: bool = False) -> bool:
        markers = markers or []
        path = final_path(response)
        if response.status_code >= 400:
            response.failure(f'{label}: HTTP {response.status_code}')
            return False
        if not allow_login_bounce and is_login_bounce(response):
            response.failure(f'{label}: bounced to {path}')
            return False
        if callable(expected):
            is_expected = expected(path)
        else:
            is_expected = path == expected
        if not is_expected:
            response.failure(f'{label}: unexpected final path {path}')
            return False
        if not has_marker(response, markers):
            response.failure(f'{label}: expected marker not found')
            return False
        response.success()
        return True

    def login_and_identify(self) -> None:
        with self.client.get(LOGIN_PATH, name='01 login form', catch_response=True) as response:
            if not self.validate(response, 'login form', LOGIN_PATH, self.marker_login, allow_login_bounce=True):
                raise StopUser('login form validation failed')
            form = first_post_form(response.text)
            if not form:
                response.failure('login form: POST form not found')
                raise StopUser('login form not found')

        login_payload = build_form_payload(form, {'team_code': self.team_code})
        login_action = urljoin(LOGIN_PATH, form.get('action') or LOGIN_PATH)
        with self.client.post(
                login_action,
                data=login_payload,
                headers=self.form_headers(LOGIN_PATH, login_payload.get('csrfmiddlewaretoken')),
                name='02 login team code',
                allow_redirects=True,
                catch_response=True,
        ) as response:
            if not self.validate(response, 'login post', lambda path: path in {RESPONDENT_PATH, STATUS_PATH}, allow_login_bounce=True):
                raise StopUser('login post validation failed')

        with self.client.get(RESPONDENT_PATH, name='03 respondent form', allow_redirects=True, catch_response=True) as response:
            if not self.validate(response, 'respondent form', lambda path: path in {RESPONDENT_PATH, STATUS_PATH}, self.marker_respondent, allow_login_bounce=True):
                raise StopUser('respondent form validation failed')
            if final_path(response) == STATUS_PATH:
                return
            form = first_post_form(response.text)
            if not form:
                response.failure('respondent form: POST form not found')
                raise StopUser('respondent form not found')

        respondent_payload = build_form_payload(
            form,
            {
                'name': self.identity.name,
                'birth_date': self.identity.birth_date,
                'phone': self.identity.phone,
                'gender': self.identity.gender,
            },
        )
        respondent_action = urljoin(RESPONDENT_PATH, form.get('action') or RESPONDENT_PATH)
        with self.client.post(
                respondent_action,
                data=respondent_payload,
                headers=self.form_headers(RESPONDENT_PATH, respondent_payload.get('csrfmiddlewaretoken')),
                name='04 respondent identify',
                allow_redirects=True,
                catch_response=True,
        ) as response:
            if not self.validate(response, 'respondent post', STATUS_PATH, self.marker_status):
                raise StopUser('respondent post validation failed')

    def open_status(self) -> None:
        with self.client.get(STATUS_PATH, name='10 status current', allow_redirects=True, catch_response=True) as response:
            self.validate(response, 'status current', STATUS_PATH, self.marker_status)

    def switch_status_days(self) -> None:
        for day_no in range(1, self.max_days + 1):
            with self.client.get(f'{STATUS_PATH}?day={day_no}', name='11 status day tab', allow_redirects=True, catch_response=True) as response:
                self.validate(response, 'status day tab', STATUS_PATH, self.marker_status)

    def submit_if_available(self) -> None:
        with self.client.get(STATUS_PATH, name='20 status before submit', allow_redirects=True, catch_response=True) as response:
            if not self.validate(response, 'status before submit', STATUS_PATH, self.marker_status):
                return
            survey_url = self.extract_survey_url(response.text)
        if not survey_url:
            return

        current_url = survey_url
        visited = set()
        for _ in range(30):
            if current_url in visited:
                return
            visited.add(current_url)

            with self.client.get(current_url, name='21 survey question', allow_redirects=True, catch_response=True) as response:
                if not self.validate(response, 'survey question', DAY_PATH_RE.match):
                    return
                form = first_post_form(response.text)
                if not form:
                    response.failure('survey question: POST form not found')
                    return

            payload = build_form_payload(form, q_only=True)
            if 'name="submit"' in response.text or "name='submit'" in response.text:
                payload['submit'] = ''
            else:
                payload['next'] = ''

            post_action = urljoin(current_url, form.get('action') or current_url)
            with self.client.post(
                    post_action,
                    data=payload,
                    headers=self.form_headers(urlparse(current_url).path or current_url,
                                              payload.get('csrfmiddlewaretoken')),
                    name='22 survey answer',
                    allow_redirects=False,
                    catch_response=True,
            ) as response:
                location = response.headers.get('Location', '')
                if response.status_code in REDIRECT_STATUSES and location:
                    response.success()
                    current_url = urljoin(current_url, location)
                    day_match = re.search(r'/surveys/day/(\d+)/', current_url)
                    if day_match:
                        self.result_day = int(day_match.group(1))
                    if '/complete/' in current_url or '/submitted/' in current_url:
                        with self.client.get(current_url, name='23 submit complete', allow_redirects=True, catch_response=True) as complete_response:
                            self.validate(complete_response, 'submit complete', lambda path: '/complete/' in path or '/submitted/' in path)
                        return
                    continue
                if response.status_code >= 400:
                    response.failure(f'survey answer POST failed: url={post_action} status={response.status_code}')
                else:
                    response.failure(
                        f'survey answer POST did not redirect: url={post_action} '
                        f'status={response.status_code} location_missing={not bool(location)}'
                    )
            return

    def open_result_pages(self) -> None:
        for day_no in range(1, self.max_days + 1):
            url = f'/surveys/day/{day_no}/result/?source=user&return_to=/surveys/status/'
            with self.client.get(url, name='30 result day tab', allow_redirects=True, catch_response=True) as response:
                self.validate(response, 'result day tab', RESULT_PATH_RE.match, self.marker_result)

        detail_url = f'/surveys/day/{self.result_day}/result/detail/?source=user&return_to=/surveys/status/'
        with self.client.get(detail_url, name='31 result detail', allow_redirects=True, catch_response=True) as response:
            self.validate(response, 'result detail', DETAIL_PATH_RE.match, self.marker_result)

    @staticmethod
    def extract_survey_url(html: str) -> str | None:
        hrefs = parse_html(html).links
        survey_hrefs = [href for href in hrefs if '/surveys/day/' in href and '?q=1' in href]
        if not survey_hrefs:
            return None
        return random.choice(survey_hrefs)


@events.init.add_listener
def validate_load_test_environment(environment, **kwargs) -> None:
    if not os.getenv('LOAD_TEST_TEAM_CODE', '').strip():
        raise RuntimeError('LOAD_TEST_TEAM_CODE is required.')


@events.test_start.add_listener
def validate_submit_identity_capacity(environment, **kwargs) -> None:
    if not env_bool('LOAD_TEST_SUBMIT', False):
        return
    user_count = int(getattr(environment.parsed_options, 'num_users', 0) or 0)
    if user_count and len(IDENTITIES) < user_count:
        raise RuntimeError(
            'LOAD_TEST_SUBMIT=1 requires at least one CSV identity per concurrent user. '
            f'users={user_count}, csv_rows={len(IDENTITIES)}'
        )
