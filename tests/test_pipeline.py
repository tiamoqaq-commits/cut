import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

class Pipeline(unittest.TestCase):
    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with socket.socket() as s:
                s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            env=os.environ|{'CUT_DATA_DIR':tmp+'/data','CUT_PORT':str(port),'CUT_STORAGE':'local','CUT_PLANNER':'rules'}
            fixture=Path(tmp)/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x120:rate=30','-t','2','-c:v','libx264','-threads','1',str(fixture)],check=True)
            def launch(module):return subprocess.Popen([sys.executable,'-m',module],env=env,stdout=subprocess.DEVNULL)
            server=launch('backend.server');worker=None
            def req(path,data=None):
                with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=data),timeout=10) as r:return json.load(r)
            def ready():
                for _ in range(100):
                    try:req('/api/health');return
                    except OSError:time.sleep(.05)
                self.fail('API failed to start')
            try:
                ready()
                a=req('/api/assets?name=fixture.mp4&category=food',fixture.read_bytes())
                with self.assertRaises(urllib.error.HTTPError):req('/api/assets',b'not video')
                with self.assertRaises(urllib.error.HTTPError):req('/api/batches',json.dumps({'asset_ids':[a['id']],'category':'wrong'}).encode())
                batch=req('/api/batches',json.dumps({'asset_ids':[a['id']],'category':'food','count':2,'duration':2,'clip_seconds':1}).encode())
                server.terminate();server.wait();server=launch('backend.server');ready()
                worker=launch('backend.worker')
                for _ in range(120):
                    jobs=req('/api/batches/'+batch['batch_id'])['jobs']
                    if all(j['status'] in ('succeeded','failed') for j in jobs):break
                    time.sleep(.25)
                self.assertEqual([j['status'] for j in jobs],['succeeded','succeeded'],jobs)
                for j in jobs:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}'+j['download_url']) as r:content=r.read()
                    out=Path(tmp)/(j['id']+'.mp4');out.write_bytes(content)
                    info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(out)]))
                    stream=info['streams'][0]
                    self.assertEqual((stream['codec_name'],stream['width'],stream['height']),('h264',720,1280))
                    self.assertAlmostEqual(float(info['format']['duration']),2,delta=.15)
                    self.assertEqual(j['progress'],100)
            finally:
                server.terminate();server.wait()
                if worker:worker.terminate();worker.wait()

if __name__=='__main__':unittest.main()
