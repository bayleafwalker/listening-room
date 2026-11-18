"""Local recording observations and explicit, hash-bound human reviews.

Origin is an opaque library identifier, never a URL or credential. Provider
observations are data, not reviews; only review() can create a review.
"""
import contextlib
import datetime
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path

_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,160}$')
_HASH = re.compile(r'^[0-9a-f]{64}$')
_CLASSES = {'instrumental', 'vocal', 'spoken', 'unknown'}
MAX_OBSERVATION_BYTES = 96 * 1024
MAX_PAGE_BYTES = 512 * 1024


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _key(origin, item_id, audio_sha256, allow_unknown=False):
    if not isinstance(origin, str) or not _ID.fullmatch(origin):
        raise ValueError('Invalid library origin')
    if not isinstance(item_id, str) or not _ID.fullmatch(item_id):
        raise ValueError('Invalid library item')
    if allow_unknown and audio_sha256 is None:
        audio_sha256 = ''
    if not isinstance(audio_sha256, str) or (not _HASH.fullmatch(audio_sha256) and not (allow_unknown and audio_sha256 == '')):
        raise ValueError('A current audio SHA256 is required')
    return origin, item_id, audio_sha256


def _observations(values, kind):
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError('Invalid metadata observations')
    clean = []
    allowed = ('provider', 'provider_id', 'method', 'model', 'version', 'date', 'confidence', 'name', 'value', 'status')
    for item in values:
        if not isinstance(item, dict):
            raise ValueError('Invalid metadata observation')
        result = {}
        for name in allowed:
            if name not in item:
                continue
            value = item[name]
            if name in ('confidence', 'value') and isinstance(value, (float, int)) and not isinstance(value, bool):
                if not math.isfinite(value) or (name == 'confidence' and not 0 <= value <= 1):
                    raise ValueError('Invalid metadata value')
            elif not isinstance(value, str) or len(value) > 200 or not re.fullmatch(r'[\w .:+-]+', value):
                raise ValueError('Invalid metadata value')
            result[name] = value
        if not result.get('provider') or (kind == 'feature' and not result.get('name')):
            raise ValueError('Metadata must identify its provider and feature')
        clean.append(result)
    if len(json.dumps(clean, ensure_ascii=True).encode('ascii')) > MAX_OBSERVATION_BYTES:
        raise ValueError('Metadata observations exceed the size limit')
    return clean


class MetadataStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS unbound_features (origin TEXT NOT NULL, item_id TEXT NOT NULL, features TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(origin,item_id))')
            db.execute('CREATE TABLE IF NOT EXISTS recordings (origin TEXT NOT NULL, item_id TEXT NOT NULL, audio_sha256 TEXT NOT NULL, identities TEXT NOT NULL, features TEXT NOT NULL, reviewed_class TEXT, reviewer TEXT, reviewed_at TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(origin,item_id))')

    @contextlib.contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self, origin, item_id, audio_sha256, identities=None, features=None, revision=None):
        key = _key(origin, item_id, audio_sha256, allow_unknown=True)
        if revision is not None:
            encoded = json.dumps(revision, sort_keys=True, separators=(',', ':'))
            if len(encoded) > 4096:
                raise ValueError('Invalid provider revision')
            identities = [x for x in (identities or []) if not (isinstance(x, dict) and x.get('provider') == 'jellyfin' and x.get('name') == 'revision')] + [{'provider': 'jellyfin', 'name': 'revision', 'method': 'sha256 of provider revision', 'value': hashlib.sha256(encoded.encode()).hexdigest()}]
        ids = _observations(identities or [], 'identity')
        feats = _observations(features or [], 'feature')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT audio_sha256,identities,features FROM recordings WHERE origin=? AND item_id=?', key[:2]).fetchone()
            same_revision = False
            if audio_sha256 is None and revision is not None:
                stamp = next((x for x in ids if x.get('name') == 'revision' and x.get('provider') == 'jellyfin'), None)
                if previous and stamp and stamp in json.loads(previous['identities']):
                    key = (origin, item_id, previous['audio_sha256'])
                    same_revision = True
            if previous and (same_revision or (key[2] and previous['audio_sha256'] == key[2])):
                confirmed = [x for x in json.loads(previous['identities']) if x.get('status') == 'confirmed']
                for entry in confirmed:
                    if not any(x.get('provider') == entry.get('provider') and x.get('name') == entry.get('name') and x.get('status') == 'confirmed' for x in ids):
                        ids.append(entry)
                if key[2]:
                    for entry in json.loads(previous['features']):
                        if (entry.get('provider') == 'Essentia MTG' and entry.get('name') in ('instrumental score', 'voice score')
                                and not any(x.get('provider') == entry.get('provider') and x.get('name') == entry.get('name') for x in feats)):
                            feats.append(entry)
            db.execute('''INSERT INTO recordings VALUES (?,?,?,?,?,NULL,NULL,NULL,?)
                ON CONFLICT(origin,item_id) DO UPDATE SET
                  reviewed_class=CASE WHEN recordings.audio_sha256=excluded.audio_sha256 THEN recordings.reviewed_class ELSE NULL END,
                  reviewer=CASE WHEN recordings.audio_sha256=excluded.audio_sha256 THEN recordings.reviewer ELSE NULL END,
                  reviewed_at=CASE WHEN recordings.audio_sha256=excluded.audio_sha256 THEN recordings.reviewed_at ELSE NULL END,
                  audio_sha256=excluded.audio_sha256, identities=excluded.identities,
                  features=excluded.features, updated_at=excluded.updated_at''',
                       (*key, json.dumps(ids), json.dumps(feats), _now()))
        return self.inspect(origin, item_id, key[2] or None)

    def inspect(self, origin, item_id, audio_sha256):
        key = _key(origin, item_id, audio_sha256, allow_unknown=True)
        with self._connect() as db:
            row = db.execute('SELECT * FROM recordings WHERE origin=? AND item_id=?', key[:2]).fetchone()
            unbound = db.execute('SELECT features FROM unbound_features WHERE origin=? AND item_id=?', key[:2]).fetchone()
        if not row or row['audio_sha256'] != key[2]:
            return None
        record = dict(origin=row['origin'], item_id=row['item_id'], audio_sha256=row['audio_sha256'] or None,
                    identities=json.loads(row['identities']), features=json.loads(row['features']),
                    unbound_features=json.loads(unbound['features']) if unbound else [],
                    review=({'classification': row['reviewed_class'], 'reviewer': row['reviewer'], 'date': row['reviewed_at']} if row['reviewed_class'] else None),
                    updated_at=row['updated_at'])
        if len(json.dumps(record, ensure_ascii=True).encode('ascii')) > MAX_PAGE_BYTES - 1024:
            raise ValueError('Metadata record exceeds the export size limit')
        return record

    def review(self, origin, item_id, audio_sha256, classification, reviewer):
        key = _key(origin, item_id, audio_sha256)
        if classification not in _CLASSES or not isinstance(reviewer, str) or not re.fullmatch(r'[\w .-]{1,100}', reviewer):
            raise ValueError('Invalid human review')
        with self._connect() as db:
            result = db.execute('UPDATE recordings SET reviewed_class=?, reviewer=?, reviewed_at=? WHERE origin=? AND item_id=? AND audio_sha256=?', (classification, reviewer, _now(), *key))
            if result.rowcount != 1:
                raise ValueError('Recording changed or has not been observed')
        return self.inspect(*key)

    def revoke(self, origin, item_id, audio_sha256):
        key = _key(origin, item_id, audio_sha256)
        with self._connect() as db:
            result = db.execute('UPDATE recordings SET reviewed_class=NULL, reviewer=NULL, reviewed_at=NULL WHERE origin=? AND item_id=? AND audio_sha256=?', key)
            if result.rowcount != 1:
                raise ValueError('Recording changed or has not been observed')

    def current(self, origin, item_id):
        _key(origin, item_id, None, allow_unknown=True)
        with self._connect() as db:
            row = db.execute('SELECT audio_sha256 FROM recordings WHERE origin=? AND item_id=?', (origin, item_id)).fetchone()
        return self.inspect(origin, item_id, row['audio_sha256'] or None) if row else None

    def observe_unbound(self, origin, item_id, features):
        _key(origin, item_id, None, allow_unknown=True)
        clean = _observations(features, 'feature')
        with self._connect() as db:
            db.execute('INSERT INTO unbound_features VALUES (?,?,?,?) ON CONFLICT(origin,item_id) DO UPDATE SET features=excluded.features,updated_at=excluded.updated_at',
                       (origin, item_id, json.dumps(clean), _now()))

    def import_records(self, records):
        if not isinstance(records, list) or len(records) > 200 or len(json.dumps(records)) > 2000000:
            raise ValueError('Import exceeds the metadata limit')
        imported = 0
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('Invalid imported metadata')
            origin, item_id = record.get('origin'), record.get('item_id')
            _key(origin, item_id, None, allow_unknown=True)
            ids = _observations(record.get('identities') or [], 'identity')
            ids = [dict(x, status='candidate') if x.get('status') == 'confirmed' else x for x in ids if x.get('name') != 'revision']
            features = _observations(record.get('features') or [], 'feature')
            unbound = _observations(record.get('unbound_features') or [], 'feature')
            # Imported review and hash are never authoritative. Do not overwrite
            # observations or reviews already held by this installation.
            with self._connect() as db:
                result = db.execute('INSERT OR IGNORE INTO recordings VALUES (?,?,?,?,?,NULL,NULL,NULL,?)',
                                    (origin, item_id, '', json.dumps(ids), json.dumps(features), _now()))
                if result.rowcount and unbound:
                    db.execute('INSERT OR IGNORE INTO unbound_features VALUES (?,?,?,?)', (origin, item_id, json.dumps(unbound), _now()))
                imported += result.rowcount
        return imported

    def export_page(self, cursor=None, limit=50):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError('Invalid metadata page limit')
        if cursor is None:
            origin, item_id = '', ''
        elif isinstance(cursor, dict) and set(cursor) == {'origin', 'item_id'}:
            origin, item_id, _ = _key(cursor['origin'], cursor['item_id'], None, allow_unknown=True)
        else:
            raise ValueError('Invalid metadata page cursor')
        with self._connect() as db:
            rows = db.execute('SELECT origin,item_id,audio_sha256 FROM recordings WHERE origin > ? OR (origin = ? AND item_id > ?) ORDER BY origin,item_id LIMIT ?',
                              (origin, origin, item_id, limit + 1)).fetchall()
        records = []
        budget = 1024
        for row in rows[:limit]:
            record = self.inspect(row['origin'], row['item_id'], row['audio_sha256'] or None)
            if record is None:
                raise ValueError('Metadata changed during export; retry the page')
            size = len(json.dumps(record, ensure_ascii=True).encode('ascii')) + 2
            if budget + size > MAX_PAGE_BYTES:
                if not records:
                    raise ValueError('Metadata record exceeds the export size limit')
                break
            records.append(record)
            budget += size
        has_more = len(rows) > len(records)
        next_cursor = ({'origin': records[-1]['origin'], 'item_id': records[-1]['item_id']} if has_more and records else None)
        return {'version': 1, 'records': records, 'next_cursor': next_cursor}

    def list_records(self, limit=100):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError('Invalid metadata limit')
        with self._connect() as db:
            rows = db.execute('SELECT origin,item_id,audio_sha256 FROM recordings ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()
        return [self.inspect(row['origin'], row['item_id'], row['audio_sha256'] or None) for row in rows]
