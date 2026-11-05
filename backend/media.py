import json
import os
import subprocess
from pathlib import Path

FFMPEG = os.getenv('FFMPEG_BIN', 'ffmpeg')
FFPROBE = os.getenv('FFPROBE_BIN', 'ffprobe')

def run(args, timeout=300):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
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

def render(clips, storage, work, progress):
    """Normalize each clip, concat, validate; publish only after successful probe."""
    parts = []
    for i, clip in enumerate(clips):
        output = work / f'part-{i}.mp4'
        run([FFMPEG, '-nostdin', '-v', 'error', '-y', '-ss', str(clip.start),
             '-i', str(storage.local_path(f'assets/{clip.asset_id}')), '-t', str(clip.duration),
             '-map', '0:v:0', '-an', '-vf',
             'scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
             '-threads', '2', str(output)])
        parts.append(output)
        progress(10 + int(75 * (i + 1) / len(clips)))
    manifest = work / 'concat.txt'
    manifest.write_text('\n'.join(f"file '{p.name}'" for p in parts))
    output = work / 'final.mp4'
    run([FFMPEG, '-nostdin', '-v', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', str(manifest),
         '-c', 'copy', '-movflags', '+faststart', str(output)])
    probe(output)
    return output
