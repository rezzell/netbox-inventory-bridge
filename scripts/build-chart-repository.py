#!/usr/bin/env python3
"""Build a Helm repository from all default-branch chart history.

Every changed chart requires a version bump. Rebuilds retain all historical
versions and produce identical package bytes for identical chart content.
"""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile


def run(args, **kwargs): return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # Full Git history is the durable source of chart releases. First-parent
    # commits permit app-only changes without repackaging unchanged charts.
    revisions = subprocess.check_output(['git', 'rev-list', '--first-parent', '--reverse', 'HEAD', '--', 'charts/']).decode().splitlines()
    versions = {}
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        for index, revision in enumerate(revisions):
            tree = base / str(index); tree.mkdir()
            archive = tree / 'source.tar'
            with archive.open('wb') as stream: run(['git', 'archive', revision, 'charts/'], stdout=stream)
            with tarfile.open(archive) as source: source.extractall(tree, filter='data')
            for chart in sorted((tree / 'charts').glob('*/Chart.yaml')):
                directory = chart.parent
                # Compare logical input, because gzip/Helm archive timestamps vary.
                digest = hashlib.sha256()
                for path in sorted(directory.rglob('*')):
                    if path.is_file() and 'tests' not in path.relative_to(directory).parts:
                        digest.update(str(path.relative_to(directory)).encode() + b'\0' + path.read_bytes() + b'\0')
                metadata = subprocess.check_output(['helm', 'show', 'chart', str(directory)]).decode()
                name = next(line.split(':', 1)[1].strip() for line in metadata.splitlines() if line.startswith('name:'))
                version = next(line.split(':', 1)[1].strip() for line in metadata.splitlines() if line.startswith('version:'))
                key = (name, version)
                value = digest.hexdigest()
                if key in versions:
                    if versions[key] != value: raise SystemExit('Chart %s %s changed without a version bump' % key)
                    continue
                versions[key] = value
                run(['helm', 'package', str(directory), '--destination', str(tree)])
                # Helm records current timestamps. Normalize archive metadata so a
                # published version has stable bytes when rebuilt from Git.
                package = tree / (name + '-' + version + '.tgz')
                normalized = io.BytesIO()
                with tarfile.open(package, 'r:gz') as source, tarfile.open(fileobj=normalized, mode='w') as target:
                    for entry in source:
                        entry.mtime, entry.uid, entry.gid = 0, 0, 0
                        entry.uname = entry.gname = ''
                        entry.pax_headers = {}
                        target.addfile(entry, source.extractfile(entry) if entry.isfile() else None)
                output = args.output / package.name
                compressed = io.BytesIO()
                with gzip.GzipFile(filename='', fileobj=compressed, mode='wb', mtime=0, compresslevel=9) as stream:
                    stream.write(normalized.getvalue())
                archive_bytes = compressed.getvalue()
                if output.exists() and output.read_bytes() != archive_bytes:
                    raise SystemExit('Refusing to overwrite existing package: ' + output.name)
                output.write_bytes(archive_bytes)
    run(['helm', 'repo', 'index', str(args.output), '--url', args.url])


if __name__ == '__main__': main()
