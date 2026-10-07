"""Non-exclusive ASMR themes inferred from a recording's own metadata."""
import html
import re


CATEGORIES = {
    'sleep': ('Sleep', r'\b(?:sleep(?:y|ing|aid)?|sleep[ -]aid|asleep|bedtime|pillow[ -]?talk|insomnia)\b'),
    'relaxation': ('Relaxation', r'\b(?:relax(?:ation|ing)?|calm(?:ing)?|unwind|de[ -]?stress|stress[ -]relief|soothing)\b'),
    'personal_attention': ('Personal Attention', r'\b(?:personal[ -]attention|hair[ -]?(?:brushing|playing|cut)|make[ -]?up|skincare|spa|massage|face[ -]?(?:touching|brushing))\b'),
    'comfort': ('Comfort & Reassurance', r'\b(?:comfort(?:s|ing)?|reassur\w*|emotional[ -]support|cuddl\w*|anxiety[ -]relief|you(?:\x27re| are) safe)\b'),
    'affirmations': ('Affirmations', r'\b(?:affirmations?|positive[ -]thinking|self[ -](?:esteem|worth|love)|you are enough)\b'),
    'hypnosis': ('Hypnosis', r'\b(?:hypno(?:sis|tic|tized|tised)?|trance|induction|fractionation|mesmeri[sz]\w*)\b'),
    'meditation': ('Meditation', r'\b(?:meditat(?:ion|ing|ive)|mindfulness|body[ -]scan|guided[ -]visuali[sz]ation)\b'),
    'breathing': ('Guided Breathing', r'\b(?:breath(?:ing|work)|breathe|box[ -]breathing)\b'),
    'whispering': ('Whispering', r'\bwhisper(?:s|ed|ing)?\b'),
    'soft_spoken': ('Soft Spoken', r'\bsoft[ -]?(?:spoken|speaking|voice)\b'),
    'nature': ('Rain & Nature', r'\b(?:rain(?:fall|y)?|thunder(?:storm)?|ocean|waves|forest[ -]sounds|nature[ -]sounds|birdsong|fireplace)\b'),
    'triggers': ('Tapping & Brushing', r'\b(?:tapping|brushing|scratching|crinkling|page[ -]turning|keyboard[ -]sounds|trigger[ -]assortment)\b'),
    'no_talking': ('No Talking', r'\b(?:no[ -]talking|non[ -]?verbal|without[ -](?:talking|speech)|wordless)\b'),
    'stories': ('Stories & Roleplay', r'\b(?:role[ -]?play|bedtime[ -]stor(?:y|ies)|storytelling|reading[ -](?:to you|a book)|audiobook)\b'),
    'binaural': ('Binaural', r'\b(?:binaural|ear[ -]to[ -]ear|3d[ -](?:audio|sound))\b'),
}


def classify(text):
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text or ''))
    text = text.replace('\u2019', "'").replace('_', ' ')
    found = []
    for key, (_, pattern) in CATEGORIES.items():
        for match in re.finditer(pattern, text, re.I):
            # A version tag such as "no hypnosis" must not claim that theme.
            if key != 'no_talking' and re.search(r'\b(?:no|without|non)[ -]*$', text[max(0, match.start()-16):match.start()], re.I):
                continue
            found.append(key)
            break
    return found


def metadata_text(track, original_titles=()):
    pieces = [track.get('title', ''), track.get('originalTitle', ''), *original_titles]
    summary = track.get('summary', '')
    # Prefer the preserved source title; never infer a track's themes from a creator bio.
    match = re.search(r'Original title:\s*([^\n]+)', summary, re.I)
    if match:
        pieces.append(match.group(1))
    for tag in ('Genre', 'Mood', 'Style', 'Label'):
        pieces.extend(x.get('tag', '') for x in track.findall(tag)
                      if not x.get('tag', '').startswith('ASMR: '))
    return ' '.join(pieces)
