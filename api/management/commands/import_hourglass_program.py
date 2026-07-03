"""Import the weekend public-talk program from an Hourglass .docx export.

Hourglass's JSON export carries persons/privileges/reports but NOT the meeting
program; the program only leaves Hourglass as a Word document ("Tout pwogram
ansanm"). That document is a well-structured table per meeting, so this command
parses it directly (stdlib zipfile + regex — no docx dependency):

  Dimanch, 12 Jiyè 2026 …
  Diskou piblik
  10:20 AM | Fason pou nou pran bon desizyon (30 min.) | Mondésir, Blondel (CONG)

For each Sunday it extracts the talk TITLE and speaker, resolves the title to
its S-34 talk NUMBER against the jwlinker corpus (titles are '<n>. <title>' in
every language there), resolves the speaker to a person_uid when the persons
table has them, and merges client-complete rows (cloned from the frontend's
schedule/source schema defaults in api/data/week_schemas.json) into the
'schedules' and 'sources' backup tables. Metadata versions are bumped so the
PWA pulls the rows on its next sync; existing weeks are updated in place, other
fields untouched.

Usage:
  manage.py import_hourglass_program <file.docx> [--dry-run] [--language 51]
"""

import copy
import html
import json
import re
import sqlite3
import unicodedata
import zipfile
from datetime import date, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

# Haitian Creole month names as Hourglass prints them.
MONTHS = {
    'janvye': 1, 'fevriye': 2, 'mas': 3, 'avril': 4, 'me': 5, 'jen': 6,
    'jiyè': 7, 'jiye': 7, 'out': 8, 'septanm': 9, 'oktòb': 10, 'oktob': 10,
    'novanm': 11, 'desanm': 12,
}

JWLINKER_DB = Path('/library/jwlinker/jw_library.db')
SCHEMAS = Path(__file__).resolve().parents[2] / 'data' / 'week_schemas.json'


def _fold(s):
    """Casefold + strip accents for tolerant matching."""
    s = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in s if not unicodedata.combining(c)).casefold().strip()


def _docx_rows(path):
    """Yield table rows (list of cell texts) from the document."""
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    for tr in re.findall(r'<w:tr[ >].*?</w:tr>', xml, re.S):
        cells = []
        for tc in re.findall(r'<w:tc[ >].*?</w:tc>', tr, re.S):
            text = re.sub(r'</w:p>', '\n', tc)
            text = html.unescape(re.sub(r'<[^>]+>', '', text))
            cells.append(text.strip())
        if any(cells):
            yield cells


def _parse_date(line):
    """'Dimanch, 12 Jiyè 2026' -> date(2026, 7, 12), or None."""
    m = re.search(r',\s*(\d{1,2})\s+(\S+)\s+(\d{4})', line)
    if not m:
        return None
    month = MONTHS.get(_fold(m.group(2)))
    if not month:
        return None
    return date(int(m.group(3)), month, int(m.group(1)))


def parse_program(path):
    """Extract [{date, title, speaker, speaker_cong}] for each Sunday talk."""
    talks = []
    current_date = None
    in_public_talk = False

    for cells in _docx_rows(path):
        joined = ' '.join(cells)
        d = _parse_date(cells[0]) if cells else None
        if d:
            current_date = d
            in_public_talk = False
            continue
        if re.search(r'Diskou piblik', joined, re.I):
            in_public_talk = True
            continue
        if in_public_talk and current_date and current_date.weekday() == 6:
            # First row after the header: [time, 'TITLE (30 min.)', 'Speaker (CONG)']
            body = [c for c in cells if c and not re.match(r'^\d{1,2}:\d{2}', c)]
            if len(body) >= 2:
                title = re.sub(r'\s*\(\d+\s*min\.?\)\s*$', '', body[0]).strip()
                speaker_raw = body[1].strip()
                m = re.match(r'(.+?)\s*\(([^)]*)\)\s*$', speaker_raw)
                speaker = (m.group(1) if m else speaker_raw).strip()
                cong = (m.group(2) if m else '').strip()
                talks.append({
                    'date': current_date, 'title': title,
                    'speaker': speaker, 'speaker_cong': cong,
                })
            in_public_talk = False
    return talks


def resolve_talk_number(title, lang_id='51'):
    """Match a talk title to its S-34 number via the jwlinker corpus."""
    if not JWLINKER_DB.exists():
        return None
    conn = sqlite3.connect(str(JWLINKER_DB))
    rows = conn.execute(
        "SELECT DISTINCT t.name FROM Topics t "
        "JOIN Categories c ON t.category_id=c.id "
        "JOIN Publications p ON c.publication_id=p.id "
        "WHERE p.code='s34' AND p.language=?", (lang_id,)
    ).fetchall()
    conn.close()

    want = _fold(title)
    best = None
    for (name,) in rows:
        # The corpus holds two naming formats: '103. Title' and 'No 78 Title'.
        m = re.match(r'(?:No\s+)?(\d+)[.\s]\s*(.*)', name)
        if not m:
            continue
        num, corpus_title = int(m.group(1)), _fold(m.group(2))
        if corpus_title == want:
            return num
        if want and (want in corpus_title or corpus_title in want):
            best = best or num
    return best


def resolve_speaker_uid(cong, display):
    """person_uid for a display name; tolerates 'Last, First' / 'First Last'."""
    from api.models import CongBackupTable

    names = {}
    try:
        persons_bt = CongBackupTable.objects.get(
            congregation=cong, table_name='persons')
        for p in (persons_bt.data or []):
            if isinstance(p, dict):
                pd = p.get('person_data', {})
                uid = p.get('person_uid', '')
                dn = pd.get('person_display_name', {}).get('value', '')
                first = pd.get('person_firstname', {}).get('value', '')
                last = pd.get('person_lastname', {}).get('value', '')
                if not uid:
                    continue
                if dn:
                    names[_fold(dn)] = uid
                if first or last:
                    names[_fold(f"{last}, {first}")] = uid
                    names[_fold(f"{first} {last}")] = uid
    except CongBackupTable.DoesNotExist:
        pass

    key = _fold(display)
    if key in names:
        return names[key]
    parts = [x.strip() for x in (display or '').split(',')]
    if len(parts) == 2:
        return names.get(_fold(f"{parts[1]} {parts[0]}"), '')
    return ''


def write_weekend_program(cong, resolved):
    """Merge resolved weekend talks into the schedules + sources tables.

    `resolved` entries carry week_of / talk_number / talk_type / speaker /
    speaker_uid. Rows are client-complete (cloned from the frontend schema
    defaults) and merged by weekOf; Metadata versions are bumped so the PWA
    pulls the change on its next sync. Shared by the docx command and the
    unified-JSON import.
    """
    from django.utils import timezone

    from api.models import CongBackupTable, Metadata

    templates = json.loads(SCHEMAS.read_text())
    now_iso = timezone.now().isoformat()

    def build_schedule(entry, talk):
        wm = entry.setdefault('weekend_meeting', {})
        wm.setdefault('speaker', {}).setdefault('part_1', [{}])
        wm['speaker']['part_1'][0] = {
            'type': 'main', 'value': talk['speaker_uid'],
            'name': talk['speaker'], 'updatedAt': now_iso,
        }
        wm.setdefault('public_talk_type', [{}])
        wm['public_talk_type'][0] = {
            'type': 'main', 'value': talk['talk_type'], 'updatedAt': now_iso,
        }

    def build_source(entry, talk):
        wm = entry.setdefault('weekend_meeting', {})
        wm.setdefault('public_talk', [{}])
        wm['public_talk'][0] = {
            'type': 'main',
            'value': talk['talk_number'] if talk['talk_number'] else '',
            'updatedAt': now_iso,
        }

    for table_name, build in (('schedules', build_schedule),
                              ('sources', build_source)):
        bt, _ = CongBackupTable.objects.get_or_create(
            congregation=cong, table_name=table_name, defaults={'data': []})
        data = bt.data if isinstance(bt.data, list) else []
        index = {e.get('weekOf'): i for i, e in enumerate(data)
                 if isinstance(e, dict)}
        for talk in resolved:
            wk = talk['week_of']
            if wk in index:
                entry = data[index[wk]]
            else:
                entry = copy.deepcopy(templates[
                    'schedule' if table_name == 'schedules' else 'source'])
                entry['weekOf'] = wk
                data.append(entry)
            build(entry, talk)
        bt.data = data
        bt.save()
        Metadata.objects.update_or_create(
            congregation=cong, cong_user=None, key=table_name,
            defaults={'value': str(int(timezone.now().timestamp() * 1000))})


def write_midweek_program(cong, meetings):
    """Merge midweek assignments (unified-JSON shape) into the schedules table.

    `meetings` come from fsr's docx parser via the unified JSON:
    [{week_of, parts: [{person, part_type, assignment, school}]}] with NWS
    part-type names. Only ASSIGNMENTS are written — the app renders midweek
    part titles from the self-hosted source-materials mirror, so schedules
    only need who does what. Rows are merged by weekOf (a weekend import for
    the same week shares the entry); persons resolve to uids where known,
    with the display name kept alongside. Returns the number of assignment
    slots written.
    """
    import re as _re

    from django.utils import timezone

    from api.models import CongBackupTable, Metadata

    templates = json.loads(SCHEMAS.read_text())
    now_iso = timezone.now().isoformat()

    bt, _ = CongBackupTable.objects.get_or_create(
        congregation=cong, table_name='schedules', defaults={'data': []})
    data = bt.data if isinstance(bt.data, list) else []
    index = {e.get('weekOf'): i for i, e in enumerate(data)
             if isinstance(e, dict)}

    written = 0
    for meeting in meetings:
        wk = meeting.get('week_of', '')
        if not wk:
            continue
        if wk in index:
            entry = data[index[wk]]
        else:
            entry = copy.deepcopy(templates['schedule'])
            entry['weekOf'] = wk
            data.append(entry)
            index[wk] = len(data) - 1

        # Entries cloned from the template are complete; older rows may not
        # be — repair any missing midweek branch from the template.
        template_mw = copy.deepcopy(templates['schedule']['midweek_meeting'])
        mw = entry.setdefault('midweek_meeting', {})
        for key, value in template_mw.items():
            mw.setdefault(key, value)

        def slot(person):
            return {'type': 'main',
                    'value': resolve_speaker_uid(cong, person),
                    'name': person, 'updatedAt': now_iso}

        for part in (meeting.get('parts') or []):
            pt = part.get('part_type', '')
            person = part.get('person', '')
            school = part.get('school', 1)
            if not person:
                continue

            if pt == 'Chairman':
                mw['chairman']['main_hall'][0] = slot(person)
            elif pt == 'AuxiliaryClassroomCounselor':
                mw['chairman']['aux_class_1'] = slot(person)
            elif pt == 'OpeningPrayer':
                mw['opening_prayer'][0] = slot(person)
            elif pt == 'ClosingPrayer':
                mw['closing_prayer'][0] = slot(person)
            elif pt == 'TreasuresTalk':
                mw['tgw_talk'][0] = slot(person)
            elif pt == 'SpiritualGems':
                mw['tgw_gems'][0] = slot(person)
            elif pt == 'BibleReading':
                if school == 1:
                    mw['tgw_bible_reading']['main_hall'][0] = slot(person)
                else:
                    mw['tgw_bible_reading']['aux_class_1'] = slot(person)
            elif pt == 'CBS':
                mw['lc_cbs']['conductor'][0] = slot(person)
            elif pt == 'CBSReader':
                mw['lc_cbs']['reader'][0] = slot(person)
            else:
                m = _re.match(r'Apply(\d+)(Assistant)?$', pt)
                if m and 1 <= int(m.group(1)) <= 4:
                    role = 'assistant' if m.group(2) else 'student'
                    part_key = f"ayf_part{m.group(1)}"
                    if school == 1:
                        mw[part_key]['main_hall'][role][0] = slot(person)
                    else:
                        mw[part_key]['aux_class_1'][role] = slot(person)
                else:
                    m = _re.match(r'Living(\d+)$', pt)
                    if m and 1 <= int(m.group(1)) <= 3 and school == 1:
                        mw[f"lc_part{m.group(1)}"][0] = slot(person)
                    else:
                        continue  # unknown/unmappable part — skip
            written += 1

    bt.data = data
    bt.save()
    Metadata.objects.update_or_create(
        congregation=cong, cong_user=None, key='schedules',
        defaults={'value': str(int(timezone.now().timestamp() * 1000))})
    return written


class Command(BaseCommand):
    help = "Import the weekend public-talk program from an Hourglass docx"

    def add_arguments(self, parser):
        parser.add_argument('docx', help='Path to the Hourglass program .docx')
        parser.add_argument('--dry-run', action='store_true',
                            help='Parse and resolve only; write nothing')
        parser.add_argument('--language', default='51',
                            help='jwlinker MEPS language id for title matching (default 51 = Creole)')

    def handle(self, *args, **opts):
        from api.models import Congregation

        path = Path(opts['docx'])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        cong = Congregation.objects.first()
        if cong is None:
            raise CommandError("No congregation exists yet")

        talks = parse_program(path)
        if not talks:
            raise CommandError(
                "No Sunday public talks found — is this the Hourglass "
                "'all programs' export?"
            )

        own = _fold(cong.cong_name)
        resolved = []
        for t in talks:
            number = resolve_talk_number(t['title'], opts['language'])
            monday = t['date'] - timedelta(days=t['date'].weekday())
            week_of = monday.strftime('%Y/%m/%d')
            local = not t['speaker_cong'] or _fold(t['speaker_cong']) == own
            resolved.append({
                **t, 'talk_number': number, 'week_of': week_of,
                'talk_type': 'localSpeaker' if local else 'visitingSpeaker',
                'speaker_uid': resolve_speaker_uid(cong, t['speaker']),
            })
            flag = '' if number else '  ⚠ title not matched in corpus'
            self.stdout.write(
                f"{week_of}  No {number or '?':>3}  {t['title']}  — "
                f"{t['speaker']} [{t['speaker_cong'] or 'local'}]"
                f"{' uid=' + resolved[-1]['speaker_uid'] if resolved[-1]['speaker_uid'] else ''}{flag}"
            )

        if opts['dry_run']:
            self.stdout.write(self.style.SUCCESS(
                f"Dry run: {len(resolved)} talks parsed, nothing written"))
            return

        write_weekend_program(cong, resolved)

        matched = sum(1 for t in resolved if t['talk_number'])
        self.stdout.write(self.style.SUCCESS(
            f"Imported {len(resolved)} weekend talks "
            f"({matched} matched to S-34 numbers) into schedules + sources"))
