"""Create code-only reproducible archives + a standalone two-commit Git bundle.

Does not modify the original Git index/history, push, or deploy. Excludes runtime
data, .env, database files, CSVs and uploads. Optimized service overlay is explicit.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ('config/db_limits.py', 'config/session_backend.py',
           'config/session_middleware.py', 'surveys/views.py')


def allowed(name):
    path = Path(name)
    if any(part.startswith('.') or part == '__pycache__' for part in path.parts):
        return False
    if name in {'manage.py', 'requirements.txt'}:
        return True
    if path.parts[0] in {'config', 'teams', 'surveys', 'admins'}:
        return path.suffix == '.py'
    return path.parts[0] in {'templates', 'static'}


def code_digest(files):
    rows = {name: hashlib.sha256(files[name]).hexdigest() for name in SERVICE}
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def archive(files, target):
    with target.open('wb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as tar:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = len(data), 0o644, 0
                    tar.addfile(info, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='39a1af27821eac9987cc4ea88d810d9046f2e94f')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    if out.exists():
        raise SystemExit('Output must be a NEW directory; snapshots are immutable.')
    baseline = subprocess.check_output(['git', 'rev-parse', args.baseline+'^{commit}'], cwd=ROOT, text=True).strip()
    source = subprocess.check_output(['git', 'archive', '--format=tar', baseline], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(source)) as tar:
        before = {row.name: tar.extractfile(row).read() for row in tar
                  if row.isfile() and allowed(row.name)}
    after = dict(before)
    for name in SERVICE:
        after[name] = (ROOT/name).read_bytes()
    # The same frozen support files are included in BOTH deployable builds.
    for path in sorted((ROOT/'tests/load').iterdir()):
        if path.is_file() and path.suffix in {'.py', '.cjs'}:
            name = str(path.relative_to(ROOT))
            before[name] = after[name] = path.read_bytes()
    if code_digest(before) == code_digest(after):
        raise SystemExit('Service digests match; no before/after comparison exists.')
    out.mkdir(parents=True)
    repo = out/'source'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'perf-before', str(repo)], check=True)
    env = {**os.environ, 'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+0000',
           'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+0000'}
    manifest = {'base_commit': baseline, 'service_overlay': list(SERVICE), 'variants': {}}
    for label, files, branch in [('before', before, 'perf-before'), ('after', after, 'perf-after')]:
        if label == 'after':
            subprocess.run(['git', '-C', str(repo), 'checkout', '-q', '-b', branch], check=True)
        for name, data in files.items():
            path = repo/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        subprocess.run(['git', '-C', str(repo), 'add', '--all'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Performance Snapshot',
                        '-c', 'user.email=snapshot@invalid', '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=/dev/null',
                        'commit', '-q', '-m', 'Frozen performance '+label], env=env, check=True)
        commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        artifact = out/(label+'.tar.gz')
        archive(files, artifact)
        manifest['variants'][label] = {'branch': branch, 'commit': commit, 'code_digest': code_digest(files),
            'archive_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
            'file_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    subprocess.run(['git', '-C', str(repo), 'bundle', 'create', str(out/'comparison.bundle'),
                    'perf-before', 'perf-after'], check=True)
    manifest['bundle_sha256'] = hashlib.sha256((out/'comparison.bundle').read_bytes()).hexdigest()
    (out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps({'output': str(out), 'variants': {key: {field: row[field] for field in
        ('branch','commit','code_digest','archive_sha256')} for key,row in manifest['variants'].items()}}, indent=2))


if __name__ == '__main__':
    main()
