import secrets

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError

from surveys.reflection_exports import REFLECTION_STORAGE_ALIAS


class Command(BaseCommand):
    help = '소감문 Private Storage 설정을 확인하고 선택적으로 쓰기·읽기·삭제를 검증합니다.'

    def add_arguments(self, parser):
        parser.add_argument('--write-test', action='store_true')

    def handle(self, *args, **options):
        storage = storages[REFLECTION_STORAGE_ALIAS]
        self.stdout.write(f'backend={storage.__class__.__module__}.{storage.__class__.__name__}')
        if not options['write_test']:
            self.stdout.write(self.style.WARNING('읽기 전용 설정 확인 완료 (--write-test 미사용)'))
            return

        path = f'_healthchecks/{secrets.token_hex(12)}.txt'
        payload = secrets.token_bytes(32)
        saved_path = None
        try:
            saved_path = storage.save(path, ContentFile(payload))
            with storage.open(saved_path, 'rb') as stored_file:
                if stored_file.read() != payload:
                    raise CommandError('Storage 읽기 결과가 원본과 일치하지 않습니다.')
        finally:
            if saved_path and storage.exists(saved_path):
                storage.delete(saved_path)
        if saved_path and storage.exists(saved_path):
            raise CommandError('Storage 검증 파일이 삭제되지 않았습니다.')
        self.stdout.write(self.style.SUCCESS('Private Storage 쓰기·읽기·삭제 검증 통과'))
