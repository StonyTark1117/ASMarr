"""Additional SFW discovery; no changes to legacy source lists or content rules."""
import collections
import hashlib
import re
import time
from urllib.parse import unquote, urlparse
from categories import CATEGORIES as THEMES


CATEGORIES = {
    'sleep': r'\b(?:sleep(?:y|ing|aid)?|sleep[ -]aid|asleep|bedtime|pillowtalk)\b',
    'relaxation': r'\b(?:asmr|relax(?:ation|ing)?|soft[ -](?:spoken|voice)|whisper(?:s|ing)?|calm(?:ing)?|meditat(?:ion|ing)|breathing)\b',
    'personal_attention': r'\b(?:personal[ -]attention|comfort(?:s|ing)?|reassur\w*|affirmations?|emotional[ -]support|cuddl\w*|hair[ -](?:brushing|playing)|makeup)\b',
    'hypnosis': r'\b(?:hypno(?:sis|tic|tized|tised)?|trance|induction|fractionation|mesmeri[sz]\w*)\b',
}
CATEGORIES.update({key: pattern for key, (_, pattern) in THEMES.items() if key not in CATEGORIES})
EXPLICIT = re.compile(r'(?i)(?<!\w)(?:nsfw|r18|18\+|explicit|porn\w*|sexual|sex|orgas\w*|masturbat\w*|ejaculat\w*|blowjob|handjob|penetration)(?!\w)')
SFW = re.compile(r'(?i)(?<!\w)sfw(?!\w)')
EXTRA_FANTASY = re.compile(r'(?i)\b(?:fantasy|neko|minotaur|witch|fox[ -]girl|cat[ -]girl)\b')


def qualify(title, cfg, api, *, profile=False, trusted=False):
    text = api.plain(title)
    if EXPLICIT.search(text):
        return False, 'explicit_metadata', []
    if re.search(r'(?i)\bscript[ -](?:offer|request|preview)\b', text):
        return False, 'script_not_recording', []
    # Only a recording's own title can establish SFW status for profile crawling.
    # Creator descriptions often advertise BOTH SFW and NSFW catalogs.
    if profile and not SFW.search(text):
        return False, 'missing_sfw_recording_tag', []
    if EXTRA_FANTASY.search(text):
        return False, 'fantasy_topic', []
    normalized = text + ' ' + ' '.join('[' + tag + ']' for tag in
        re.findall(r'(?i)(?<!\w)([FM]4[FMA]+)(?!\w)', text))
    ok, reason = api.title_passes(normalized, dict(cfg, hypno_required=False), trusted=trusted)
    if not ok:
        return False, reason, []
    enabled = cfg.get('sfw_expansion', {}).get('categories', list(CATEGORIES))
    categories = [name for name, pattern in CATEGORIES.items()
                  if name in enabled and re.search(pattern, text, re.I)]
    return (True, 'accepted', categories) if categories else (False, 'missing_topic', [])


def soundgasm_targets(post, api):
    return [x for x in api.targets(post, {}) if x[0] == 'soundgasm']


def owner(url):
    pieces = unquote(urlparse(url).path).strip('/').split('/')
    return pieces[1] if len(pieces) == 3 and pieces[0] == 'u' else ''


def auto_candidate(records, author, options):
    recent = sorted(records.values(), key=lambda x: x['published'], reverse=True)[:20]
    matches = [r for r in recent if r['eligible'] and r['owner'].casefold() == author.casefold()]
    minimum = options.get('auto_min_recordings', 5)
    if len(matches) < minimum or len(matches) / len(recent) < options.get('auto_min_match_ratio', .8):
        return False
    span = max(x['published'] for x in matches) - min(x['published'] for x in matches)
    return span >= options.get('auto_min_span_days', 30) * 86400


def discover(db, cfg, secrets, http, api):
    opts = cfg.get('sfw_expansion', {})
    if not opts.get('enabled', False):
        return []
    now = int(time.time())
    reports, posts_by_source = [], []
    creators = {c['reddit'].casefold(): dict(c) for c in opts.get('creators', [])}
    automatic = api.meta_get(db, 'sfw_auto_creators', {})
    creators.update({k: v for k, v in automatic.items() if k not in creators})
    evidence = api.meta_get(db, 'sfw_creator_evidence', {})
    added = []
    new_this_run = 0

    def report(name):
        r = {'name': name, 'status': 'healthy', 'parsed': 0, 'pages': 0,
             'accepted': 0, 'items': collections.Counter(), 'rejected': collections.Counter(),
             'categories': collections.Counter()}
        reports.append(r)
        return r

    def admit(stats, sid, creator, title, targets, published, categories, initial):
        nonlocal new_this_run
        key = api.canonical(targets[0][1])
        existing = api.lookup(db, key)
        counter_key = 'sfw_initial_count:' + creator['creator'].casefold()
        count = api.meta_get(db, counter_key, 0)
        if not existing and initial and count >= opts.get('initial_per_creator', 3):
            stats['items']['older_backlog_not_imported'] += 1
            return True
        if not existing and new_this_run >= opts.get('max_new_per_run', 12):
            stats['items']['deferred_by_run_limit'] += 1
            return False
        result = api.enqueue(db, sid, stats['name'], creator['creator'], title, targets, published)
        stats['accepted'] += 1
        stats['items'][result] += 1
        stats['categories'].update(categories)
        if result == 'queued':
            new_this_run += 1
            if initial:
                api.meta_set(db, counter_key, count + 1)
            api.meta_set(db, 'sfw_provenance:' + key, {'source_id': sid, 'categories': categories,
                         'evidence': 'recording_title_sfw' if ':soundgasm:' in sid else 'sfw_community_post'})
        return True

    reddit = api.Reddit(http, secrets.get('reddit', {}))
    for sub in opts.get('subreddits', []):
        stats = report('sfw:reddit:' + sub)
        posts, after = [], None
        try:
            for page in range(opts.get('reddit_pages', 3)):
                batch, after = reddit.page(sub, after)
                stats['pages'] += 1
                posts.extend(batch)
                if not after:
                    break
            stats['parsed'] = len(posts)
            stats['listing_window_full'] = bool(after)
            if not posts:
                stats['status'] = 'empty'
            for post in posts:
                author = post.get('author', '')
                targets = soundgasm_targets(post, api)
                if not targets or author in {'', '[deleted]', 'AutoModerator'}:
                    continue
                ok, reason, categories = qualify(post.get('title', ''), cfg, api)
                key = api.canonical(targets[0][1])
                evidence.setdefault(author.casefold(), {})[key] = {
                    'published': int(post.get('created_utc', 0)), 'owner': owner(key),
                    'eligible': ok and not post.get('over_18', False), 'post_id': post['id']}
        except (api.SourceError, api.requests.RequestException, ValueError, KeyError) as e:
            stats['status'] = 'unavailable'
            stats['error'] = str(e) if isinstance(e, api.SourceError) else type(e).__name__
        # A run's queue budget must not discard eligible Reddit recordings when
        # they leave the newest-post window before the next daily run.
        deferred = api.meta_get(db, 'sfw_reddit_deferred:' + stats['name'], {})
        deferred.update({post['id']: post for post in posts})
        posts_by_source.append((stats, list(deferred.values())))

    if opts.get('auto_add', False):
        for author, records in evidence.items():
            if (author in creators or len(added) >= opts.get('max_auto_additions_per_run', 2)
                    or len(creators) >= opts.get('max_creators', 20)):
                continue
            if auto_candidate(records, author, opts):
                qualifying = [r for r in records.values() if r['eligible'] and r['owner'].casefold() == author]
                profile = max(qualifying, key=lambda r: r['published'])['owner']
                c = {'reddit': author, 'soundgasm': profile, 'creator': profile,
                     'trusted_speaker': False, 'added_at': now, 'automatic': True}
                creators[author] = automatic[author] = c
                added.append(profile)
    # Bound candidate state, keeping the most recent 20 distinct recordings per author.
    evidence = {a: dict(sorted(records.items(), key=lambda x: x[1]['published'], reverse=True)[:20])
                for a, records in evidence.items()}
    if len(evidence) > 500:
        evidence = dict(sorted(evidence.items(), key=lambda x: max(r['published'] for r in x[1].values()), reverse=True)[:500])
    api.meta_set(db, 'sfw_creator_evidence', evidence)
    api.meta_set(db, 'sfw_auto_creators', automatic)

    for stats, posts in posts_by_source:
        deferred = {}
        for post in sorted(posts, key=lambda p: p.get('created_utc', 0), reverse=True):
            creator = creators.get(post.get('author', '').casefold())
            if not creator:
                stats['rejected']['creator_not_qualified'] += 1
                continue
            if post.get('over_18', False):
                stats['rejected']['marked_nsfw'] += 1
                continue
            ok, reason, categories = qualify(post.get('title', ''), cfg, api,
                                             trusted=creator.get('trusted_speaker', False))
            targets = [t for t in soundgasm_targets(post, api)
                       if owner(t[1]).casefold() == creator['soundgasm'].casefold()]
            if not ok or not targets:
                stats['rejected'][reason if not ok else 'no_verified_soundgasm_target'] += 1
                continue
            published = int(post.get('created_utc', 0))
            if not admit(stats, 'sfw:reddit:' + post['id'], creator, post['title'], targets,
                         published, categories, published <= creator['added_at']):
                deferred[post['id']] = {k: post[k] for k in
                    ('id', 'author', 'title', 'created_utc', 'over_18', 'url', 'url_overridden_by_dest', 'selftext') if k in post}
        api.meta_set(db, 'sfw_reddit_deferred:' + stats['name'], deferred)

    for creator in creators.values():
        stats = report('sfw:soundgasm:' + creator['soundgasm'])
        stats['creator'] = creator['creator']
        try:
            entries = api.sg_listing(http, creator['soundgasm'])
            stats['pages'] = 1
            stats['parsed'] = len(entries)
            checkpoint = 'sfw_sg_seen:' + creator['soundgasm'].casefold()
            previous = api.meta_get(db, checkpoint)
            seen, deferred = set(previous or []), set()
            initial_key = 'sfw_sg_initial:' + creator['soundgasm'].casefold()
            initial_urls = api.meta_get(db, initial_key)
            if initial_urls is None:
                initial_urls = [api.canonical(e['url']) for e in entries]
                api.meta_set(db, initial_key, initial_urls)
            if not entries:
                stats['status'] = 'empty'
            for entry in entries:
                key = api.canonical(entry['url'])
                if owner(key).casefold() != creator['soundgasm'].casefold():
                    stats['rejected']['profile_identity_mismatch'] += 1
                    continue
                if key in seen:
                    stats['items']['previously_seen'] += 1
                    continue
                ok, reason, categories = qualify(entry['title'], cfg, api, profile=True,
                                                 trusted=creator.get('trusted_speaker', False))
                if not ok:
                    stats['rejected'][reason] += 1
                    continue
                sid = 'sfw:soundgasm:' + hashlib.sha256(key.encode()).hexdigest()[:24]
                if not admit(stats, sid, creator, entry['title'], [['soundgasm', key]], 0,
                             categories, key in initial_urls):
                    deferred.add(key)
            api.meta_set(db, checkpoint, sorted((seen | {api.canonical(e['url']) for e in entries}) - deferred))
        except (api.SourceError, api.requests.RequestException, ValueError, KeyError) as e:
            stats['status'] = 'unavailable'
            stats['error'] = str(e) if isinstance(e, api.SourceError) else type(e).__name__
    if reports:
        reports[0]['automatically_added_creators'] = added
    return reports
