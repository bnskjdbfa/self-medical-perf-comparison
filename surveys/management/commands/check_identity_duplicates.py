from collections import defaultdict

from django.core.management.base import BaseCommand

from surveys.services import normalize_name
from teams.models import Participant, Respondent


class Command(BaseCommand):
    help = (
        '식별 키(팀+이름+생년월일+성별)가 겹치는 참가자·응답자를 찾습니다. 이름은 저장값이 아니라 '
        '현재 정규화 규칙으로 다시 계산하므로, 정규화 규칙을 바꾸는 마이그레이션 전에도 미리 볼 수 있습니다.'
    )

    def handle(self, *args, **options):
        hard = 0
        for label, model in (('참가자 명단(Participant)', Participant), ('응답자(Respondent)', Respondent)):
            groups = defaultdict(list)
            for row in model.objects.values('id', 'team_id', 'name', 'birth_date', 'gender').order_by('team_id', 'id'):
                groups[(row['team_id'], normalize_name(row['name']), row['birth_date'], row['gender'])].append(row['id'])
            self.stdout.write(f'== {label} ==')
            found = False
            for (team_id, normalized, birth_date, gender), ids in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
                if len(ids) < 2:
                    continue
                found = True
                if gender is None:
                    kind = '경고(성별 없음, 로그인 불가)'
                else:
                    kind = '충돌(마이그레이션 차단)'
                    hard += 1
                self.stdout.write(f'  {kind} team={team_id} 이름={normalized} 생년월일={birth_date} 성별={gender} ids={ids}')
            if not found:
                self.stdout.write('  겹침 없음')
        if hard:
            self.stdout.write(self.style.ERROR(f'충돌 {hard}건: 같은 사람이면 한 행만 남기고 나머지를 정리한 뒤 migrate 하세요.'))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS('현재 식별 규칙으로 겹치는 행이 없습니다.'))
