"""One-shot Hourglass → Organized (+ New World Scheduler) import.

Hourglass's data leaves it in two artifacts: a JSON export (congregation,
publishers, privileges, reports, attendance, groups) and a Word "Tout pwogram
ansanm" document (the meeting program, which the JSON does not contain). This
command ingests BOTH in one shot, server-side — no browser required:

  1. Discover the newest Hourglass JSON and program .docx (drop directory,
     default /library/hourglass, or explicit paths).
  2. Migrate persons, meeting attendance, field service groups, branch and
     congregation field-service reports into the Organized backup tables.
     The mappings are a faithful port of the PWA's own Hourglass importer
     (organized-app useImportHourglass.jsx), so the rows are exactly what the
     app would have produced itself.
  3. Import the weekend public-talk program from the docx (delegates to
     import_hourglass_program's parser) and backfill speaker person_uids now
     that persons are known.
  4. Bump per-table Metadata so the PWA pulls everything on its next sync.
  5. Optionally (--nws-out DIR) emit the New World Scheduler field-service CSV
     in the exact format the fsr tool produces
     (Date,FirstName,LastName,SharedInMinistry,BibleStudies,AP,Hours,Credit,Remarks).

Usage:
  manage.py import_hourglass                       # discover in /library/hourglass
  manage.py import_hourglass --dir /some/dir --dry-run
  manage.py import_hourglass export.json program.docx --nws-out /library/hourglass/nws


RESCUE PATH ONLY: the canonical import is the Organized PWA
(Congregation settings -> Import), which consumes fsr's unified JSON
in the browser and writes through the normal sync path. Use this
command only when no browser is available (headless restore,
automation experiments); expect it to lag the PWA importer.
"""

import csv
import json
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

DEFAULT_DIR = Path('/library/hourglass')

# Hourglass privilege key -> Organized assignment code(s), verbatim from
# the PWA's useImportHourglass.jsx.
PRIVILEGE_ASSIGNMENTS = {
    'reading': [100], 'initcall': [123], 'rv': [124], 'study': [125, 126],
    'stutalk': [104], 'fm_discussion': [127], 'aux_chairman': [128],
    'cbs': [115], 'cbs_reader': [116], 'mm_chairman': [110],
    'prayer': [111, 119], 'dfg': [113], 'lac': [114], 'pt': [120],
    'treasures': [112], 'wm_chairman': [118], 'wt_conductor': [130],
    'wm_reader': [122],
}


def _now_iso():
    """JS-style ISO timestamp ('...sssZ') — what the client writes itself."""
    return datetime.now(dt_timezone.utc).isoformat(timespec='milliseconds') \
        .replace('+00:00', 'Z')


def _fold(s):
    s = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in s if not unicodedata.combining(c)).casefold().strip()


def _display_name(lastname, firstname):
    """'Dupont', 'Jean' -> 'D. Jean' (PWA's generateDisplayName)."""
    if not lastname:
        return firstname
    if not firstname:
        return lastname
    initials = ' '.join(f"{p[0]}." for p in lastname.split(' ') if p)
    return f"{initials} {firstname}"


def _is_hourglass_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            head = json.load(f)
        return isinstance(head, dict) and 'publishers' in head
    except (ValueError, OSError):
        return False


def discover(directory):
    """Newest Hourglass JSON and newest .docx in the drop directory."""
    directory = Path(directory)
    if not directory.is_dir():
        return None, None
    jsons = sorted(
        (p for p in directory.glob('*.json') if _is_hourglass_json(p)),
        key=lambda p: p.stat().st_mtime, reverse=True)
    docs = sorted(directory.glob('*.docx'),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return (jsons[0] if jsons else None), (docs[0] if docs else None)


# ── PWA-migration ports ─────────────────────────────────────────────────────

def migrate_persons(data):
    now = _now_iso()
    publishers = list(data.get('publishers') or [])
    publishers += list(data.get('notPublishers') or [])
    privileges_map = data.get('privileges') or {}
    addresses = {a.get('id'): a for a in (data.get('addresses') or [])}
    reports = data.get('reports') or []

    persons = []
    for rec in publishers:
        phone = rec.get('cellphone') or ''
        for key in ('homephone', 'otherphone'):
            if rec.get(key):
                phone = f"{phone}; {rec[key]}" if phone else rec[key]

        status = rec.get('status')
        is_unbaptized = status == 'Unbaptized Publisher'
        is_student = status == 'StudentOnly'
        is_baptized = status in (
            'Baptized Publisher', 'Continuous Auxiliary Pioneer',
            'Regular Pioneer', 'Special Pioneer')

        assignments = []
        for priv_key, codes in PRIVILEGE_ASSIGNMENTS.items():
            if rec.get('id') in (privileges_map.get(priv_key) or []):
                assignments.extend(codes)

        privileges = []
        if rec.get('appt') == 'Elder':
            privileges.append('elder')
        if rec.get('appt') == 'MS':
            privileges.append('ms')

        pub_reports = sorted(
            (r for r in reports if (r.get('user') or {}).get('id') == rec.get('id')),
            key=lambda r: f"{r.get('year')}/{str(r.get('month')).zfill(2)}")

        # Pioneer enrollment history (faithful port, incl. the PWA's
        # skip-last-report loop bound).
        enrollments, lock, enrollment, start = [], False, '', ''
        for r in pub_reports[:-1]:
            pio = r.get('pioneer')
            if pio in ('Auxiliary', 'Regular', 'Special'):
                if not lock:
                    lock = True
                    enrollment = {'Special': 'SP', 'Regular': 'FR'}.get(pio, 'AP')
                    start = date(r['year'], r['month'], 1).strftime('%Y/%m/%d')
            if pio is None:
                if lock:
                    first = date(r['year'], r['month'], 1)
                    end = first - timedelta(days=1)
                    enrollments.append({
                        'id': str(uuid.uuid4()), 'enrollment': enrollment,
                        'updatedAt': now, '_deleted': False,
                        'start_date': start, 'end_date': end.strftime('%Y/%m/%d'),
                    })
                lock, enrollment, start = False, '', ''
        if lock:
            enrollments.append({
                'id': str(uuid.uuid4()), 'enrollment': enrollment,
                'updatedAt': now, '_deleted': False,
                'start_date': start, 'end_date': None,
            })

        first_report = None
        if pub_reports:
            r0 = pub_reports[0]
            first_report = f"{r0['year']}/{str(r0['month']).zfill(2)}/01"
        if any((r.get('credithours') or 0) > 0 for r in pub_reports):
            assignments.append(300)

        def history(active):
            if not active or not first_report:
                return []
            return [{'id': str(uuid.uuid4()), '_deleted': False,
                     'updatedAt': now, 'start_date': first_report,
                     'end_date': None}]

        baptism = rec.get('baptism')
        birth = rec.get('birth')
        address = (addresses.get(rec.get('address_id')) or {}).get('line1') \
            if rec.get('address_id') is not None else None

        contacts = []
        for c in (rec.get('emergencycontacts') or []):
            joined = c.get('email') or ''
            for key in ('cellphone', 'homephone', 'otherphone'):
                if c.get(key):
                    joined = f"{joined}; {c[key]}" if joined else c[key]
            contacts.append({'id': str(uuid.uuid4()), 'updatedAt': now,
                             '_deleted': False, 'name': c.get('name', ''),
                             'contact': joined})

        persons.append({
            '_deleted': {'value': False, 'updatedAt': now},
            'person_uid': rec.get('uuid') or str(uuid.uuid4()),
            'person_data': {
                'disqualified': {'value': False, 'updatedAt': now},
                'female': {'value': rec.get('sex') == 'Female', 'updatedAt': now},
                'male': {'value': rec.get('sex') == 'Male', 'updatedAt': now},
                'archived': {'value': False, 'updatedAt': now},
                'person_firstname': {'value': rec.get('firstname', ''), 'updatedAt': now},
                'person_lastname': {'value': rec.get('lastname', ''), 'updatedAt': now},
                'person_display_name': {
                    'value': _display_name(rec.get('lastname', ''), rec.get('firstname', '')),
                    'updatedAt': now},
                'birth_date': {'value': birth, 'updatedAt': now},
                'address': {'value': address, 'updatedAt': now},
                'email': {'value': rec.get('email') or '', 'updatedAt': now},
                'phone': {'value': phone, 'updatedAt': now},
                'publisher_baptized': {
                    'active': {'value': is_baptized, 'updatedAt': now},
                    'baptism_date': {
                        'value': baptism.replace('-', '/') if isinstance(baptism, str) else None,
                        'updatedAt': now},
                    'other_sheep': {'value': not rec.get('anointed'), 'updatedAt': now},
                    'anointed': {'value': bool(rec.get('anointed')), 'updatedAt': now},
                    'history': history(is_baptized),
                },
                'publisher_unbaptized': {
                    'active': {'value': is_unbaptized, 'updatedAt': now},
                    'history': history(is_unbaptized),
                },
                'midweek_meeting_student': {
                    'active': {'value': is_student, 'updatedAt': now},
                    'history': [{'id': str(uuid.uuid4()), '_deleted': False,
                                 'updatedAt': now,
                                 'start_date': date.today().strftime('%Y/%m/%d'),
                                 'end_date': None}] if is_student else [],
                },
                'timeAway': [],
                'assignments': [{'type': 'main', 'updatedAt': now,
                                 'values': assignments}],
                'privileges': [
                    {'id': str(uuid.uuid4()), 'privilege': p, 'updatedAt': now,
                     '_deleted': False, 'start_date': first_report,
                     'end_date': None} for p in privileges],
                'enrollments': enrollments,
                'emergency_contacts': contacts,
                'categories': ['main'],
                'first_report': {'value': first_report, 'updatedAt': now},
            },
        })
    return persons


def migrate_attendance(data):
    now = _now_iso()
    out = []
    for att in ((data.get('attendance') or {}).get('attendance') or []):
        entry = {
            '_deleted': {'value': False, 'updatedAt': now},
            'month_date': (att.get('month') or '').replace('-', '/', 1),
        }
        for week in range(1, 6):
            week_data = {}
            for kind, prefix in (('midweek', 'mw'), ('weekend', 'we')):
                slot = {'type': 'main', 'updatedAt': now}
                count = att.get(f'{prefix}{week}')
                if count:  # PWA sends `undefined` (key absent) when falsy
                    slot['present'] = count
                week_data[kind] = [slot]
            entry[f'week_{week}'] = week_data
        out.append(entry)
    return out


def migrate_groups(data):
    now = _now_iso()
    publishers = data.get('publishers') or []
    groups = data.get('fsGroups') or []
    out = []
    for idx, group in enumerate(groups):
        members = []
        overseer = next((p for p in publishers
                         if p.get('group_id') == group.get('id')
                         and p.get('id') == group.get('overseer_id')), None)
        assistant = next((p for p in publishers
                          if p.get('group_id') == group.get('id')
                          and p.get('id') == group.get('assistant_id')), None)
        if overseer:
            members.append({'isAssistant': False, 'isOverseer': True,
                            'person_uid': overseer.get('uuid'), 'sort_index': 0})
        if assistant:
            members.append({'isAssistant': True, 'isOverseer': False,
                            'person_uid': assistant.get('uuid'), 'sort_index': 1})
        seen = {m['person_uid'] for m in members}
        others = [{'isAssistant': False, 'isOverseer': False,
                   'person_uid': p.get('uuid')}
                  for p in publishers
                  if p.get('group_id') == group.get('id')
                  and p.get('uuid') not in seen]
        members += others

        start_sort = 2 if (not overseer or not assistant) else 0
        final = []
        for m in members:
            if m.get('sort_index'):  # PWA quirk: 0 is falsy, gets recomputed
                final.append(m)
            else:
                final.append({**m, 'sort_index':
                              members.index(m) + start_sort})
        out.append({
            'group_id': str(uuid.uuid4()),
            'group_data': {'_deleted': False, 'updatedAt': now, 'name': '',
                           'sort_index': idx, 'members': final},
        })
    return out


def migrate_branch_reports(data):
    now = _now_iso()
    attendance = ((data.get('attendance') or {}).get('attendance') or [])
    out = []
    for rep in (data.get('monthlyTotals') or []):
        month = rep.get('month', '')
        we_avg = next((a.get('weAvg') for a in attendance
                       if a.get('month') == month), 0) or 0
        pub = rep.get('pub') or {}
        aux = rep.get('aux') or {}
        reg = rep.get('reg') or {}
        out.append({
            'report_date': month.replace('-', '/', 1),
            'report_data': {
                '_deleted': False, 'updatedAt': now,
                'submitted': pub.get('createdAt') is not None,
                'publishers_active': pub.get('active', 0),
                'weekend_meeting_average': we_avg,
                'publishers': {'report_count': pub.get('count', 0),
                               'bible_studies': pub.get('studies', 0)},
                'APs': {'report_count': aux.get('count', 0),
                        'hours': aux.get('hours', 0),
                        'bible_studies': aux.get('studies', 0)},
                'FRs': {'report_count': reg.get('count', 0),
                        'hours': reg.get('hours', 0),
                        'bible_studies': reg.get('studies', 0)},
            },
        })
    return out


def _report_hours(rep):
    """Hourglass exports carry minutes_as_hours (PWA path) or minutes (fsr)."""
    if rep.get('minutes_as_hours') is not None:
        try:
            return round(float(rep['minutes_as_hours']))
        except (TypeError, ValueError):
            return 0
    try:
        return int(rep.get('minutes') or 0) // 60
    except (TypeError, ValueError):
        return 0


def migrate_cong_reports(data):
    now = _now_iso()
    publishers = list(data.get('publishers') or []) + list(data.get('notPublishers') or [])
    by_id = {p.get('id'): p for p in publishers}
    out = []
    for rep in (data.get('reports') or []):
        person = by_id.get((rep.get('user') or {}).get('id'))
        if not person:
            continue
        month = f"{rep.get('year')}/{str(rep.get('month')).zfill(2)}"
        submitted = (rep.get('submitted_month') or '').replace('-', '/', 1)
        hours = _report_hours(rep)
        out.append({
            'report_id': str(uuid.uuid4()),
            'report_data': {
                '_deleted': False, 'updatedAt': now,
                'report_date': month,
                'person_uid': person.get('uuid'),
                'shared_ministry': hours > 0
                    or (rep.get('credithours') or 0) > 0
                    or (rep.get('studies') or 0) > 0,
                'hours': {'field_service': hours,
                          'credit': {'value': 0,
                                     'approved': rep.get('credithours') or 0}},
                'bible_studies': rep.get('studies') or 0,
                'comments': rep.get('remarks') or '',
                'late': {'value': bool(submitted) and month != submitted,
                         'submitted': submitted},
                'status': 'confirmed',
            },
        })
    return out


# ── New World Scheduler CSV (exact fsr export format) ──────────────────────

def write_nws_csv(data, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    publishers = data.get('publishers') or []
    reports = data.get('reports') or []
    by_key = {((r.get('user') or {}).get('id'), r.get('year'), r.get('month')): r
              for r in reports}
    months = sorted({(r.get('year'), r.get('month')) for r in reports})

    path = out_dir / f"NWScheduler_field_service_{date.today():%Y%m%d}.csv"
    fieldnames = ['Date', 'FirstName', 'LastName', 'SharedInMinistry',
                  'BibleStudies', 'AP', 'Hours', 'Credit', 'Remarks']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for year, month in months:
            for pub in publishers:
                rep = by_key.get((pub.get('id'), year, month))
                row = {'Date': f"{year:04d}-{month:02d}",
                       'FirstName': pub.get('firstname', ''),
                       'LastName': pub.get('lastname', ''),
                       'SharedInMinistry': False, 'BibleStudies': '',
                       'AP': False, 'Hours': '', 'Credit': '', 'Remarks': ''}
                if rep:
                    hours = _report_hours(rep)
                    inferred = hours > 0 or (rep.get('studies') or 0) > 0
                    flag = rep.get('has_reported_field_service')
                    shared = flag if isinstance(flag, bool) else inferred
                    row['SharedInMinistry'] = shared
                    if isinstance(rep.get('remarks'), str) and rep['remarks'].strip():
                        row['Remarks'] = rep['remarks'].strip()
                    if shared:
                        if rep.get('pioneer') == 'Auxiliary':
                            row['AP'] = True
                        if hours > 0:
                            row['Hours'] = str(hours)
                        if (rep.get('studies') or 0) > 0:
                            row['BibleStudies'] = str(rep['studies'])
                        credit = rep.get('credithours')
                        if credit and float(credit) > 0:
                            credit = float(credit)
                            row['Credit'] = str(int(credit)) \
                                if credit.is_integer() else str(credit)
                writer.writerow(row)
    return path


class Command(BaseCommand):
    help = "One-shot Hourglass import: JSON (persons/reports/…) + program docx (+ NWS CSV)"

    def add_arguments(self, parser):
        parser.add_argument('paths', nargs='*',
                            help='Explicit .json and/or .docx paths (else auto-discover)')
        parser.add_argument('--dir', default=str(DEFAULT_DIR),
                            help=f'Drop directory to discover files in (default {DEFAULT_DIR})')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--nws-out', metavar='DIR',
                            help='Also write the New World Scheduler CSV to DIR')
        parser.add_argument('--language', default='51',
                            help='jwlinker MEPS language id for talk-title matching')

    def handle(self, *args, **opts):
        from django.core.management import call_command
        from django.utils import timezone

        from api.models import CongBackupTable, Congregation, Metadata

        json_path = docx_path = None
        for p in map(Path, opts['paths']):
            if not p.exists():
                raise CommandError(f"File not found: {p}")
            if p.suffix.lower() == '.json':
                json_path = p
            elif p.suffix.lower() == '.docx':
                docx_path = p
        if json_path is None and docx_path is None:
            json_path, docx_path = discover(opts['dir'])
        elif json_path is None or docx_path is None:
            found_json, found_docx = discover(opts['dir'])
            json_path = json_path or found_json
            docx_path = docx_path or found_docx

        if json_path is None and docx_path is None:
            raise CommandError(
                f"Nothing to import: no Hourglass JSON or program .docx found "
                f"in {opts['dir']} (drop the exports there, or pass paths)")

        cong = Congregation.objects.first()
        if cong is None:
            raise CommandError("No congregation exists yet")

        self.stdout.write(f"JSON:    {json_path or '— (skipped)'}")
        self.stdout.write(f"Program: {docx_path or '— (skipped)'}")

        data = None
        tables = {}
        embedded_program = None
        if json_path:
            data = json.loads(Path(json_path).read_text(encoding='utf-8'))
            tables = {
                'persons': migrate_persons(data),
                'meeting_attendance': migrate_attendance(data),
                'field_service_groups': migrate_groups(data),
                'branch_field_service_reports': migrate_branch_reports(data),
                'cong_field_service_reports': migrate_cong_reports(data),
            }
            for name, rows in tables.items():
                self.stdout.write(f"  {name}: {len(rows)} records")

            # A unified JSON (fsr `export organized`) embeds the program the
            # plain Hourglass export lacks — it supersedes the docx.
            program = data.get('program')
            if isinstance(program, dict) and (
                    program.get('weekend') or program.get('midweek')):
                embedded_program = program
                self.stdout.write(
                    f"  embedded program: "
                    f"{len(program.get('weekend') or [])} weekend talk(s), "
                    f"{len(program.get('midweek') or [])} midweek meeting(s) "
                    f"(from {program.get('source_docx', '?')}) — docx not needed")

        if opts['dry_run']:
            if embedded_program:
                for t in self._resolve_embedded(cong, embedded_program,
                                                opts['language']):
                    self.stdout.write(
                        f"{t['week_of']}  No {t['talk_number'] or '?':>3}  "
                        f"{t['title']}  — {t['speaker']}")
            elif docx_path:
                call_command('import_hourglass_program', str(docx_path),
                             '--dry-run', '--language', opts['language'])
            self.stdout.write(self.style.SUCCESS('Dry run: nothing written'))
            return

        for name, rows in tables.items():
            if not rows:
                continue
            CongBackupTable.objects.update_or_create(
                congregation=cong, table_name=name, defaults={'data': rows})
            Metadata.objects.update_or_create(
                congregation=cong, cong_user=None, key=name,
                defaults={'value': str(int(timezone.now().timestamp() * 1000))})

        if embedded_program:
            from api.management.commands.import_hourglass_program import (
                write_midweek_program, write_weekend_program,
            )
            resolved = self._resolve_embedded(cong, embedded_program,
                                              opts['language'])
            write_weekend_program(cong, resolved)
            matched = sum(1 for t in resolved if t['talk_number'])
            self.stdout.write(
                f"Imported {len(resolved)} weekend talks from the embedded "
                f"program ({matched} with S-34 numbers)")
            midweek = embedded_program.get('midweek') or []
            if midweek:
                slots = write_midweek_program(cong, midweek)
                self.stdout.write(
                    f"Imported {len(midweek)} midweek meeting(s) "
                    f"({slots} assignment slots)")
        elif docx_path:
            call_command('import_hourglass_program', str(docx_path),
                         '--language', opts['language'])

        if data:
            self._backfill_speaker_uids(cong, Metadata, CongBackupTable)

        if opts['nws_out']:
            if not data:
                self.stdout.write(self.style.WARNING(
                    'No JSON loaded — cannot write the NWS CSV'))
            else:
                path = write_nws_csv(data, opts['nws_out'])
                self.stdout.write(f"NWS CSV: {path}")

        self.stdout.write(self.style.SUCCESS('Import complete'))

    def _resolve_embedded(self, cong, program, language):
        """Unified-JSON program['weekend'] -> write_weekend_program entries.

        fsr already resolved the outline numbers and normalized names to
        'First Last'; re-resolve only what is missing (number 0, uids).
        """
        from api.management.commands.import_hourglass_program import (
            resolve_talk_number, resolve_speaker_uid,
        )

        own = _fold(cong.cong_name)
        resolved = []
        for t in (program.get('weekend') or []):
            number = t.get('outline_number') or \
                resolve_talk_number(t.get('title', ''), language)
            local = not t.get('speaker_cong') or \
                _fold(t.get('speaker_cong')) == own
            resolved.append({
                'week_of': t.get('week_of', ''),
                'title': t.get('title', ''),
                'talk_number': number,
                'talk_type': 'localSpeaker' if local else 'visitingSpeaker',
                'speaker': t.get('speaker', ''),
                'speaker_uid': resolve_speaker_uid(cong, t.get('speaker', '')),
            })
        return [t for t in resolved if t['week_of']]

    def _backfill_speaker_uids(self, cong, Metadata, CongBackupTable):
        """Fill schedule speaker uids the program import stored as name-only."""
        from django.utils import timezone
        try:
            persons_bt = CongBackupTable.objects.get(
                congregation=cong, table_name='persons')
            sched_bt = CongBackupTable.objects.get(
                congregation=cong, table_name='schedules')
        except CongBackupTable.DoesNotExist:
            return

        index = {}
        for p in (persons_bt.data or []):
            pd = p.get('person_data', {})
            first = pd.get('person_firstname', {}).get('value', '')
            last = pd.get('person_lastname', {}).get('value', '')
            uid = p.get('person_uid')
            if uid and (first or last):
                index[_fold(f"{last}, {first}")] = uid
                index[_fold(f"{first} {last}")] = uid
                index[_fold(pd.get('person_display_name', {}).get('value', ''))] = uid

        changed = 0
        data = sched_bt.data or []
        for entry in data:
            part_1 = (entry.get('weekend_meeting', {})
                      .get('speaker', {}).get('part_1') or [])
            for slot in part_1:
                if isinstance(slot, dict) and slot.get('name') and not slot.get('value'):
                    uid = index.get(_fold(slot['name']))
                    if uid:
                        slot['value'] = uid
                        changed += 1
        if changed:
            sched_bt.data = data
            sched_bt.save()
            Metadata.objects.update_or_create(
                congregation=cong, cong_user=None, key='schedules',
                defaults={'value': str(int(timezone.now().timestamp() * 1000))})
            self.stdout.write(f"  backfilled {changed} speaker uid(s) in schedules")
