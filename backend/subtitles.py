"""Generate plain SRT and escaped ASS; do not interpret subtitle text as ASS commands."""
import os
import re
import shutil
import subprocess
import unicodedata

def cues_for(clips, request):
    mode = request.get('subtitle_mode', 'none')
    if mode == 'manual':
        lines = request['subtitle_lines']
        step = sum(c.duration for c in clips) / len(lines)
        return [(i * step, (i + 1) * step, text) for i, text in enumerate(lines)]
    if mode == 'ai':
        cues, cursor = [], 0.0
        for clip in clips:
            cues.append((round(cursor * 30) / 30, round((cursor + clip.duration) * 30) / 30, clip.text))
            cursor += clip.duration
        return cues
    return []

def stamp(seconds, ass=False):
    units = 100 if ass else 1000
    n = round(seconds * units)
    hours, n = divmod(n, 3600 * units)
    minutes, n = divmod(n, 60 * units)
    secs, fraction = divmod(n, units)
    return f'{hours}:{minutes:02}:{secs:02}.{fraction:02}' if ass else f'{hours:02}:{minutes:02}:{secs:02},{fraction:03}'

def safe_ass(text):
    # Remove control sequences and substitute all override delimiters before wrapping.
    text = ''.join(c for c in text if not unicodedata.category(c).startswith('C'))
    text = text.replace('\\', '／').replace('{', '（').replace('}', '）')
    return r'\N'.join(text[i:i+18] for i in range(0, len(text), 18))

def write_subtitles(cues, work):
    font = os.getenv('CUT_SUBTITLE_FONT', 'Noto Sans CJK SC')
    if not re.fullmatch(r'[\w -]{1,80}', font):
        raise ValueError('CUT_SUBTITLE_FONT must be a font family name')
    # Fail clearly when Chinese glyph coverage is absent instead of burning empty squares.
    if any('\u3400' <= c <= '\u9fff' for _, _, text in cues for c in text):
        if not shutil.which('fc-list'):
            raise ValueError('Chinese subtitles require fontconfig and a Chinese font (e.g. Noto Sans CJK SC)')
        fonts = subprocess.run(['fc-list', ':lang=zh', 'family'],capture_output=True,text=True,timeout=10)
        if fonts.returncode or not fonts.stdout.strip():
            raise ValueError('Install a Chinese font (e.g. fonts-noto-cjk) before rendering Chinese subtitles')
    (work / 'captions.srt').write_text('\n\n'.join(
        f'{i+1}\n{stamp(a)} --> {stamp(b)}\n{text}' for i,(a,b,text) in enumerate(cues)) + '\n', encoding='utf-8')
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,45,45,100,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    (work / 'captions.ass').write_text(header + '\n'.join(
        f'Dialogue: 0,{stamp(a,True)},{stamp(b,True)},Default,,0,0,0,,{safe_ass(text)}' for a,b,text in cues) + '\n',encoding='utf-8')
