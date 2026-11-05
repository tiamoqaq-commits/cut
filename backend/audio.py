"""Qwen TTS per subtitle cue, cached speech, and quiet uploaded background music."""
import hashlib
import json
import math
import os
import shutil
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from .ai import NoRedirect
from .media import FFMPEG, FFPROBE, run

def tts_config():
    endpoint = os.getenv('CUT_TTS_URL', '').strip()
    key = os.getenv('CUT_TTS_API_KEY', '').strip()
    model = os.getenv('CUT_TTS_MODEL', 'qwen3-tts-flash')
    voice = os.getenv('CUT_TTS_VOICE', 'Cherry')
    u = urlsplit(endpoint)
    if not key or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('Set CUT_TTS_URL and CUT_TTS_API_KEY in .env for narration')
    if u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in ('localhost','127.0.0.1','::1')):
        raise ValueError('TTS endpoint requires HTTPS (except local tests)')
    return endpoint, key, model, voice

def audio_duration(path):
    info = json.loads(run([FFPROBE, '-v','error','-show_streams','-show_format','-of','json',str(path)],30))
    if not any(x['codec_type']=='audio' for x in info.get('streams', [])):
        raise ValueError('File has no audio stream')
    d = float(info['format']['duration'])
    if not math.isfinite(d) or not .05 <= d <= 3600:
        raise ValueError('Audio duration must be 0.05–3600 seconds')
    return d

def synthesize(text, storage):
    endpoint,key,model,voice = tts_config()
    signature = hashlib.sha256(json.dumps([endpoint,model,voice,text],ensure_ascii=False).encode()).hexdigest()
    cache = storage.local_path(f'tts/{signature}.wav')
    if cache.exists():
        audio_duration(cache)
        return cache
    if not 1 <= len(text) <= 600:
        raise ValueError('TTS text must contain 1–600 characters')
    request = urllib.request.Request(endpoint, data=json.dumps({
        'model':model, 'input':{'text':text,'voice':voice,'language_type':'Chinese'}
    }).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request,timeout=90) as r:
            raw = r.read(262145)
        if len(raw)>262144:
            raise ValueError('TTS response too large')
        data=json.loads(raw)
        if data.get('status_code',200)!=200 or data.get('code'):
            raise ValueError('TTS provider rejected request; check model, region, key and quota')
        url=data['output']['audio']['url']
        target=urlsplit(url)
        local = urlsplit(endpoint).hostname in ('localhost','127.0.0.1','::1')
        if local and target.hostname == urlsplit(endpoint).hostname and target.scheme in ('http','https'):
            pass
        elif target.hostname and target.hostname.endswith('.aliyuncs.com') and target.scheme in ('http','https'):
            # Qwen returns signed OSS URLs; upgrade transport without changing signature query.
            target=target._replace(scheme='https')
        else:
            raise ValueError('Unexpected TTS download host')
        if target.username or target.password:
            raise ValueError('Invalid TTS download URL')
        pending=cache.with_suffix('.pending')
        try:
            # No bearer credentials are forwarded to the audio download.
            with opener.open(urlunsplit(target),timeout=90) as response, pending.open('wb') as f:
                total=0
                while chunk:=response.read(65536):
                    total+=len(chunk)
                    if total>30*1024*1024:
                        raise ValueError('TTS audio exceeds 30 MB')
                    f.write(chunk)
            audio_duration(pending)
            os.replace(pending,cache)
        finally:
            pending.unlink(missing_ok=True)
        return cache
    except urllib.error.HTTPError as e:
        raise ValueError(f'TTS HTTP {e.code}; check region, model, key and quota') from None
    except (OSError,urllib.error.URLError):
        raise ValueError('TTS connection/download failed or timed out') from None
    except (KeyError,TypeError,json.JSONDecodeError):
        raise ValueError('Invalid TTS response: missing output.audio.url') from None

def add_audio(video, cues, req, storage, work):
    if req.get('audio_mode','none')=='none':
        return video
    parts=[]
    for i,(start,end,text) in enumerate(cues):
        source=synthesize(text,storage)
        length=audio_duration(source)
        slot=end-start
        speed=max(1.,length/max(.05,slot-.05))
        if speed>2:
            raise ValueError(f'字幕第 {i+1} 句配音过长；请缩短文案或增加成片时长（最大加速2倍）')
        part=work/f'voice-{i}.wav'
        run([FFMPEG,'-v','error','-y','-i',str(source),'-vn','-af',
             f'atempo={speed},apad,atrim=duration={slot}',
             '-ar','48000','-ac','2','-c:a','pcm_s16le',str(part)])
        parts.append(part)
    if not parts:
        raise ValueError('Narration needs subtitle text')
    manifest=work/'voice.txt'
    manifest.write_text('\n'.join(f"file '{p.name}'" for p in parts))
    narration=work/'narration.wav'
    run([FFMPEG,'-v','error','-y','-f','concat','-safe','1','-i',str(manifest),'-c:a','copy',str(narration)])
    duration=sum(end-start for start,end,_ in cues)
    music=storage.local_path('music/'+req['music_id'])
    out=work/'with-audio.mp4'
    # Music loops, fades out, and remains quiet; limiter prevents clipping.
    filters=(f'[1:a]aresample=48000,apad,atrim=duration={duration},volume=1[voice];'
             f'[2:a]aresample=48000,atrim=duration={duration},volume={req["music_volume"]},'
             f'afade=t=out:st={max(0,duration-1)}:d={min(1,duration)}[bgm];'
             '[voice][bgm]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0[a]')
    run([FFMPEG,'-v','error','-y','-i',str(video),'-i',str(narration),'-stream_loop','-1','-i',str(music),
         '-filter_complex',filters,'-map','0:v:0','-map','[a]','-c:v','copy','-c:a','aac','-b:a','192k',
         '-ac','2','-ar','48000','-t',str(duration),'-movflags','+faststart',str(out)])
    audio_duration(out)
    return out
