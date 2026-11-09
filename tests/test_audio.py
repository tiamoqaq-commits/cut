import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from backend.audio import add_audio, synthesize, tts_config

class AudioValidation(unittest.TestCase):
    def test_silent_mode_unchanged(self):
        p=Path('video.mp4')
        self.assertEqual(add_audio(p,[],{},None,None),p)

    def test_missing_tts_key(self):
        with patch.dict(os.environ,{'CUT_TTS_URL':'https://dashscope.aliyuncs.com/tts','CUT_TTS_API_KEY':''}):
            with self.assertRaisesRegex(ValueError,'CUT_TTS'):
                tts_config()

    def test_speech_too_long_never_truncates(self):
        with patch('backend.audio.synthesize',return_value=Path('voice.wav')),patch('backend.audio.audio_duration',return_value=10):
            with self.assertRaisesRegex(ValueError,'配音过长'):
                add_audio(Path('video.mp4'),[(0,1,'long')],{'audio_mode':'tts_bgm'},None,Path('.'))

    def test_error_body_never_leaks_key(self):
        class Storage:
            def local_path(self,key):return Path(tmp)/'audio.wav'
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'CUT_TTS_URL':'https://dashscope.aliyuncs.com/tts','CUT_TTS_API_KEY':'tts-secret'}):
            e=urllib.error.HTTPError('https://example.com',401,'tts-secret',{},io.BytesIO(b'tts-secret'))
            with patch('backend.audio.urllib.request.OpenerDirector.open',side_effect=e):
                with self.assertRaisesRegex(ValueError,'TTS HTTP 401') as caught:
                    synthesize('test',Storage())
                self.assertNotIn('tts-secret',str(caught.exception))
