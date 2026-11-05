"""Run exactly one worker per local data directory; advisory lock prevents duplicates."""
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict
from .core import DATA, connect, now, providers
from .media import render

logging.basicConfig(level=logging.INFO)

def claim():
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if row:
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1, progress=1, updated_at=? WHERE id=?", (now(), row['id']))
        return dict(row) if row else None

def process(job):
    def update(progress):
        with connect() as c:
            c.execute('UPDATE jobs SET progress=?,updated_at=? WHERE id=?', (progress, now(), job['id']))
    try:
        storage, planner = providers()
        req = json.loads(job['request'])
        with connect() as c:
            assets = [dict(c.execute('SELECT * FROM assets WHERE id=?', (x,)).fetchone()) for x in req['asset_ids']]
        clips = planner.plan(assets, req, job['id'])
        with connect() as c:
            c.execute('UPDATE jobs SET plan=? WHERE id=?', (json.dumps([asdict(x) for x in clips]), job['id']))
        with tempfile.TemporaryDirectory(prefix='cut-') as tmp:
            from pathlib import Path
            file = render(clips, storage, Path(tmp), update)
            key = f"outputs/{job['id']}.mp4"
            # Copy into destination filesystem first so replace is atomic across /tmp mounts.
            pending = storage.local_path(key + '.pending')
            import shutil
            shutil.copyfile(file, pending)
            storage.publish(pending, key)
        with connect() as c:
            c.execute("UPDATE jobs SET status='succeeded',progress=100,output_key=?,error=NULL,updated_at=? WHERE id=?", (key, now(), job['id']))
    except Exception as e:
        logging.exception('Job %s failed', job['id'])
        with connect() as c:
            c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?", (str(e)[-2000:], now(), job['id']))

def main():
    import fcntl
    DATA.mkdir(parents=True, exist_ok=True)
    lock = open(DATA / 'worker.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Worker already running for this data directory')
    providers()
    with connect() as c:
        c.execute("UPDATE jobs SET status='queued',progress=0,updated_at=? WHERE status='running'", (now(),))
    logging.info('Worker ready')
    while True:
        job = claim()
        if job:
            process(job)
        else:
            time.sleep(1)

if __name__ == '__main__':
    main()
