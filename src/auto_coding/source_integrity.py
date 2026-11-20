"""Versioned source inventories, also executed as an isolated stdlib guest script.

The host baseline is authoritative. Guest observations remain cooperative reports,
not tamper-resistant attestation of a malicious guest or its filesystem.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata

VERSION = 1
LIMIT = 16 * 1024 * 1024
MAX_FILES = 50000
# Python and .NET UTC providers can differ by a Windows clock tick. Process
# ordering (all .NET observations) remains exact; only cross-provider bounds use it.
CLOCK_TOLERANCE = dt.timedelta(milliseconds=20)
IDENTITY_FIELDS = ('sourceCommit', 'sourceBranch', 'sessionRunId', 'runId', 'sandboxId',
                   'invocation', 'suiteIndex', 'policySha256')
EXCLUSIONS = {'version': 1, 'rootGitMetadata': True, 'untrackedCPythonBytecode': True}


class IntegrityError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError('Duplicate JSON field')
        result[key] = value
    return result


def read(path):
    path = Path(path)
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise IntegrityError('Linked integrity evidence is unsupported')
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise IntegrityError('Missing/oversized integrity evidence')
    return path.read_bytes()


def load(path):
    return json.loads(read(path).decode('utf-8'), object_pairs_hook=_pairs)


def safe_path(name):
    if (not isinstance(name, str) or not name or len(name) > 4096 or '\\' in name or ':' in name
            or unicodedata.normalize('NFC', name) != name or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or PurePosixPath(name).is_absolute() or PurePosixPath(name).as_posix() != name):
        raise IntegrityError('Unsafe or non-normalized inventory path')
    for part in name.split('/'):
        if (part in {'.', '..'} or part.endswith(('.', ' ')) or '~' in part
                or part.casefold() in {'.git', '.codex'} or part.casefold().split('.')[0] in {
                    'con', 'prn', 'aux', 'nul', *[f'com{i}' for i in range(10)], *[f'lpt{i}' for i in range(10)]}):
            raise IntegrityError('Unsafe or ambiguous inventory path')
    return name


def records(values):
    if not isinstance(values, list) or len(values) > MAX_FILES:
        raise IntegrityError('Incomplete or oversized file inventory')
    aliases, files, directories = {}, set(), set()
    for item in values:
        if (not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}
                or type(item['size']) is not int or item['size'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise IntegrityError('Malformed size/SHA-256 record')
        name = safe_path(item['path'])
        if name in files:
            raise IntegrityError('Duplicate file inventory path')
        parts = name.split('/')
        for index in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:index])
            alias = prefix.casefold()
            if alias in aliases and aliases[alias] != prefix:
                raise IntegrityError('Ambiguous Windows inventory identity')
            aliases[alias] = prefix
            if index < len(parts):
                directories.add(prefix)
        files.add(name)
    if files & directories:
        raise IntegrityError('A path cannot be both file and directory')
    return sorted(values, key=lambda item: item['path'])


def identity(request):
    value = {key: request[key] for key in IDENTITY_FIELDS}
    if (any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 1024
            for key in IDENTITY_FIELDS if key not in {'invocation', 'suiteIndex'})
            or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', value['sourceCommit'])
            or not re.fullmatch('[0-9a-f]{64}', value['policySha256'])
            or any(type(value[key]) is not int or value[key] < 1 for key in ('invocation', 'suiteIndex'))):
        raise IntegrityError('Invalid integrity identity')
    if any(any(ord(char) < 32 for char in value[key]) for key in IDENTITY_FIELDS if isinstance(value[key], str)):
        raise IntegrityError('Unsafe integrity identity')
    return value


def baseline(request, files):
    files = records(files)
    return {'schemaVersion': VERSION, 'kind': 'source-baseline', 'identity': identity(request),
            'exclusions': dict(EXCLUSIONS), 'files': files, 'fileCount': len(files),
            'totalBytes': sum(item['size'] for item in files)}


def validate_baseline(value, request, expected_sha):
    if (not isinstance(value, dict) or set(value) != {'schemaVersion','kind','identity','exclusions','files','fileCount','totalBytes'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != VERSION
            or value['kind'] != 'source-baseline' or value['identity'] != identity(request)
            or canonical(value['exclusions']) != canonical(EXCLUSIONS) or type(value['fileCount']) is not int
            or type(value['totalBytes']) is not int or value != baseline(request, value['files'])
            or not isinstance(expected_sha, str) or digest(value) != expected_sha):
        raise IntegrityError('Baseline schema, inventory, identity or fingerprint mismatch')
    identity(value['identity'])
    return value


def file_record(name, data):
    return {'path': safe_path(name), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def compare(expected, observed):
    left = {item['path']: item for item in records(expected)}
    right = {item['path']: item for item in records(observed)}
    return {'added': sorted(right.keys() - left.keys()), 'deleted': sorted(left.keys() - right.keys()),
            'modified': sorted(name for name in left.keys() & right.keys() if left[name] != right[name])}


def bytecode(name):
    parts = name.split('/')
    return (len(parts) >= 2 and parts[-2] == '__pycache__'
            and re.fullmatch(r'[^/]+\.cpython-[0-9]{2,3}(?:\.opt-[0-9]+)?\.pyc', parts[-1]) is not None)


def inventory(root, protected):
    root = Path(root)
    protected = {item['path'] for item in records(protected)}
    pending, found, excluded = [root], [], []
    total, seen = 0, 0
    while pending:
        directory = pending.pop()
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise IntegrityError('Inventory directory is a link or not a directory')
        with os.scandir(directory) as entries:
            for entry in entries:
                seen += 1
                if seen > MAX_FILES * 2:
                    raise IntegrityError('Inventory enumeration limit exceeded')
                path = Path(entry.path)
                name = path.relative_to(root).as_posix()
                before = path.lstat()
                if stat.S_ISLNK(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
                    raise IntegrityError('Inventory contains a link/reparse point')
                if name == '.git' and stat.S_ISDIR(before.st_mode):
                    excluded.append(name)
                    continue
                safe_path(name)
                if stat.S_ISDIR(before.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(before.st_mode):
                    raise IntegrityError('Inventory contains a non-regular file')
                if name not in protected and bytecode(name):
                    excluded.append(name)
                    continue
                total += before.st_size
                if total > 128 * 1024 * 1024 or len(found) >= MAX_FILES:
                    raise IntegrityError('Inventory byte/file bound exceeded')
                with path.open('rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise IntegrityError('File identity changed during inventory')
                    hashed = hashlib.file_digest(stream, 'sha256').hexdigest()
                    after = os.fstat(stream.fileno())
                last = path.lstat()
                # Windows lstat/fstat expose different ctime semantics. Compare
                # ctime only within the same API; content/identity/mtime still
                # have to agree across both APIs and the complete read.
                stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                if (stamp(before) != stamp(opened) or stamp(opened) != stamp(after) or stamp(before) != stamp(last)
                        or before.st_ctime_ns != last.st_ctime_ns or opened.st_ctime_ns != after.st_ctime_ns):
                    raise IntegrityError(f'File changed during hashing: {name}')
                found.append({'path': name, 'size': before.st_size, 'sha256': hashed})
    return records(found), sorted(excluded)


def check_archive(path, expected):
    import zipfile
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if (len(entries) > MAX_FILES or any(item.is_dir() or stat.S_ISLNK(item.external_attr >> 16) for item in entries)
                or sum(item.file_size for item in entries) > LIMIT):
            raise IntegrityError('Invalid packaged source inventory')
        observed = records([file_record(item.filename, archive.read(item)) for item in entries])
    differences = compare(expected['files'], observed)
    if any(differences.values()):
        raise IntegrityError('Packaged source differs from host SHA-256 baseline')


def validate_observation(value, expected, stage):
    keys = {'schemaVersion','kind','stage','identity','baselineSha256','complete','files','fileCount','totalBytes',
            'excluded','matchesBaseline','differences','startedUtc','finishedUtc','error'}
    if (not isinstance(value, dict) or set(value) != keys or type(value['schemaVersion']) is not int
            or value['schemaVersion'] != VERSION or value['kind'] != 'source-observation' or value['stage'] != stage
            or value['identity'] != expected['identity'] or value['baselineSha256'] != digest(expected)
            or value['complete'] is not True or value['error'] is not None
            or type(value['fileCount']) is not int or type(value['totalBytes']) is not int):
        raise IntegrityError('Missing, incomplete, stale or malformed integrity observation')
    identity(value['identity'])
    observed = records(value['files'])
    if (observed != value['files'] or len(observed) != value['fileCount']
            or sum(item['size'] for item in observed) != value['totalBytes']):
        raise IntegrityError('Truncated or inconsistent inventory')
    excluded = value['excluded']
    protected = {item['path'] for item in expected['files']}
    if (not isinstance(excluded, list) or any(not isinstance(name, str) for name in excluded)
            or excluded != sorted(set(excluded)) or len(excluded) > MAX_FILES
            or any(name != '.git' and (not bytecode(safe_path(name)) or name in protected) for name in excluded)):
        raise IntegrityError('Invalid inventory exclusion')
    differences = compare(expected['files'], observed)
    matches = not any(differences.values())
    if value['matchesBaseline'] is not matches or value['differences'] != differences:
        raise IntegrityError('Guest integrity verdict contradicts inventory')
    if timestamp(value['finishedUtc']) < timestamp(value['startedUtc']):
        raise IntegrityError('Invalid integrity observation timestamps')
    return {'complete': True, 'matchesBaseline': matches, 'fileCount': len(observed), 'differences': differences}


def timestamp(value):
    if not isinstance(value, str):
        raise IntegrityError('Missing integrity timestamp')
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise IntegrityError('Integrity timestamps must have a timezone')
    return result


def worker_arguments(stage, sha):
    return ['-I','-B',r'C:\ACSetup\source_integrity.py','--root',r'C:\AutoCodingTest',
            '--baseline',r'C:\ACSetup\source-baseline.json','--sha256',sha,'--stage',stage,
            '--output',rf'C:\ACResults\integrity-{stage}.json']


def verify_receipt(root, request, result, read_json):
    try:
        spec = request['sourceIntegrity']
        if set(spec) != {'schemaVersion', 'baselineSha256'} or type(spec['schemaVersion']) is not int or spec['schemaVersion'] != VERSION:
            raise IntegrityError('Missing versioned source-integrity contract')
        expected = validate_baseline(load(root / 'source-baseline.json'), request, spec['baselineSha256'])
        if read(root / 'input/source-baseline.json') != canonical(expected):
            raise IntegrityError('Read-only baseline copy is missing or differs from host authority')
        check_archive(root / 'input/baseline.zip', expected)
        receipt = result.get('sourceIntegrity')
        if (not isinstance(receipt, dict) or set(receipt) != {'schemaVersion','baselineSha256','pre','post'}
                or type(receipt['schemaVersion']) is not int or receipt['schemaVersion'] != VERSION
                or receipt['baselineSha256'] != digest(expected)):
            raise IntegrityError('Missing/malformed guest integrity receipt')
        verdict = {'schemaVersion': VERSION, 'baselineSha256': digest(expected), 'verified': False,
                   'hostPackageVerified': True, 'observations': {}}
        for stage in ('pre', 'post'):
            value = load(root / 'results' / f'integrity-{stage}.json')
            observed = validate_observation(value, expected, stage)
            process = read_json(root / 'results' / f'integrity-{stage}-hash.process.json')
            if (type(process.get('exitCode')) is not int or process['exitCode'] != (0 if observed['matchesBaseline'] else 1)
                    or process.get('timedOut') is not False):
                raise IntegrityError('Integrity worker failed or timed out')
            if (process.get('executable') != r'C:\Python313Embed\python.exe'
                    or process.get('arguments') != worker_arguments(stage, digest(expected))
                    or process.get('workingDirectory') != r'C:\AutoCodingTest'
                    or type(process.get('timeoutSeconds')) is not int or not 1 <= process['timeoutSeconds'] <= 120
                    or process.get('stdout') != rf'C:\ACResults\integrity-{stage}-hash.stdout.log'
                    or process.get('stderr') != rf'C:\ACResults\integrity-{stage}-hash.stderr.log'
                    or not (timestamp(result['startedUtc']) <= timestamp(process['startedUtc'])
                            <= timestamp(process['finishedUtc']) <= timestamp(result['finishedUtc']))
                    or not (timestamp(process['startedUtc']) - CLOCK_TOLERANCE <= timestamp(value['startedUtc'])
                            <= timestamp(value['finishedUtc']) <= timestamp(process['finishedUtc']) + CLOCK_TOLERANCE)):
                raise IntegrityError('Incomplete or stale integrity worker metadata')
            for stream in ('stdout', 'stderr'):
                read(root / 'results' / f'integrity-{stage}-hash.{stream}.log')
            suites = result.get('suites', [])
            if suites:
                boundaries = [timestamp(suite['process']['startedUtc' if stage == 'pre' else 'finishedUtc'])
                              for suite in suites]
                boundary = min(boundaries) if stage == 'pre' else max(boundaries)
                if ((stage == 'pre' and timestamp(process['finishedUtc']) > boundary)
                        or (stage == 'post' and timestamp(process['startedUtc']) < boundary)):
                    raise IntegrityError('Integrity observation is not before/after the test')
            # The expected hashes are never read from writable results.
            verdict['observations'][stage] = observed
            claimed = receipt[stage]
            if (not isinstance(claimed, dict) or set(claimed) != {'complete','matchesBaseline'}
                    or claimed['complete'] is not True or claimed['matchesBaseline'] is not observed['matchesBaseline']):
                raise IntegrityError('Guest receipt contradicts integrity evidence')
        verdict['verified'] = all(item['matchesBaseline'] for item in verdict['observations'].values())
        return verdict
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise IntegrityError(f'Source integrity evidence rejected: {exc}') from exc


def valid_verdict(value):
    return (isinstance(value, dict) and set(value) == {'schemaVersion','baselineSha256','verified','hostPackageVerified','observations'}
            and type(value.get('schemaVersion')) is int and value['schemaVersion'] == VERSION
            and value.get('verified') is True and value.get('hostPackageVerified') is True
            and isinstance(value.get('baselineSha256'), str) and re.fullmatch('[0-9a-f]{64}', value['baselineSha256']) is not None
            and isinstance(value.get('observations'), dict) and set(value['observations']) == {'pre', 'post'}
            and all(isinstance(item, dict) and set(item) == {'complete','matchesBaseline','fileCount','differences'}
                    and item.get('complete') is True and item.get('matchesBaseline') is True
                    and type(item.get('fileCount')) is int and item['fileCount'] >= 0
                    and item.get('differences') == {'added':[],'deleted':[],'modified':[]}
                    for item in value['observations'].values())
            and value['observations']['pre']['fileCount'] == value['observations']['post']['fileCount'])


def observe(root, expected, stage):
    start = dt.datetime.now(dt.timezone.utc).isoformat()
    value = {'schemaVersion': VERSION, 'kind': 'source-observation', 'stage': stage,
             'identity': dict(expected['identity']), 'baselineSha256': digest(expected), 'complete': False,
             'files': [], 'fileCount': 0, 'totalBytes': 0, 'excluded': [], 'matchesBaseline': False,
             'differences': None, 'startedUtc': start, 'finishedUtc': start, 'error': None}
    try:
        files, excluded = inventory(root, expected['files'])
        differences = compare(expected['files'], files)
        value.update(files=files, fileCount=len(files), totalBytes=sum(item['size'] for item in files),
                     excluded=excluded, complete=True, differences=differences,
                     matchesBaseline=not any(differences.values()))
    except (OSError, ValueError) as exc:
        value['error'] = str(exc)
    value['finishedUtc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    return value


def main():
    parser = argparse.ArgumentParser()
    for name in ('root', 'baseline', 'sha256', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--stage', choices=('pre', 'post'), required=True)
    args = parser.parse_args()
    expected = load(args.baseline)
    validate_baseline(expected, expected['identity'], args.sha256)
    value = observe(args.root, expected, args.stage)
    output = Path(args.output)
    if output.exists():
        raise IntegrityError('Integrity observation already exists')
    temporary = output.with_suffix(output.suffix + '.tmp')
    payload = canonical(value)
    if len(payload) > LIMIT:
        raise IntegrityError('Integrity observation exceeds evidence size bound')
    with temporary.open('xb') as stream:
        stream.write(payload)
    os.replace(temporary, output)
    return 0 if value['complete'] and value['matchesBaseline'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
