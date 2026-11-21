"""Authenticated CLI capabilities, using the existing bounded app-server transport."""
import json
import os
from pathlib import Path
import re
import shutil
import sys

from auto_coding.process_runner import bounded_run
from auto_coding.reviewer_permissions import _APP_SERVER_CLIENT


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
EFFORT = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def collect_models(request):
    """Reject incomplete catalogs; never infer access from a model name."""
    account = request({'id': 2, 'method': 'account/read', 'params': {'refreshToken': False}})
    if not account.get('account'):
        raise ValueError('Sign in to the local Codex CLI, then refresh models.')
    cursor, seen, models = None, set(), []
    for page in range(10):
        result = request({'id': page + 3, 'method': 'model/list',
                          'params': {'cursor': cursor, 'limit': 100}})
        for item in result['data']:
            model = item.get('model')
            efforts = [entry['reasoningEffort'] for entry in item.get('supportedReasoningEfforts', [])]
            if (not isinstance(model, str) or not IDENTIFIER.fullmatch(model)
                    or not efforts or any(not isinstance(e, str) or not EFFORT.fullmatch(e) for e in efforts)
                    or item.get('defaultReasoningEffort') not in efforts):
                raise ValueError('Unsupported Codex model catalog format. Update the CLI and refresh models.')
            models.append({'model': model, 'displayName': str(item.get('displayName') or model)[:160],
                           'efforts': efforts, 'default': item['defaultReasoningEffort']})
        cursor = result.get('nextCursor')
        if not cursor:
            if not models or len({m['model'] for m in models}) != len(models):
                raise ValueError('Empty or ambiguous Codex model catalog.')
            return models
        if cursor in seen:
            break
        seen.add(cursor)
    raise ValueError('Incomplete Codex catalog pagination. Update the CLI and refresh models.')


# The helper runs only trusted source and returns whitelisted, non-secret fields.
_CLIENT = (_APP_SERVER_CLIENT + '\nfrom auto_coding.model_catalog import collect_models\n'
           'models = collect_models(request)\nprocess.stdin.close(); process.wait(timeout=5)\n'
           'print(json.dumps(models))\n')


def load_catalog(repo):
    executable = shutil.which('codex')
    if not executable:
        raise ValueError('Codex CLI unavailable. Install and authenticate Codex, then refresh models.')
    trusted = str(Path(__file__).resolve().parents[1])
    environment = os.environ.copy()
    environment.pop('PYTHONHOME', None)
    environment['PYTHONPATH'] = trusted
    result = bounded_run([sys.executable, '-B', '-c', _CLIENT,
                          json.dumps([[executable, 'app-server', '--strict-config'], str(repo), None])],
                         cwd=trusted, env=environment, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise ValueError('Cannot read authenticated Codex models. Check CLI sign-in/configuration, then refresh models.')
    try:
        models = json.loads(result.stdout)
        if not isinstance(models, list) or not models:
            raise ValueError()
        return models
    except (ValueError, TypeError):
        raise ValueError('Invalid Codex catalog response. Update the CLI and refresh models.') from None


def validate_profile(profile, models):
    if not isinstance(profile, dict):
        raise ValueError('Apply model configuration first.')
    catalog = {m['model']: m for m in models}
    for role in ('audit', 'a', 'b'):
        model = profile.get('model_' + role)
        effort = 'high' if role == 'audit' else profile.get('effort_' + role)
        if (not isinstance(model, str) or not isinstance(effort, str)
                or model not in catalog or effort not in catalog[model]['efforts']):
            raise ValueError(f'Model/reasoning for {role.upper()} is unavailable. Refresh models and apply configuration.')
    if type(profile.get('attempts')) is not int or not 1 <= profile['attempts'] <= 10:
        raise ValueError('Attempts must be a whole number from 1 to 10.')
    if not isinstance(profile.get('goal'), str) or len(profile['goal']) > 4000:
        raise ValueError('Audit goal must be at most 4000 characters.')
    if set(profile) != {'model_audit', 'model_a', 'model_b', 'effort_a', 'effort_b', 'attempts', 'goal'}:
        raise ValueError('Unsupported configuration fields.')
    return dict(profile)
