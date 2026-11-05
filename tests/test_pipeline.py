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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

class ModelHandler(BaseHTTPRequestHandler):
    calls = 0
    def log_message(self,*args):pass
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        info=json.loads(payload['messages'][1]['content'])
        type(self).calls += 1
        assert self.path == '/v1/chat/completions'
        assert self.headers['Authorization'] == 'Bearer test-key'
        plan={'clips':[{'asset_id':info['assets'][0]['id'],'start':0,'duration':1,'text':'Simple style'},
                       {'asset_id':info['assets'][0]['id'],'start':1,'duration':1,'text':'Everyday detail'}]}
        raw=json.dumps({'choices':[{'message':{'content':json.dumps(plan)}}]}).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

class Pipeline(unittest.TestCase):
    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with socket.socket() as s:
                s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            env=os.environ|{'CUT_DATA_DIR':tmp+'/data','CUT_PORT':str(port),'CUT_STORAGE':'local','CUT_PLANNER':'rules'}
            fixture=Path(tmp)/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x120:rate=30','-t','2','-c:v','libx264','-threads','1',str(fixture)],check=True)
            def launch(module):return subprocess.Popen([sys.executable,'-m',module],env=env,stdout=subprocess.DEVNULL)
            model=ThreadingHTTPServer(('127.0.0.1',0),ModelHandler)
            Thread(target=model.serve_forever,daemon=True).start()
            env.update({'CUT_LLM_BASE_URL':f'http://127.0.0.1:{model.server_port}/v1','CUT_LLM_MODEL':'test-model','CUT_LLM_API_KEY':'test-key'})
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
                batch=req('/api/batches',json.dumps({'asset_ids':[a['id']],'category':'food','count':2,'duration':2,'clip_seconds':1,'subtitle_mode':'manual','subtitle_lines':['Everyday style']}).encode())
                server.terminate();server.wait();server=launch('backend.server');ready()
                ai_batch=req('/api/batches',json.dumps({'asset_ids':[a['id']],'category':'food','count':1,'duration':2,'clip_seconds':1,'planner':'chat','subtitle_mode':'ai','product_brief':'Simple everyday product'}).encode())
                env['CUT_SUBTITLE_FONT']='invalid,font'
                worker=launch('backend.worker')
                for _ in range(120):
                    jobs=req('/api/batches/'+batch['batch_id'])['jobs']+req('/api/batches/'+ai_batch['batch_id'])['jobs']
                    if all(j['status'] in ('succeeded','failed') for j in jobs):break
                    time.sleep(.25)
                self.assertTrue(all(j['status']=='failed' for j in jobs),jobs)
                self.assertEqual(ModelHandler.calls,1)
                worker.terminate();worker.wait()
                env['CUT_SUBTITLE_FONT']='DejaVu Sans'
                for j in jobs:req('/api/jobs/'+j['id']+'/retry',b'{}')
                worker=launch('backend.worker')
                for _ in range(120):
                    jobs=req('/api/batches/'+batch['batch_id'])['jobs']+req('/api/batches/'+ai_batch['batch_id'])['jobs']
                    if all(j['status'] in ('succeeded','failed') for j in jobs):break
                    time.sleep(.25)
                self.assertEqual([j['status'] for j in jobs],['succeeded','succeeded','succeeded'],jobs)
                self.assertTrue(all(j['attempts']==2 for j in jobs))
                self.assertEqual(ModelHandler.calls,1)
                for j in jobs:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}'+j['download_url']) as r:content=r.read()
                    out=Path(tmp)/(j['id']+'.mp4');out.write_bytes(content)
                    info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(out)]))
                    stream=info['streams'][0]
                    self.assertEqual((stream['codec_name'],stream['width'],stream['height']),('h264',720,1280))
                    self.assertAlmostEqual(float(info['format']['duration']),2,delta=.15)
                    self.assertEqual(j['progress'],100)
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}'+j['subtitles_url']) as r:
                        self.assertIn(b'00:00:00,000 -->',r.read())
                    # Compare first-frame subtitle area to a baseline render, proving pixels are burned.
                    baseline=Path(tmp)/'baseline.mp4'
                    if not baseline.exists():
                        subprocess.run(['ffmpeg','-v','error','-i',str(fixture),'-vf','scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30','-an','-threads','1',str(baseline)],check=True)
                    def pixels(path):
                        return subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-vf','crop=600:180:60:1020','-frames:v','1','-f','rawvideo','-pix_fmt','gray','-threads','1','-'])
                    self.assertNotEqual(pixels(out),pixels(baseline))
            finally:
                server.terminate();server.wait()
                model.shutdown();model.server_close()
                if worker:worker.terminate();worker.wait()

if __name__=='__main__':unittest.main()
