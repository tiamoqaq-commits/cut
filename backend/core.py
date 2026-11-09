"""Persistent local job queue and replaceable planning/storage contracts."""
import json
import os
import random
import sqlite3
import uuid
from contextlib import contextmanager
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
# Minimal .env loader: real environment always wins; no external dependency.
if (ROOT / '.env').exists():
    for line in (ROOT / '.env').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))
DATA = Path(os.getenv('CUT_DATA_DIR', str(ROOT / 'data'))).resolve()
DB = DATA / 'cut.sqlite3'

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    try:
        yield from _initialize(c)
    finally:
        c.close()

def _initialize(c):
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS assets (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
      duration REAL NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS music (id TEXT PRIMARY KEY, name TEXT NOT NULL, duration REAL NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, status TEXT NOT NULL,
      progress INTEGER NOT NULL DEFAULT 0, request TEXT NOT NULL,
      plan TEXT, output_key TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id);
    ''')
    with c:
        yield c

class Storage(Protocol):
    """Cloud adapter must download to local_path and publish a local render atomically."""
    def local_path(self, key: str) -> Path: ...
    def publish(self, local_file: Path, key: str) -> None: ...

class LocalStorage:
    def local_path(self, key):
        path = (DATA / key).resolve()
        if DATA not in path.parents:
            raise ValueError('Invalid storage key')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def publish(self, local_file, key):
        os.replace(local_file, self.local_path(key))

@dataclass
class Clip:
    asset_id: str
    start: float
    duration: float
    text: str = ""

class Planner(Protocol):
    def plan(self, assets: list[dict], request: dict, seed: str) -> list[Clip]: ...

class RulePlanner:
    """No AI claim: deterministic shuffled order and random valid cut offsets."""
    def plan(self, assets, request, seed):
        rng = random.Random(seed)
        candidates = list(assets)
        rng.shuffle(candidates)
        remaining = request['duration']
        clips = []
        i = 0
        while remaining > 0.01:
            asset = candidates[i % len(candidates)]
            duration = min(remaining, request['clip_seconds'], asset['duration'])
            start = rng.uniform(0, max(0, asset['duration'] - duration))
            clips.append(Clip(asset['id'], start, duration))
            remaining -= duration
            i += 1
        return clips

def providers(planner_name=None):
    if os.getenv('CUT_STORAGE', 'local') != 'local':
        raise ValueError('Storage adapter not implemented; use CUT_STORAGE=local')
    name = planner_name or os.getenv('CUT_PLANNER', 'rules')
    if name == 'rules':
        planner = RulePlanner()
    elif name == 'chat':
        from .ai import ChatPlanner
        planner = ChatPlanner()
    else:
        raise ValueError('Unknown planner; choose rules or chat')
    return LocalStorage(), planner

def job_json(row):
    d = dict(row)
    d['request'] = json.loads(d['request'])
    d['plan'] = json.loads(d['plan']) if d['plan'] else None
    d['download_url'] = '/api/jobs/' + d['id'] + '/output' if d['status'] == 'succeeded' else None
    d['subtitles_url'] = '/api/jobs/' + d['id'] + '/subtitles' if d['status'] == 'succeeded' and d['request'].get('subtitle_mode', 'none') != 'none' else None
    return d

def validate_batch(body, c):
    ids = body.get('asset_ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 30 or any(not isinstance(x, str) for x in ids):
        raise ValueError('asset_ids must contain 1–30 uploaded asset IDs')
    if len(set(ids)) != len(ids):
        raise ValueError('asset_ids must be unique')
    for key, low, high, default in [('count', 1, 20, 10), ('duration', 1, 60, 15), ('clip_seconds', 1, 10, 3)]:
        value = body.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{key} must be an integer from {low} to {high}')
        body[key] = value
    category = body.get('category', 'general')
    if not isinstance(category, str) or not 1 <= len(category) <= 64:
        raise ValueError('category must be 1–64 characters')
    rows = c.execute('SELECT * FROM assets WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
    if len(rows) != len(ids):
        raise ValueError('Unknown asset ID')
    if any(r['category'] != category for r in rows):
        raise ValueError('All assets must match the batch category')
    planner = body.get('planner', os.getenv('CUT_PLANNER', 'rules'))
    mode = body.get('subtitle_mode', 'none')
    brief = body.get('product_brief', '')
    lines = body.get('subtitle_lines', [])
    if planner not in ('rules', 'chat') or mode not in ('none', 'manual', 'ai'):
        raise ValueError('Invalid planner or subtitle_mode')
    if not isinstance(brief, str) or len(brief) > 2000:
        raise ValueError('product_brief must be a string of up to 2000 characters')
    if not isinstance(lines, list) or len(lines) > 60 or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 48 or any(ord(c) < 32 for c in x) for x in lines):
        raise ValueError('subtitle_lines: up to 60 single lines, each 1–48 characters')
    if mode == 'manual' and (not lines or len(lines) > body['duration'] * 2):
        raise ValueError('Manual subtitles need at least one line and at least 0.5 seconds per line')
    if mode == 'ai' and (planner != 'chat' or not brief.strip()):
        raise ValueError('AI subtitles require chat planner and a product brief')
    if planner == 'chat':
        from .ai import configuration
        configuration()
    audio_mode = body.get('audio_mode', 'none')
    if audio_mode not in ('none','tts_bgm'):
        raise ValueError('audio_mode must be none or tts_bgm')
    music_id = body.get('music_id')
    volume = body.get('music_volume', .12)
    if isinstance(volume, bool) or not isinstance(volume,(int,float)) or not math.isfinite(volume) or not 0 <= volume <= .5:
        raise ValueError('music_volume must be from 0 to 0.5')
    if audio_mode == 'tts_bgm':
        if mode == 'none':
            raise ValueError('AI 配音需要选择手写字幕或 AI 字幕作为朗读文案')
        if not isinstance(music_id,str) or not c.execute('SELECT id FROM music WHERE id=?',(music_id,)).fetchone():
            raise ValueError('请先上传并选择背景音乐')
        from .audio import tts_config
        tts_config()
    return {k: body[k] for k in ('asset_ids', 'count', 'duration', 'clip_seconds')} | {
        'category': category, 'planner': planner, 'subtitle_mode': mode,
        'product_brief': brief.strip(), 'subtitle_lines': [x.strip() for x in lines],
        'audio_mode': audio_mode, 'music_id': music_id if audio_mode == 'tts_bgm' else None, 'music_volume': volume}


def create_batch(body):
    with connect() as c:
        req = validate_batch(body, c)
        batch = uuid.uuid4().hex
        for _ in range(req['count']):
            c.execute('INSERT INTO jobs(id,batch_id,status,request,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                      (uuid.uuid4().hex, batch, 'queued', json.dumps(req), now(), now()))
    return batch
