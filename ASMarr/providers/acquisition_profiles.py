"""Opt-in reusable text/discovery rules layered over the legacy defaults.

No custom fields means no change to the legacy speaker/topic decision or reason.
Terms are literal words/phrases, never executable regular expressions.
"""
import math
import re

TEXT_FIELDS = frozenset({'allowedSpeakers', 'allowedAudiences', 'requireSpeakerTag',
                        'trustedMissingSpeakerTag', 'requiredTopics', 'topicMatch', 'excludedTerms'})
DISCOVERY_FIELDS = TEXT_FIELDS | {'minimumDuration', 'backlogLimit', 'hypno_required',
                                 'hypno_keywords', 'fantasy_blocklist'}
TAG = re.compile(r'\[\s*([A-Z]{1,5})4([A-Z]{1,10})\s*\]', re.I)
AUDIENCE = re.compile(r'NB|F|M|A', re.I)
CODES = {'F', 'M', 'NB', 'A', 'ANY'}


def validate(settings):
    for key in ('allowedSpeakers', 'allowedAudiences'):
        if key in settings:
            value = settings[key]
            if not isinstance(value, list) or not value or any(not isinstance(x, str) or x.upper() not in CODES for x in value):
                raise ValueError('invalid_profile_' + key)
    for key in ('requiredTopics', 'excludedTerms'):
        if key in settings:
            value = settings[key]
            if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in value):
                raise ValueError('invalid_profile_' + key)
    for key in ('requireSpeakerTag', 'trustedMissingSpeakerTag'):
        if key in settings and type(settings[key]) is not bool:
            raise ValueError('invalid_profile_' + key)
    if not isinstance(settings.get('topicMatch','any'),str) or settings.get('topicMatch', 'any') not in {'any', 'all'}:
        raise ValueError('invalid_profile_topicMatch')
    for key in ('minimumDuration', 'backlogLimit'):
        if key in settings and (type(settings[key]) not in (int, float) or (type(settings[key]) is float and not math.isfinite(settings[key])) or settings[key] < 0):
            raise ValueError('invalid_profile_' + key)
    if 'backlogLimit' in settings and type(settings['backlogLimit']) is not int:
        raise ValueError('invalid_profile_backlogLimit')


def phrase_matches(text, term):
    pattern = r'(?<!\w)' + r'\s+'.join(re.escape(word) for word in term.split()) + r'(?!\w)'
    return re.search(pattern, text, re.I) is not None


def speaker_decision(text, settings, trusted=False):
    """None requests the exact legacy speaker policy; tuple is a custom decision."""
    fields = {'allowedSpeakers', 'allowedAudiences', 'requireSpeakerTag', 'trustedMissingSpeakerTag'}
    if not fields.intersection(settings):
        return None
    validate(settings)
    tags = TAG.findall(text)
    speakers = {x.upper() for x in settings.get('allowedSpeakers', ['F'])}
    audiences = {x.upper() for x in settings.get('allowedAudiences', ['M', 'A'])}
    # Contradictory explicit tags cannot be bypassed by a trusted source or an
    # additional allowed tag elsewhere in the title/description.
    for speaker, audience in tags:
        if speaker.upper() not in CODES-{'ANY'} or ('ANY' not in speakers and speaker.upper() not in speakers):
            return False, 'speaker_not_allowed'
        parts=AUDIENCE.findall(audience)
        if ''.join(parts).upper()!=audience.upper() or ('ANY' not in audiences and any(x.upper() not in audiences for x in parts)):
            return False, 'audience_not_allowed'
    if not tags and settings.get('requireSpeakerTag', True) and not (trusted and settings.get('trustedMissingSpeakerTag', True)):
        return False, 'missing_speaker_tag'
    return True, 'accepted'


def topic_decision(text, settings):
    """None requests legacy topic handling; an empty explicit list disables it."""
    if 'requiredTopics' not in settings:
        return None
    validate(settings)
    topics = settings['requiredTopics']
    matches = [phrase_matches(text, topic) for topic in topics]
    accepted = not topics or (all(matches) if settings.get('topicMatch', 'any') == 'all' else any(matches))
    return (True, 'accepted') if accepted else (False, 'missing_topic')


def excluded_decision(text, settings):
    validate(settings)
    for term in settings.get('excludedTerms', []):
        if phrase_matches(text, term):
            return False, 'excluded_term:' + term
    return True, 'accepted'


def discovery_config(base, settings):
    """Translate only profile fields relevant to polling/initial enrollment."""
    validate(settings)
    result = dict(base)
    result.update({key: value for key, value in settings.items() if key in DISCOVERY_FIELDS})
    if 'minimumDuration' in settings:
        result['youtube_min_duration_seconds'] = settings['minimumDuration']
    if 'backlogLimit' in settings:
        result['youtube_initial_downloads'] = settings['backlogLimit']
    return result
