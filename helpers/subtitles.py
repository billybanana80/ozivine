"""
Ozivine shared subtitle helpers.

Some streaming services expose subtitles as separate sidecar files (WebVTT)
rather than embedding them in the HLS/DASH stream. Each service module parses
its own playback response into a common list-of-dicts:

    [{"url": str, "lang": str, "name": str, "default": bool, "forced": bool}, ...]

and then calls embed_subtitles() to convert them to SRT via ffmpeg and mux them
into the MKV that N_m3u8DL-RE produced.

Requires ffmpeg and mkvmerge on PATH (both are project-level requirements).
"""

import os
import re
import subprocess

from helpers.colors import bcolors


def sanitize_filename(name):
    """Replace characters N_m3u8DL-RE strips from output filenames (cross-platform
    invalid chars, e.g. "?" -> "_"), so the on-disk name is predictable and the
    subtitle mux step can find the file."""
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def describe(subtitles):
    """Short comma-separated language list for display, e.g. "en, ar"."""
    return ", ".join(s.get("lang", "und") for s in subtitles)


def _fetch_and_convert_to_srt(url, srt_path):
    """Use ffmpeg to fetch a WebVTT URL and write it as SRT in one step.
    No intermediate file is created. Raises subprocess.CalledProcessError on failure."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", url, srt_path],
        check=True,
    )


def embed_subtitles(downloads_path, base_name, subtitles):
    """Convert each subtitle track to SRT via ffmpeg (fetching directly from its
    URL), then mux all tracks into {downloads_path}/{base_name}.mkv in place using
    a temp file and atomic replace.

    Each subtitle dict needs at least "url" and "lang"; "name", "default", and
    "forced" are optional and used to set the MKV track metadata.

    Returns True on success, False otherwise.
    """
    video_path = os.path.join(downloads_path, f"{base_name}.mkv")
    if not os.path.exists(video_path):
        print(f"{bcolors.WARNING}Video file not found, skipping subtitle mux: {video_path}{bcolors.ENDC}")
        return False

    multiple = len(subtitles) > 1
    prepared = []
    for index, sub in enumerate(subtitles):
        lang = sub.get("lang", "und")
        suffix = lang if not multiple else f"{lang}.{index}"
        srt_path = os.path.join(downloads_path, f"{base_name}.{suffix}.srt")
        try:
            _fetch_and_convert_to_srt(sub["url"], srt_path)
        except subprocess.CalledProcessError as e:
            print(f"{bcolors.WARNING}Failed to convert {lang} subtitle to SRT: {e}{bcolors.ENDC}")
            continue
        prepared.append({
            "path": srt_path,
            "lang": lang,
            "name": sub.get("name", ""),
            "default": bool(sub.get("default")),
            "forced": bool(sub.get("forced")),
        })
        print(f"{bcolors.OKGREEN}✅ Downloaded and converted {lang} subtitle{bcolors.ENDC}")

    if not prepared:
        print(f"{bcolors.WARNING}No subtitle files prepared; nothing to mux.{bcolors.ENDC}")
        return False

    tmp_path = os.path.join(downloads_path, f"{base_name}.tmp.mkv")
    command = ["mkvmerge", "-o", tmp_path, video_path]
    for sub in prepared:
        command += ["--language", f"0:{sub['lang']}"]
        if sub["name"]:
            command += ["--track-name", f"0:{sub['name']}"]
        command += [
            "--default-track-flag", f"0:{'yes' if sub['default'] else 'no'}",
            "--forced-display-flag", f"0:{'yes' if sub['forced'] else 'no'}",
            sub["path"],
        ]

    result = subprocess.run(command)
    # mkvmerge exit codes: 0 = success, 1 = success with warnings, 2 = error.
    if result.returncode in (0, 1):
        os.replace(tmp_path, video_path)
        for sub in prepared:
            try:
                os.remove(sub["path"])
            except OSError:
                pass
        print(f"{bcolors.OKGREEN}✅ Embedded {len(prepared)} subtitle track(s) into {base_name}.mkv{bcolors.ENDC}")
        return True

    print(f"{bcolors.FAIL}mkvmerge failed (exit {result.returncode}); leaving .srt files in place.{bcolors.ENDC}")
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    return False
