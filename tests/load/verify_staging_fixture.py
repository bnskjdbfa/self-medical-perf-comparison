"""Verify owned fixture resets on disposable LOCAL PostgreSQL only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-port', type=int, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    from run_login_concurrency import isolated_database, ROOT
    os.environ.update({'APP_ENV':'staging', 'PYTHON_DOTENV_DISABLED':'1', 'DB_IS_PRODUCTION':'false',
        'ALLOW_DATABASE_ENV_MISMATCH':'false', 'DJANGO_SETTINGS_MODULE':'config.settings',
        'DJANGO_DEBUG':'true', 'DJANGO_ALLOWED_HOSTS':'testserver',
        'DJANGO_SECURE_SSL_REDIRECT':'false', 'DJANGO_CACHE_URL':'', 'REDIS_URL':'',
        'STAGING_PERF_ENABLED':'1'})
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='fixture-check-') as temp, isolated_database(temp,args.postgres_port) as db:
        os.environ.update(db)
        import django
        django.setup()
        from django.core.management import call_command
        from django.test import Client
        from django.utils import timezone
        from teams.models import Team, Respondent
        from surveys.models import DocumentSubmission, SurveySubmission, SurveyQuestion, SurveyAnswer
        call_command('show_db_target')
        call_command('migrate',verbosity=0)
        command = [sys.executable,str(ROOT/'tests/load/prepare_staging_fixture.py')]
        def prepare(*arguments):
            result = subprocess.run(command+list(arguments),check=True,capture_output=True,text=True)
            return json.loads(result.stdout.splitlines()[-1])
        initial = prepare('--init')
        c=Client()
        code='/surveys/preview/v2/code/'
        doc='/surveys/preview/v2/documents/third_party_consent/'
        assert c.get(code).status_code==200
        assert c.post(code,{'team_code':'PERF-STAGING'}).status_code==302
        assert c.post('/surveys/preview/v2/privacy-consent/',{'agreed':'on'}).status_code==302
        assert c.post('/surveys/preview/v2/identity/',{
            'name':'Stage Fixture 01','birth_date':'900101','gender':'M'}).status_code==302
        assert c.get(doc).status_code==200
        assert c.post(doc,{'action':'record_consent','document_type':'third_party_consent','agreed':'on'}).status_code==200
        assert c.post(doc,{'action':'finalize','document_type':'safety_pledge','agreed':'on'}).status_code==200
        respondent=Respondent.objects.get()
        team=Team.objects.get(code='PERF-STAGING')
        submission=SurveySubmission.objects.create(team=team,respondent=respondent,day_no=1,
            form_version=1,submitted_at=timezone.now())
        SurveyAnswer.objects.create(submission=submission,question=SurveyQuestion.objects.first(),answer_text='yes')
        assert DocumentSubmission.objects.count()==3
        restored=prepare('--reset','--confirm','RESET-PERF-STAGING')
        assert initial==restored
        assert not Respondent.objects.exists() and not SurveyAnswer.objects.exists()
        stranger=Team.objects.create(name='Guard fixture',code='NOT-OWNED',
            start_date=timezone.localdate(),max_day_no=3)
        refused=subprocess.run(command+['--reset','--confirm','RESET-PERF-STAGING'],capture_output=True,text=True)
        assert refused.returncode!=0 and Team.objects.filter(pk=stranger.pk).exists()
        args.report.write_text(json.dumps({'status':'pass','initial':initial,'restored':restored,
            'unrelated_team_reset_refused':True},indent=2)+'\n')
        print('PASS: real login/consents + submission reset restores hash/IDs; unrelated team refuses reset')


if __name__=='__main__':
    main()
