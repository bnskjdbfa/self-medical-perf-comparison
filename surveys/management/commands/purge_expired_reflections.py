from django.core.management.base import BaseCommand

from surveys.reflection_exports import purge_expired_reflection_exports


class Command(BaseCommand):
    help = '최초 ZIP 다운로드 후 보관기간이 지난 소감문 PDF와 원문을 삭제합니다.'

    def handle(self, *args, **options):
        purged_count = purge_expired_reflection_exports()
        self.stdout.write(
            self.style.SUCCESS(f'만료 소감문 정리 완료: {purged_count}건')
        )
