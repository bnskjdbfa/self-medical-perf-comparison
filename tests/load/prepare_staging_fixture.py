"""Explicit reset of OWNED synthetic data in a dedicated staging-only database.

Run from repository root: python tests/load/prepare_staging_fixture.py --init
Later: --reset --confirm RESET-PERF-STAGING. Never calls flush or deletes other teams.
"""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
CODE = 'PERF-STAGING'
MARKER = '__isolated_performance_fixture__'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--init', action='store_true')
    modes.add_argument('--reset', action='store_true')
    parser.add_argument('--confirm')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.conf import settings
    if (settings.APP_ENV != 'staging' or settings.DB_IS_PRODUCTION is not False
            or os.environ.get('STAGING_PERF_ENABLED') != '1'
            or settings.DATABASES['default']['ENGINE'] != 'django.db.backends.postgresql'):
        raise SystemExit('Dedicated non-production PostgreSQL staging only.')
    from django.apps import apps
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.contrib.sessions.models import Session
    from django.core.management import call_command
    from django.core.management.color import no_style
    from django.db import connection, transaction
    from django.utils import timezone
    from teams.models import Team, Participant, Respondent
    from surveys.models import (TeamFeatureSetting, SurveyForm, SurveySection, SurveyQuestion,
                                LoginSessionFamily, SurveySubmission, DocumentSubmission,
                                SurveyAnswer, RequiredDocumentConsentBundle, DocumentTemplate)
    from surveys.services import normalize_name
    call_command('show_db_target')
    # A dedicated empty DB is required at enrollment. On reset only the one
    # enrolled team and its cascades, plus anonymous session state, are owned.
    if get_user_model().objects.exists() or Team.objects.exclude(code=CODE).exists():
        raise SystemExit('Refusing a database containing users or unrelated teams.')
    marker = Group.objects.filter(name=MARKER).exists()
    if args.init and (marker or Team.objects.exists() or Respondent.objects.exists()
                      or Session.objects.exists() or LoginSessionFamily.objects.exists()):
        raise SystemExit('Init requires empty domain/session data; existing data is not adopted.')
    if args.reset and (not marker or args.confirm != 'RESET-PERF-STAGING'):
        raise SystemExit('Reset requires the staging fixture marker and explicit confirmation.')
    with transaction.atomic():
        # Respect the service's PROTECT constraints. Unexpected protected
        # records cause a rollback; never disable constraints or use flush.
        SurveyAnswer.objects.filter(submission__team__code=CODE).delete()
        SurveySubmission.objects.filter(team__code=CODE).delete()
        DocumentSubmission.objects.filter(team__code=CODE).delete()
        RequiredDocumentConsentBundle.objects.filter(team__code=CODE).delete()
        Respondent.objects.filter(team__code=CODE).delete()
        SurveyForm.objects.filter(team__code=CODE).delete()
        DocumentTemplate.objects.filter(team__code=CODE).delete()
        Participant.objects.filter(team__code=CODE).delete()
        Team.objects.filter(code=CODE).delete()
        LoginSessionFamily.objects.all().delete()
        Session.objects.all().delete()
        # Reset sequences to each table's actual MAX, preserving global rows.
        with connection.cursor() as cursor:
            for statement in connection.ops.sequence_reset_sql(no_style(), apps.get_models()):
                cursor.execute(statement)
        Group.objects.get_or_create(name=MARKER)
        team = Team.objects.create(name='Synthetic staging performance', code=CODE,
            organization_name='Fixture', organization_name_en='Fixture',
            start_date=timezone.localdate(), max_day_no=3)
        TeamFeatureSetting.objects.create(team=team, enable_privacy_consent=True,
            enable_third_party_consent=True, enable_safety_pledge=True)
        form = SurveyForm.objects.create(team=team, day_no=1, title='Performance fixture', version=1)
        section = SurveySection.objects.create(form=form, title='Fixture', section_type='physical')
        for index in range(20):
            SurveyQuestion.objects.create(section=section, key=f'q{index}', label=f'Fixture {index}',
                question_type=SurveyQuestion.QuestionType.SINGLE, options_json=['yes','no'], is_required=True)
        Participant.objects.bulk_create([Participant(team=team, name=f'Stage Fixture {i:02d}',
            normalized_name=normalize_name(f'Stage Fixture {i:02d}'), birth_date=date(1990,1,1),
            gender='M', is_active=True) for i in range(1,51)])
        logical = {'team': list(Team.objects.values('id','code','start_date','max_day_no')),
            'features': list(TeamFeatureSetting.objects.values('id','team_id','enable_privacy_consent',
                'enable_third_party_consent','enable_safety_pledge')),
            'participants': list(Participant.objects.values('id','team_id','normalized_name','birth_date','gender').order_by('id')),
            'forms': list(SurveyForm.objects.values('id','team_id','day_no','version','title','is_active','form_type').order_by('id')),
            'sections': list(SurveySection.objects.values('id','form_id','title','section_type').order_by('id')),
            'questions': list(SurveyQuestion.objects.values('id','section_id','key','label','question_type',
                'is_required','options_json').order_by('id')),
            'respondents': Respondent.objects.count(), 'documents': DocumentSubmission.objects.count(),
            'submissions': SurveySubmission.objects.count(), 'sessions': Session.objects.count(),
            'session_families': LoginSessionFamily.objects.count()}
        digest = hashlib.sha256(json.dumps(logical, default=str, sort_keys=True).encode()).hexdigest()
    print(json.dumps({'fixture': CODE, 'logical_state_sha256': digest,
        'team_id': team.id, 'participants': 50, 'questions':20,
        'respondents': logical['respondents'], 'documents':logical['documents'],
        'submissions':logical['submissions'], 'sessions':logical['sessions'],
        'session_families':logical['session_families']}))


if __name__ == '__main__':
    main()
