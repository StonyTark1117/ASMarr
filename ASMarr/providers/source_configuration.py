"""Public, allowlisted source settings. Secrets and tool paths never enter SQLite."""
import copy
import json
import re

FIELDS = {
    'reddit': {'subreddits': [], 'allowlist': [], 'reddit_max_pages': 5},
    'soundgasm': {'soundgasm_creators': []},
    'youtube': {'youtube_enabled': False, 'youtube_reddit_links_enabled': False,
                'youtube_poll_limit': 50, 'youtube_catchup_limit': 500,
                'youtube_initial_downloads': 3, 'youtube_min_duration_seconds': 180},
    'sfw': {'enabled': False, 'auto_add': False, 'subreddits': [],
            'initial_per_creator': 3, 'reddit_pages': 3, 'max_new_per_run': 12,
            'max_creators': 20, 'max_auto_additions_per_run': 2},
}


def validate(kind, value):
    if kind not in FIELDS or not isinstance(value, dict):
        raise ValueError('invalid_source_configuration')
    for key, item in value.items():
        if key not in FIELDS[kind]:
            raise ValueError('unsupported_source_field')
        default = FIELDS[kind][key]
        if isinstance(default, bool):
            valid = isinstance(item, bool)
        elif isinstance(default, list):
            valid = isinstance(item, list) and len(item) <= 1000 and all(
                isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', x) for x in item)
        else:
            maximum = 86400 if key.endswith('seconds') else 5000
            minimum = 1 if key in {'reddit_max_pages', 'reddit_pages', 'youtube_poll_limit', 'youtube_catchup_limit', 'max_creators'} else 0
            maximum = 50 if key in {'reddit_max_pages', 'reddit_pages'} else maximum
            valid = type(item) is int and minimum <= item <= maximum
        if not valid:
            raise ValueError('invalid_source_field')
    return copy.deepcopy(value)


def overrides(db, kind):
    row = db.execute('SELECT value FROM settings WHERE key=?', ('source.' + kind + '.configuration',)).fetchone()
    return validate(kind, json.loads(row[0])) if row else {}


def apply(db, base):
    result = copy.deepcopy(base)
    for kind in FIELDS:
        values = overrides(db, kind)
        if kind == 'sfw':
            if values:
                result['sfw_expansion'] = dict(result.get('sfw_expansion', {}), **values)
        else:
            result.update(values)
    return result


def snapshot(db, base, kind):
    if kind not in FIELDS:
        raise ValueError('invalid_source_configuration')
    effective = apply(db, base)
    source = effective.get('sfw_expansion', {}) if kind == 'sfw' else effective
    configuration = {key: source.get(key, default) for key, default in FIELDS[kind].items()}
    # Only discovery checkpoint keys, never arbitrary meta or protected config.
    prefix = {'reddit': 'reddit_checkpoint:', 'youtube': 'youtube_seen:'}.get(kind)
    checkpoints = [{'key': row[0], 'value': json.loads(row[1])} for row in
                   db.execute('SELECT key,value FROM meta WHERE key LIKE ? ORDER BY key', (prefix + '%',))] if prefix else []
    test = db.execute("SELECT created,finished,state,result FROM commands WHERE name='source-test' AND json_extract(arguments,'$.kind')=? ORDER BY created DESC LIMIT 1", (kind,)).fetchone()
    latest = dict(zip(['created', 'finished', 'state', 'result'], test)) if test else None
    if latest and latest['result']:
        result = json.loads(latest['result'])
        latest['result'] = {key: result[key] for key in ('status', 'kind') if key in result}
    return {'kind': kind, 'configuration': configuration, 'checkpoints': checkpoints,
            'checkpointPolicy': 'Discovery cursors' if prefix else 'No cursor; profile inventory is re-enumerated',
            'rateLimits': {'minimumHttpIntervalSeconds': effective.get('request_interval', 1.1), 'maximumHttpAttempts': 3,
                           'retryAfterRespected': True, 'remaining': None,
                           'note': 'Remaining quota is not reported; source health shows throttling failures.'},
            'latestTest': latest}


def save(db, kind, values):
    value = validate(kind, values)
    with db:
        db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   ('source.' + kind + '.configuration', json.dumps(value)))
    return {'status': 'saved'}
