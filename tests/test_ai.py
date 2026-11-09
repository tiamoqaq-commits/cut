import json
import math
import os
import tempfile
import io
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.ai import ChatPlanner, validate_plan
from backend.core import Clip
from backend.subtitles import cues_for, safe_ass, write_subtitles

class AIValidation(unittest.TestCase):
    def setUp(self):
        self.assets=[{'id':'a','duration':4}]
        self.req={'duration':2,'clip_seconds':2,'subtitle_mode':'ai'}
        self.valid={'clips':[{'asset_id':'a','start':0,'duration':2,'text':'真实卖点'}]}

    def test_bounds_and_untrusted_outputs(self):
        self.assertEqual(validate_plan(self.valid,self.assets,self.req,Clip)[0].text,'真实卖点')
        for changes in ({'asset_id':'other'},{'start':-1},{'start':3},{'duration':float('nan')},
                        {'duration':True},{'duration':0},{'text':''},{'text':'x'*49}):
            data={'clips':[self.valid['clips'][0]|changes]}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_plan(data,self.assets,self.req,Clip)

    def test_fit_short_long_and_fractional_plans(self):
        for durations,target in (([1],15),([2,2,2],2),([.1],60),([.31,.87],10),([2,.1],1)):
            req=self.req | {'duration':target}
            data={'clips':[{'asset_id':'a','start':1,'duration':d,'text':'文案'} for d in durations]}
            with self.subTest(durations=durations,target=target):
                result=validate_plan(data,self.assets,req,Clip)
                self.assertEqual(sum(round(c.duration*30) for c in result),target*30)
                self.assertAlmostEqual(sum(c.duration for c in result),target)
                for clip in result:
                    self.assertGreaterEqual(clip.duration,.1)
                    self.assertLessEqual(clip.start+clip.duration,4)
                    self.assertLessEqual(clip.duration,2)
                    self.assertEqual(clip.text,'文案')
                self.assertLessEqual(len(result),600)

    def test_caption_timeline_and_escaping(self):
        clips=[Clip('a',0,1),Clip('a',1,1)]
        cues=cues_for(clips,{'subtitle_mode':'manual','subtitle_lines':['one','two']})
        self.assertEqual(cues,[(0,1,'one'),(1,2,'two')])
        text=safe_ass(r'{\pos(1,1)}hello\Nworld')
        self.assertNotIn('{',text)
        self.assertNotIn(r'\pos',text)
        with tempfile.TemporaryDirectory() as tmp:
            write_subtitles(cues,Path(tmp))
            self.assertIn('00:00:00,000 --> 00:00:01,000',(Path(tmp)/'captions.srt').read_text())

    def test_missing_configuration(self):
        with patch.dict(os.environ,{'CUT_LLM_BASE_URL':'','CUT_LLM_MODEL':''}):
            with self.assertRaisesRegex(ValueError,'Set CUT_LLM'):
                ChatPlanner().plan([],{},'seed')

    def test_chinese_font_missing_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp, patch('backend.subtitles.shutil.which',return_value=None):
            with self.assertRaisesRegex(ValueError,'Chinese subtitles require'):
                write_subtitles([(0,1,'中文')],Path(tmp))


class ModelErrors(unittest.TestCase):
    def test_provider_error_does_not_echo_secrets(self):
        error=urllib.error.HTTPError('https://example.com/v1/chat/completions',401,'key=private-secret',{},io.BytesIO(b'private-secret'))
        with patch.dict(os.environ,{'CUT_LLM_BASE_URL':'https://example.com/v1','CUT_LLM_MODEL':'example'}), patch('backend.ai.urllib.request.OpenerDirector.open',side_effect=error):
            with self.assertRaisesRegex(ValueError,'HTTP 401') as caught:
                ChatPlanner().plan([],{'category':'food','duration':2,'clip_seconds':1},'seed')
            self.assertNotIn('private-secret',str(caught.exception))
