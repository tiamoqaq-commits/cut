import json
import os
import subprocess
from pathlib import Path

FFMPEG = os.getenv('FFMPEG_BIN', 'ffmpeg')
FFPROBE = os.getenv('FFPROBE_BIN', 'ffprobe')

def run(args, timeout=300, cwd=None):
    result = subprocess.run(args, capture_output=True, timeout=timeout, cwd=cwd)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace')[-2000:])
    return result.stdout

def probe(path):
    info = json.loads(run([FFPROBE, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], 30))
    if not any(s['codec_type'] == 'video' for s in info['streams']):
        raise ValueError('No video stream')
    duration = float(info['format']['duration'])
    if not 0.1 <= duration <= 3600:
        raise ValueError('Video duration must be 0.1–3600 seconds')
    return duration

def render(clips, storage, work, progress, cues=None):
    """Normalize each clip, concat, validate; publish only after successful probe."""
    parts = []
    cursor = 0.0
    for i, clip in enumerate(clips):
        output = work / f'part-{i}.mp4'
        frames = round((cursor + clip.duration) * 30) - round(cursor * 30)
        cursor += clip.duration
        run([FFMPEG, '-nostdin', '-v', 'error', '-y', '-ss', str(clip.start),
             '-i', str(storage.local_path(f'assets/{clip.asset_id}')), '-frames:v', str(frames),
             '-map', '0:v:0', '-an', '-vf',
             'scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=1',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
             '-threads', '2', str(output)])
        parts.append(output)
        progress(10 + int(75 * (i + 1) / len(clips)))
    manifest = work / 'concat.txt'
    manifest.write_text('\n'.join(f"file '{p.name}'" for p in parts))
    output = work / 'final.mp4'
    run([FFMPEG, '-nostdin', '-v', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', str(manifest),
         '-c', 'copy', '-movflags', '+faststart', str(output)])
    if cues:
        from .subtitles import write_subtitles
        write_subtitles(cues, work)
        burned = work / 'subtitled.mp4'
        # Fixed relative filename prevents filter path quoting/injection issues.
        run([FFMPEG, '-nostdin', '-v', 'error', '-y', '-i', 'final.mp4',
             '-vf', 'ass=captions.ass', '-an', '-c:v', 'libx264', '-preset', 'veryfast',
             '-crf', '23', '-pix_fmt', 'yuv420p', '-threads', '2', '-movflags', '+faststart',
             'subtitled.mp4'], cwd=work)
        output = burned
        progress(95)
    probe(output)
    return output
