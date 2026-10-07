"""Compact Plex titles; retain original descriptions separately."""
import re

# Ordered so negatives and specific variants are handled before general forms.
VARIANTS = [
    (r'\b(?:no|without)\s+(?:background\s+)?music\b', 'No music'),
    (r'\b(?:no|without)\s+(?:sfx|sound effects)\b', 'No SFX'),
    (r'\b(?:with\s+)?(?:background\s+)?music\s*(?:version|edit|only)?\b', 'Music'),
    (r'\b(?:with\s+)?(?:sfx|sound effects)\b', 'SFX'),
    (r'\b(?:raw\s+vocals?|vocals?\s+only|voice\s+only|raw\s+audio)\b', 'Vocal only'),
    (r'\b(?:centred|centered)\s+audio\b', 'Centred'),
    (r'\bbinaurals?\s*(?:version|edit)?\b', 'Binaural'),
    (r'\bsubliminals?\b', 'Subliminal'),
    (r'\bwhispered\b', 'Whispered'),
    (r'\blayered\s+voices?\b', 'Layered'),
    (r'\bremastered\s*(?:version|edit)?\b', 'Remastered'),
    (r'\b(?:updated|reworked)\s+version\b', 'Updated'),
    (r'\balternate\s+version\b', 'Alternate'),
    (r'\bextended\s*(?:version|edit)\b', 'Extended'),
    (r'\bfull\s+version\b', 'Full'),
    (r'\b(?:half\s+hour|30\s*min(?:ute)?s?)\s+loop\b', '30m loop'),
    (r'\b(?:one\s+hour|1\s*hour|60\s*min(?:ute)?s?)\s+loop\b', '1h loop'),
    (r'\bone\s+rotation\b', '1 cycle'),
    (r'\bno\s+wakener\b', 'No wakener'),
    (r'\bpmr\s+frac\s+induction\b', 'PMR induction'),
    (r'\bdark\s+version\b', 'Dark'),
]


def compact_title(original, limit=90):
    text = re.sub(r'\s+', ' ', original).strip()
    # Many imports concatenate a post title and a file title repeating the name.
    if ' — ' in text:
        left, right = text.rsplit(' — ', 1)
        name = re.split(r'[\[(]', left)[0].strip()
        if len(name) >= 5 and right.casefold().startswith(name.casefold()):
            text = right
        elif len(set(re.findall(r'\w+', left.casefold())) & set(re.findall(r'\w+', right.casefold()))) >= 2:
            text = right
    labels = []
    # Keep explicit edition labels, including qualifiers not in a fixed keyword list.
    for group in re.findall(r'\[([^\]]+)\]|\(([^)]+)\)', text):
        value = next(x for x in group if x)
        if re.search(r'\b(?:edit(?:ion)?|version|mix|v\d+|p\d+|no wake|wake up|live discord|longer version)\b', value, re.I):
            label = re.sub(r'\b(?:edit(?:ion)?|version)\b', '', value, flags=re.I)
            label = re.sub(r'\s+', ' ', label).strip().title()
            label = re.sub(r'\b(Yt|Hfo|Sfx|F4m|F4a|F4f)\b', lambda m:m.group().upper(), label)
            if label and len(label) <= 42:
                labels.append(label)
                text = text.replace('['+value+']','').replace('('+value+')','')
    audience = re.search(r'\[\s*((?:F)?4[MAF]+)\s*(?:version)?\s*\]', text, re.I)
    if audience:
        labels.append(audience.group(1).upper())
    # Preserve these distinctions when imported file titles identify a variant.
    if re.search(r'\bfractionation\s+vocal\s+only\b', text, re.I):
        labels.append('Fractionation')
        text = re.sub(r'\bfractionation(?=\s+vocal\s+only)', '', text, flags=re.I)
    if re.search(r'\btraining\s+vocal\s+only\b', text, re.I):
        labels.append('Training')
        text = re.sub(r'\btraining(?=\s+vocal\s+only)', '', text, flags=re.I)
    for pattern, label in VARIANTS:
        if re.search(pattern, text, re.I):
            if label not in labels:
                labels.append(label)
            text = re.sub(pattern, '', text, flags=re.I)
    text = re.sub(r'\[[^\]]*\]', '', text)
    # Parenthetical descriptions belong in notes; format labels were extracted above.
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\bHypNovember\s+Day\s+\d+\s*\w*', '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip(' -—_:,')
    text = re.sub(r'\s+[-—:]\s*$', '', text).strip() or re.sub(r'\s+', ' ', original).strip()
    prefix = ('[' + ' · '.join(labels) + '] ') if labels else ''
    # Always keep the version labels ahead of long descriptive titles.
    available = max(20, limit - len(prefix))
    if len(text) > available:
        shortened = text[:available - 1].rsplit(' ', 1)[0]
        text = (shortened or text[:available - 1]).rstrip(' -—,:') + '…'
    return prefix + text, labels


def library_titles(rows):
    """Prevent compaction from hiding distinctions between different source titles."""
    import collections
    plans = [dict(r, new_title=compact_title(r['title'])[0], versions=compact_title(r['title'])[1]) for r in rows]
    groups = collections.defaultdict(list)
    for p in plans:
        groups[(p['artist'], p['new_title'])].append(p)
    for group in groups.values():
        originals = {p['title'] for p in group}
        if len(originals) < 2:
            continue
        for p in group:
            clauses = [a or b for a,b in re.findall(r'\[([^\]]+)\]|\(([^)]+)\)',p['title'])]
            unique = [c for c in clauses if all(c not in other for other in originals if other != p['title'])]
            if unique:
                label = min(unique,key=len)
                label = re.sub(r'\s+', ' ', label).strip()
            else:
                # If another variant adds a qualifier, this is the unqualified source.
                label = 'Original'
            if len(label) > 36:
                label = label[:35].rsplit(' ',1)[0] + '…'
            base = p['new_title']
            budget = 90 - len(label) - 3
            if len(base)>budget:
                base=base[:budget-1].rsplit(' ',1)[0]+'…'
            p['new_title']='['+label+'] '+base
            p['versions']=[label]+p['versions']
    # Residual ambiguity must be reviewed, not silently pushed into Plex.
    final=collections.defaultdict(set)
    for p in plans:final[(p['artist'],p['new_title'])].add(p['title'])
    collisions=[k for k,v in final.items() if len(v)>1]
    if collisions:
        raise ValueError('Ambiguous compact titles: '+repr(collisions))
    return plans
