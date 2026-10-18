"""The single crawl path for platform facts.

Both the CP coordinator and the dashboard need the same Gitea snapshot; two
independent crawls paid the same O(objects) cost twice, on two schedules.
This module owns the only crawl: one global store keeps the last full
snapshot per project (raw JSON per item, order preserved) and serves it to
every consumer inside its TTL window, refreshing on demand otherwise.

The crawl itself is injected — callers pass ``Gitea.snapshot`` — so this
module never builds credentials and never imports the adapter or the
dashboard. Stored items round-trip through JSON unchanged, so a snapshot
served from here is equal to the one the crawl would return: derived work
item versions cannot drift.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .runtime import data_home

SCHEMA = '''
CREATE TABLE IF NOT EXISTS items (
    project_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    number INTEGER,
    state TEXT,
    updated_at TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (project_key, kind, number)
);
CREATE TABLE IF NOT EXISTS meta (
    project_key TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (project_key, key)
);
'''


def default_db_path():
    """One store for every project on this machine (~/.code-loop)."""
    return data_home() / 'observer.sqlite'


def connect(db_path):
    # Same contention disciplines as the dashboard store: a 15s busy window
    # (a locked store must slow a crawl down, never crash a loop) and WAL so
    # the daemon driver and a collector process never block each other.
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL')
    except sqlite3.OperationalError:
        pass  # a concurrent writer holds the switch; the next connect retries
    db.executescript(SCHEMA)
    return db


def _meta(db, project_key, key):
    row = db.execute('SELECT value FROM meta WHERE project_key=? AND key=?',
                     (project_key, key)).fetchone()
    try:
        return json.loads(row['value']) if row is not None else None
    except ValueError:
        return None  # corrupt row: callers treat it as absent and re-crawl


def _known(db, project_key):
    """{(kind, number): (state, updated_at)} — the adapter's incremental basis."""
    return {(row['kind'], row['number']): (row['state'], row['updated_at'])
            for row in db.execute('SELECT kind, number, state, updated_at FROM items '
                                  'WHERE project_key=?', (project_key,))}


def _read(db, project_key):
    """One consistent read (a WAL reader snapshot): the meta rows and the item
    rows can never come from two different crawls."""
    db.execute('BEGIN')
    try:
        stored = {'synced_at': _meta(db, project_key, 'synced_at'),
                  'base_sha': _meta(db, project_key, 'base_sha'),
                  'items': db.execute('SELECT kind, raw_json FROM items WHERE project_key=? '
                                      'ORDER BY kind, ordinal', (project_key,)).fetchall()}
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return stored


def _assemble(stored):
    if not isinstance(stored['base_sha'], str) or not stored['base_sha']:
        raise ValueError('INVALID_SNAPSHOT')
    snapshot = {'issues': [], 'prs': [], 'base_sha': stored['base_sha']}
    for row in stored['items']:
        snapshot['issues' if row['kind'] == 'issue' else 'prs'].append(json.loads(row['raw_json']))
    return snapshot


def _refresh(db, project_key, crawl, now, known=None):
    found = crawl(known if known is not None else _known(db, project_key))
    if not isinstance(found, dict) or any(k not in found for k in ('issues', 'prs', 'base_sha')):
        raise ValueError('INVALID_SNAPSHOT')
    # Serialize everything before opening the write transaction: a malformed
    # crawl fails without touching the stored facts. Items without a positive
    # integer identity are rejected too — the store keys rows by number and
    # SQLite would not dedupe NULLs.
    rows = []
    for kind, key in (('issue', 'issues'), ('pr', 'prs')):
        for ordinal, item in enumerate(found[key]):
            if not isinstance(item, dict) or type(item.get('number')) is not int or item['number'] <= 0:
                raise ValueError('INVALID_SNAPSHOT')
            rows.append((project_key, kind, ordinal, item['number'], item.get('state'),
                         item.get('updated_at'), json.dumps(item, ensure_ascii=False)))
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('DELETE FROM items WHERE project_key=?', (project_key,))
        db.executemany('INSERT INTO items VALUES (?,?,?,?,?,?,?)', rows)
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?,?)',
                   (project_key, 'base_sha', json.dumps(found['base_sha'])))
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?,?)',
                   (project_key, 'synced_at', json.dumps(now)))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return found


def refresh(db_path, project_key, crawl, now=None):
    """Crawl once and atomically replace this project's stored snapshot."""
    db = connect(db_path)
    try:
        return _refresh(db, project_key, crawl, time.time() if now is None else now)
    finally:
        db.close()


def snapshot(db_path, project_key, ttl, crawl, now=None, known=None):
    """Serve the stored snapshot while it is within ``ttl``; crawl otherwise.

    A missing, expired, or unreadable store falls through to a refresh, so a
    corrupt row self-heals on the next read instead of wedging the caller.

    ``known`` overrides the incremental basis for this crawl (issue #165):
    a consumer with its own store (e.g. the dashboard) passes its own known
    items so closed+unchanged PRs it has never seen are fully crawled
    (comments/reviews fetched), instead of being skipped by the observer's
    own basis and landing as empty threads. When ``known`` is provided and
    does not cover every stored item (the consumer has never seen some
    entity), the stored snapshot is treated as incomplete for this caller
    and a fresh crawl runs even inside the TTL — otherwise the consumer
    would store empty threads that never self-heal.
    """
    now = time.time() if now is None else now
    db = connect(db_path)
    try:
        stored = _read(db, project_key)
        synced_at = stored['synced_at']
        if isinstance(synced_at, (int, float)) and not isinstance(synced_at, bool) \
                and now - synced_at <= ttl:
            if known is None or _covers(stored, known):
                try:
                    return _assemble(stored)
                except (ValueError, TypeError):
                    pass  # corrupt store: fall through to a fresh crawl
        return _refresh(db, project_key, crawl, now, known=known)
    finally:
        db.close()


def _covers(stored, known):
    """True when ``known`` mentions every item the store holds — i.e. the
    caller has already seen everything, so the stored snapshot is complete
    for it. A consumer whose store was reset (empty known) fails this and
    forces a fresh crawl (issue #165)."""
    for row in stored['items']:
        try:
            number = json.loads(row['raw_json']).get('number')
        except ValueError:
            return False
        key = (row['kind'], number)
        if key not in known:
            return False
    return True


def iter_projects(projects_root):
    """Project config paths under the CP projects root, in key order."""
    root = Path(projects_root)
    return sorted(root.glob('*/control-plane.json')) if root.is_dir() else []


def refresh_all(db_path, projects_root, crawl_factory, now=None):
    """One driver tick: refresh every project; failures stay contained.

    A project that raises never stops the projects after it (an earlier
    failure silently starving later projects is a repeat offence), and the
    caller gets every outcome to log. Returns ``[(project_key, None|exc)]``.
    """
    results = []
    for config_path in iter_projects(projects_root):
        key = config_path.parent.name
        try:
            refresh(db_path, key, crawl_factory(config_path), now)
            results.append((key, None))
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            results.append((key, exc))
    return results


def run_driver(db_path, projects_root, crawl_factory, period, stop=None, report=None, now=None):
    """Keep every project's facts within one ``period`` of freshness.

    Runs until ``stop`` is set; each tick reports ``(project_key, error)``
    through ``report`` so the caller owns the logging policy.
    """
    stop = threading.Event() if stop is None else stop
    while not stop.is_set():
        started = time.monotonic()
        for key, exc in refresh_all(db_path, projects_root, crawl_factory, now=now):
            if report is not None:
                report(key, exc)
        stop.wait(max(0.0, period - (time.monotonic() - started)))
