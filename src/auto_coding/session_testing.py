"""Approval-bound coverage declarations for a complete coding session.

Mappings are reviewed declarations, not inferred semantic coverage. Every bound
test command remains mandatory; an unmatched change never falls back to CI.
"""
from fnmatch import fnmatchcase
import re

from auto_coding import source_integrity


def coverage_policy(value, command_count, minimum_tests):
    required = {'rules', 'minimum_tests_per_suite'}
    if (not isinstance(value, dict) or not required <= set(value)
            <= required | {'mandatory_security_suites'}):
        raise ValueError('Session coverage requires rules and minimum_tests_per_suite')

    def indices(items):
        if (not isinstance(items, list) or not items or len(items) > command_count
                or any(type(index) is not int or not 1 <= index <= command_count for index in items)
                or len(set(items)) != len(items)):
            raise ValueError('Coverage suite indices must name distinct approved mandatory commands')
        return sorted(items)

    minimum = value['minimum_tests_per_suite']
    if (not isinstance(minimum, list) or len(minimum) != command_count
            or any(type(count) is not int or not 1 <= count <= 1000000 for count in minimum)
            or sum(minimum) < minimum_tests):
        raise ValueError('Session suite minimum counts must cover the approved combined minimum')
    rules = value['rules']
    if not isinstance(rules, list) or not 1 <= len(rules) <= 100:
        raise ValueError('Session coverage requires 1..100 explicit rules')
    result, seen = [], set()
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) != {'id', 'paths', 'suite_indices', 'reason'}
                or not isinstance(rule['id'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,49}', rule['id']) or rule['id'] in seen
                or not isinstance(rule['reason'], str) or not 1 <= len(rule['reason'].strip()) <= 2000
                or not isinstance(rule['paths'], list) or not 1 <= len(rule['paths']) <= 100
                or any(not isinstance(path, str) for path in rule['paths'])
                or len(set(rule['paths'])) != len(rule['paths'])):
            raise ValueError('Malformed or duplicate session coverage rule')
        paths = [source_integrity.safe_path(pattern) for pattern in rule['paths']]
        result.append({'id': rule['id'], 'paths': sorted(paths),
                       'suite_indices': indices(rule['suite_indices']), 'reason': rule['reason'].strip()})
        seen.add(rule['id'])
    security = value.get('mandatory_security_suites', [])
    if security:
        security = indices(security)
    elif security != []:
        raise ValueError('Mandatory security suites must be an index list')
    return {'rules': sorted(result, key=lambda rule: rule['id']),
            'minimum_tests_per_suite': list(minimum), 'mandatory_security_suites': security}


def changed_records(output):
    if not output:
        return []
    if not output.endswith('\0'):
        raise ValueError('Truncated session change inventory')
    fields, index, records = output[:-1].split('\0'), 0, []
    while index < len(fields):
        status = fields[index]
        index += 1
        count = 2 if re.fullmatch(r'R(?:100|[0-9]{1,2})', status) else 1
        if status not in {'A', 'M', 'D', 'T'} and count != 2:
            raise ValueError('Unsupported session change status')
        paths = fields[index:index + count]
        if len(paths) != count:
            raise ValueError('Incomplete renamed session path')
        records.append({'status': status, 'paths': [source_integrity.safe_path(path) for path in paths]})
        index += count
    return records


def select_session(repo, state, final_head, policy, policy_digest, git):
    """Inventory initial..final plus every approved checkpoint, including reverts."""
    coverage = policy.get('session_coverage')
    if coverage is None:
        raise ValueError('Windows final testing requires an explicitly approved session_coverage map; '
                         'prepare and audit that policy, rather than replacing the existing mandatory suites')
    initial = state.get('initial_git_head')
    if (not isinstance(initial, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', initial)
            or not isinstance(final_head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', final_head)):
        raise ValueError('Missing approved session starting commit')
    history = state.get('history')
    if not isinstance(history, list) or any(not isinstance(record, dict) for record in history):
        raise ValueError('Ambiguous approved session history')
    records = [record for record in history if record.get('commit')]
    if [record.get('task') for record in records] != state['completed']:
        raise ValueError('Session coverage requires all approved task checkpoints')

    def changes(before, after):
        git(repo, ['--no-replace-objects', 'merge-base', '--is-ancestor', before, after])
        return changed_records(git(repo, ['--no-replace-objects', 'diff', '--no-ext-diff', '--no-textconv',
                                          '--name-status', '-z', '--find-renames', before, after, '--']))

    previous, checkpoints, paths = initial, [], set()
    for record in records:
        if (not isinstance(record.get('commit'), str)
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', record['commit'])
                or not isinstance(record.get('review'), dict)
                or record.get('base_head') != previous or record.get('branch') not in (None, state['branch'])
                or record.get('review', {}).get('decision') != 'APPROVED'
                or record.get('review', {}).get('issues') != []):
            raise ValueError('Session checkpoint is not bound to its approved base/review')
        delta = changes(previous, record['commit'])
        checkpoints.append({'task': record['task'], 'base': previous, 'head': record['commit'], 'changes': delta})
        paths.update(path for change in delta for path in change['paths'])
        previous = record['commit']
    if previous != final_head:
        raise ValueError('Final source is not the complete approved session output')
    net = changes(initial, final_head)
    paths.update(path for change in net for path in change['paths'])
    matches, missing = [], []
    for path in sorted(paths):
        rules = [rule for rule in coverage['rules'] if any(fnmatchcase(path, pattern) for pattern in rule['paths'])]
        if not rules:
            missing.append(path)
        matches.append({'path': path, 'rules': [rule['id'] for rule in rules],
                        'suite_indices': sorted({index for rule in rules for index in rule['suite_indices']})})
    if missing:
        raise ValueError('Approved session coverage is missing paths: ' + ', '.join(missing[:20]))
    selection = {'schemaVersion': 1, 'sessionRunId': state['run_id'], 'initialCommit': initial,
                 'finalCommit': final_head, 'branch': state['branch'], 'policySha256': policy_digest,
                 'checkpoints': checkpoints, 'netChanges': net, 'coverage': matches,
                 'selectedSuiteIndices': list(range(1, len(policy['test_commands']) + 1)),
                 'mandatorySecuritySuites': coverage['mandatory_security_suites'],
                 'minimumTestsPerSuite': coverage['minimum_tests_per_suite']}
    if len(source_integrity.canonical(selection)) > 512 * 1024:
        raise ValueError('Session selection exceeds the bounded evidence size')
    return selection
