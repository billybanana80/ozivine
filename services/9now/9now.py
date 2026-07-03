import requests
import re
import json
import subprocess
import os
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import base64
import binascii
import datetime
import urllib3
from colors import bcolors
import icons
from services.proxy import append_downloader_proxy, current_proxy_url, mask_proxy_command

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

#   9Now Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the MPD, Licence, PSSH and Decryption keys.
#   eg: https://www.9now.com.au/paramedics/season-5/episode-10
#   Authentication: None
#   Geo-Locking: requires an Australian IP address
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the 9Now URL to extract the series name, season, and episode number, and then fetches the Brightcove video ID from the 9Now API.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for both encrypted and non-encrypted video files.

# Constants for API URLs and headers
BRIGHTCOVE_KEY = "BCpkADawqM1TWX5yhWjKdzhXnHCmGvnaozGSDICiEFNRv0fs12m6WA2hLxMHM8TGAEM6pv7lhJsdNhiQi76p4IcsT_jmXdtEU-wnfXhOBTx-cGR7guCqVwjyFAtQa75PFF-TmWESuiYaNTzg"
BRIGHTCOVE_ACCOUNT = "4460760524001"
BRIGHTCOVE_HEADERS = {
    "BCOV-POLICY": BRIGHTCOVE_KEY,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.9now.com.au",
    "Referer": "https://www.9now.com.au/"
}
BRIGHTCOVE_API = lambda video_id: f"https://edge.api.brightcove.com/playback/v1/accounts/{BRIGHTCOVE_ACCOUNT}/videos/{video_id}"

def apply_9now_proxy_stability_options(command, thread_count=4, retry_count=10):
    if not current_proxy_url():
        return command
    command = re.sub(r"\s--thread-count\s+\d+", "", command)
    command = re.sub(r"\s--download-retry-count\s+\d+", "", command)
    command = re.sub(r"\s--http-request-timeout\s+\d+", "", command)
    return (
        f"{command} "
        f"--thread-count {thread_count} "
        f"--download-retry-count {retry_count} "
        f"--http-request-timeout 60"
    )

def retry_9now_proxy_download(command):
    if not current_proxy_url():
        return None

    retry_command = apply_9now_proxy_stability_options(
        command,
        thread_count=1,
        retry_count=20,
    )
    print(f"{bcolors.YELLOW}{icons.ICON_WARNING} 9Now proxy download failed; retrying with single-threaded segment downloads...{bcolors.ENDC}")
    print(mask_proxy_command(retry_command))
    return subprocess.run(retry_command, shell=True)


def _season_tag_from_slug(slug: str) -> str:
    """
    season-20252026 -> S2025
    season-5        -> S05
    fallback        -> S00
    """
    if not slug or not slug.startswith("season-"):
        return "S00"
    rest = slug[len("season-"):]
    # year span 2025/2026 style as 20252026
    m = re.match(r"(\d{4})(\d{4})", rest)
    if m:
        return f"S{m.group(1)}"
    # plain number
    m2 = re.match(r"(\d+)", rest)
    if m2:
        return f"S{int(m2.group(1)):02d}"
    return "S00"

def _get_season_page(series_name: str, season_slug: str):
    url = f"https://tv-api.9now.com.au/v2/pages/tv-series/{series_name}/seasons/{season_slug}?device=web"
    r = requests.get(url, timeout=20)
    if r.status_code == 200:
        return r.json()
    return None

def _extract_clips_from_page(page_json):
    """
    Find clip items from the page rails.
    Returns list of dicts (clip items).
    """
    out = []
    if not page_json:
        return out
    for block in page_json.get("items", []):
        for it in block.get("items", []):
            if it.get("type") == "clip":
                out.append(it)
    return out

def _clip_sort_key(c):
    dt = c.get('availability') or c.get('updatedAt') or ""
    for f in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.datetime.strptime(dt, f)
        except Exception:
            pass
    return datetime.datetime.min

def get_video_id_from_url(video_url):
    # Episodes (existing patterns)
    season_episode_match = re.search(r'9now\.com\.au/([^/]+)/season-(\d+)/episode-(\d+)', video_url)
    year_episode_match   = re.search(r'9now\.com\.au/([^/]+)/(\d{4})/episode-(\d+)', video_url)
    special_episode_match= re.search(r'9now\.com\.au/([^/]+)/special/episode-(\d+)', video_url)

    # NEW: Clips
    # e.g. https://www.9now.com.au/premier-league-epl-football/season-20252026/clip-cmeop4x67000m0hmmc1822v1i
    clip_match = re.search(r'9now\.com\.au/([^/]+)/(?P<season>season-[^/]+)/(?P<clip>clip-[^/?#]+)', video_url)

    if season_episode_match:
        series_name, season, episode = season_episode_match.groups()
        api_url = f"https://tv-api.9now.com.au/v2/pages/tv-series/{series_name}/seasons/season-{season}/episodes/episode-{episode}?device=web"
        data = requests.get(api_url).json()
        try:
            video_id = data['episode']['video']['brightcoveId']
            return series_name, f"S{int(season):02}", f"E{int(episode):02}", video_id
        except KeyError:
            raise ValueError("Could not find the video ID in the API response.")

    elif special_episode_match:
        series_name, episode = special_episode_match.groups()
        api_url = f"https://tv-api.9now.com.au/v2/pages/tv-series/{series_name}/seasons/special/episodes/episode-{episode}?device=web"
        data = requests.get(api_url).json()
        try:
            video_id = data['episode']['video']['brightcoveId']
            return series_name, "S00", f"E{int(episode):02}", video_id
        except KeyError:
            raise ValueError("Could not find the video ID in the API response.")

    elif year_episode_match:
        series_name, year, episode = year_episode_match.groups()
        api_url = f"https://tv-api.9now.com.au/v2/pages/tv-series/{series_name}?device=web"
        data = requests.get(api_url).json()
        try:
            episodes = data['items'][0]['items']
            for item in episodes:
                if item.get('episodeNumber') == int(episode):
                    video_id = item['video']['brightcoveId']
                    return series_name, f"S{year}", f"E{int(episode):02}", video_id
            raise ValueError("Could not find the episode in the API response.")
        except KeyError:
            raise ValueError("Could not find the video ID in the API response.")

    elif clip_match:
        series_name = clip_match.group(1)
        season_slug = clip_match.group('season')          # e.g. season-20252026
        clip_slug   = clip_match.group('clip')            # e.g. clip-cmeop4x...

        # Fetch the season landing page, find the clip by its link path
        season_page = _get_season_page(series_name, season_slug)
        clips = _extract_clips_from_page(season_page)

        # Sort newest -> oldest, then find index
        clips_sorted = sorted(clips, key=_clip_sort_key, reverse=True)

        # The webUrl usually looks like "/{series}/{season}/{clip-slug}".
        # Some 9Now rail links now append a context suffix after the clip slug.
        target_path_tail = f"/{series_name}/{season_slug}/{clip_slug}".lower()

        found = None
        for idx, c in enumerate(clips_sorted, start=1):
            web_url = ((c.get('link') or {}).get('webUrl') or '').lower().rstrip("/")
            if web_url.endswith(target_path_tail) or f"{target_path_tail}/" in web_url:
                found = (idx, c)
                break

        if not found:
            raise ValueError("Clip not found on the season page rails (URL mismatch).")

        clip_idx, clip_obj = found
        video_id = (clip_obj.get('video') or {}).get('brightcoveId')
        if not video_id:
            raise ValueError("Brightcove ID missing for the matched clip.")


        # Clean up the display name for filesystem use
        raw_title = clip_obj.get('displayName') or clip_obj.get('name') or ''
        safe_title = re.sub(r'[^A-Za-z0-9]+', '.', raw_title)
        safe_title = re.sub(r'\.+', '.', safe_title).strip('.')

        season_tag = _season_tag_from_slug(season_slug)
        episode_tag = f"C{clip_idx:02d}"

        return series_name, season_tag, episode_tag, video_id, safe_title

    else:
        raise ValueError("Could not extract series name, season/year/clip from the URL.")


# Function to get PSSH from MPD URL
def get_pssh(url_mpd):
    try:
        response = requests.get(url_mpd)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        pssh_elements = root.findall(".//{urn:mpeg:dash:schema:mpd:2011}ContentProtection")

        for elem in pssh_elements:
            pssh = elem.find("{urn:mpeg:cenc:2013}pssh")
            if pssh is not None and pssh.text:
                pssh_data = pssh.text.strip()
                try:
                    base64.b64decode(pssh_data)  # Validate Base64
                    return pssh_data
                except binascii.Error as e:
                    print(f"Invalid PSSH data: {e}")
    except Exception as e:
        print(f"Error fetching PSSH: {e}")
    return None

# Function to get maximum video height from MPD URL
def get_max_height_mpd(url_mpd):
    try:
        response = requests.get(url_mpd)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        max_height = 0
        for rep in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation"):
            height = rep.get('height')
            if height is not None:
                max_height = max(max_height, int(height))
        return max_height
    except Exception as e:
        print(f"Error fetching max height from MPD: {e}")
    return 0

# Function to get maximum video height from m3u8 URL
def get_max_height_m3u8(url_m3u8):
    try:
        response = requests.get(url_m3u8)
        response.raise_for_status()
        max_height = 0
        for line in response.text.splitlines():
            if "RESOLUTION" in line:
                resolution = re.search(r"RESOLUTION=\d+x(\d+)", line)
                if resolution:
                    height = int(resolution.group(1))
                    max_height = max(max_height, height)
        return max_height
    except Exception as e:
        print(f"Error fetching max height from m3u8: {e}")
    return 0

def get_mpd_streams(url_mpd):
    streams = []
    try:
        response = requests.get(url_mpd)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for adaptation in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}AdaptationSet"):
            mime_type = adaptation.get("mimeType", "")
            content_type = adaptation.get("contentType", "")
            language = adaptation.get("lang", "")
            if "video" in mime_type or content_type == "video":
                stream_type = "video"
            elif "audio" in mime_type or content_type == "audio":
                stream_type = "audio"
            elif "text" in mime_type or "ttml" in mime_type or content_type == "text":
                stream_type = "subtitle"
            else:
                stream_type = "stream"

            for rep in adaptation.findall("{urn:mpeg:dash:schema:mpd:2011}Representation"):
                height = rep.get("height")
                width = rep.get("width")
                bandwidth = rep.get("bandwidth")
                codecs = rep.get("codecs") or adaptation.get("codecs", "")
                streams.append({
                    "type": stream_type,
                    "resolution": f"{width}x{height}" if width and height else "",
                    "bandwidth": int(bandwidth) if str(bandwidth or "").isdigit() else 0,
                    "codecs": codecs,
                    "language": language,
                })
    except Exception as e:
        print(f"Error fetching MPD streams: {e}")
    return sorted(streams, key=lambda item: (item["type"] != "video", -item["bandwidth"]))

def get_m3u8_streams(url_m3u8):
    streams = []
    try:
        response = requests.get(url_m3u8)
        response.raise_for_status()
        pending = None
        for line in response.text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                resolution = re.search(r"RESOLUTION=(\d+x\d+)", line)
                bandwidth = re.search(r"BANDWIDTH=(\d+)", line)
                codecs = re.search(r'CODECS="([^"]+)"', line)
                pending = {
                    "type": "video",
                    "resolution": resolution.group(1) if resolution else "?",
                    "bandwidth": int(bandwidth.group(1)) if bandwidth else 0,
                    "codecs": codecs.group(1) if codecs else "",
                    "url": "",
                }
            elif pending and line and not line.startswith("#"):
                pending["url"] = line
                streams.append(pending)
                pending = None
    except Exception as e:
        print(f"Error fetching m3u8 streams: {e}")
    return sorted(streams, key=lambda item: item["bandwidth"], reverse=True)

def print_streams(streams):
    if not streams:
        print(f"\n{bcolors.WARNING}No stream variants found.{bcolors.ENDC}")
        return

    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    header = f"  {'#':>2}  {'Type':<4} {'Resolution':<10} {'Bitrate':<16} {'Codec':<18} {'Lang':<5}"
    divider = f"  {'-' * 2}  {'-' * 4} {'-' * 10} {'-' * 16} {'-' * 18} {'-' * 5}"
    print(header)
    print(divider)
    for idx, stream in enumerate(streams, start=1):
        kbps = round(stream.get("bandwidth", 0) / 1000)
        bitrate = f"{kbps} Kbps" if kbps else "unknown bitrate"
        codecs = stream.get("codecs") or "unknown codecs"
        stream_type = stream.get("type", "stream")
        if stream_type == "video":
            label = "Vid"
            resolution = stream.get("resolution") or "-"
        elif stream_type == "audio":
            label = "Aud"
            resolution = "-"
        elif stream_type == "subtitle":
            label = "Sub"
            resolution = "-"
        else:
            label = "Stream"
            resolution = stream.get("resolution") or "-"

        language = stream.get("language") or "-"
        print(f"  {idx:>2}  {label:<4} {resolution:<10} {bitrate:<16} {codecs:<18} {language:<5}")

def subtitle_extension(track, content):
    mime_type = (track.get("mime_type") or "").lower()
    path = urlparse(track.get("src") or "").path.lower()
    text = content.lstrip("\ufeff").lstrip()

    if text.startswith("WEBVTT") or "webvtt" in mime_type or path.endswith(".vtt"):
        return "srt"
    if "ttml" in mime_type or path.endswith((".ttml", ".dfxp")) or text.startswith("<tt"):
        return "ttml"
    if path.endswith(".srt") or re.search(r"(?m)^\d+\s*\n\d{2}:\d{2}:\d{2},\d{3}\s+-->", text):
        return "srt"
    return "srt"

def subtitle_has_real_cues(content):
    text = content.lstrip("\ufeff").strip()
    if not text:
        return False

    if text.startswith("WEBVTT"):
        return "-->" in text
    if text.startswith("<") and "<tt" in text[:300].lower():
        return bool(re.search(r"\bbegin\s*=", text, re.IGNORECASE))
    return bool(re.search(r"(?m)^\d+\s*\n\d{2}:\d{2}:\d{2},\d{3}\s+-->", text))

def get_valid_external_subtitles(brightcove_response):
    subtitles = []
    seen_urls = set()

    for track in brightcove_response.get("text_tracks") or []:
        src = track.get("src")
        kind = (track.get("kind") or "").lower()
        label = (track.get("label") or "").lower()
        language = (track.get("srclang") or "").strip().lower()

        if not src or src in seen_urls:
            continue
        if kind not in {"captions", "subtitles"}:
            continue
        if label in {"thumbnail", "thumbnails"}:
            continue

        try:
            response = requests.get(src, headers=BRIGHTCOVE_HEADERS, timeout=20)
            response.raise_for_status()
            content = response.text
        except Exception:
            continue

        if not subtitle_has_real_cues(content):
            continue

        seen_urls.add(src)
        subtitles.append({
            "url": src,
            "language": language or "und",
            "label": track.get("label") or language or "Subtitle",
            "kind": kind,
            "extension": subtitle_extension(track, content),
            "content": content,
        })

    return subtitles

def print_external_subtitles(subtitles):
    if not subtitles:
        return

    print(f"\n{bcolors.YELLOW}External subtitles:{bcolors.ENDC}")
    header = f"  {'#':>2}  {'Lang':<5} {'Kind':<10} {'Format':<6} {'Label':<20}"
    divider = f"  {'-' * 2}  {'-' * 5} {'-' * 10} {'-' * 6} {'-' * 20}"
    print(header)
    print(divider)
    for idx, subtitle in enumerate(subtitles, start=1):
        print(
            f"  {idx:>2}  "
            f"{subtitle.get('language', '-'):<5} "
            f"{subtitle.get('kind', '-'):<10} "
            f"{subtitle.get('extension', '-'):<6} "
            f"{subtitle.get('label', '-'):<20}"
        )

def vtt_timestamp_to_srt(timestamp):
    return timestamp.replace(".", ",")

def vtt_to_srt(vtt_text):
    text = vtt_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    current = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            if current:
                cues.append(current)
                current = []
            continue
        if stripped == "WEBVTT" or stripped.startswith(("NOTE", "STYLE", "REGION", "X-TIMESTAMP-MAP")):
            continue
        current.append(stripped)

    if current:
        cues.append(current)

    srt_blocks = []
    for cue in cues:
        timing_index = next((idx for idx, line in enumerate(cue) if "-->" in line), None)
        if timing_index is None:
            continue

        timing = cue[timing_index]
        match = re.match(
            r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})",
            timing,
        )
        if not match:
            continue

        text_lines = cue[timing_index + 1:]
        if not text_lines:
            continue

        srt_blocks.append(
            f"{len(srt_blocks) + 1}\n"
            f"{vtt_timestamp_to_srt(match.group('start'))} --> {vtt_timestamp_to_srt(match.group('end'))}\n"
            f"{chr(10).join(text_lines)}"
        )

    if not srt_blocks:
        return None

    return "\n\n".join(srt_blocks) + "\n"

def subtitle_content_for_save(subtitle):
    content = subtitle.get("content") or ""
    if subtitle.get("extension") == "srt":
        converted = vtt_to_srt(content)
        if converted:
            return converted
    return content

def subtitle_filename(base_name, subtitle, index, used_names):
    language = re.sub(r"[^A-Za-z0-9]+", "", subtitle.get("language") or "und") or "und"
    extension = subtitle.get("extension") or "srt"
    name = f"{base_name}.{language}.{extension}"
    if name in used_names:
        name = f"{base_name}.{language}.{index}.{extension}"
    used_names.add(name)
    return name

def save_external_subtitles(subtitles, downloads_path, formatted_file_name):
    if not subtitles:
        return

    os.makedirs(downloads_path, exist_ok=True)
    used_names = set()
    for idx, subtitle in enumerate(subtitles, start=1):
        print(f"{bcolors.OKCYAN}{icons.ICON_WAITING} Processing subtitle:{bcolors.ENDC} {subtitle.get('language', 'und')} {subtitle.get('label', 'Subtitle')}")
        content = subtitle_content_for_save(subtitle)
        if not content:
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Subtitle skipped: no usable cues found{bcolors.ENDC}")
            continue

        filename = subtitle_filename(formatted_file_name, subtitle, idx, used_names)
        path = os.path.join(downloads_path, filename)
        with open(path, "w", encoding="utf-8-sig", newline="") as file:
            file.write(content)
        print(f"{bcolors.GREEN}{icons.ICON_SUCCESS} Subtitle saved:{bcolors.ENDC} {path}")

def format_base_name(series_name, season, episode, max_height, clip_title=None):
    base_name = series_name.title().replace('-', '.').replace(' ', '.').replace('_', '.').replace('/', '.').replace(':', '.')
    if clip_title:
        return f"{base_name}.{clip_title}.{season}{episode}.{max_height}p.9NOW.WEB-DL.AAC2.0.H.264"
    return f"{base_name}.{season}{episode}.{max_height}p.9NOW.WEB-DL.AAC2.0.H.264"

def build_9now_command(source_url, downloads_path, formatted_file_name, keys=None, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    command = (
        f'N_m3u8DL-RE "{source_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}"'
    )
    if keys:
        command += " --key " + " --key ".join(keys)
    command = apply_9now_proxy_stability_options(command)
    return append_downloader_proxy(command)

def print_9now_info(source_url, source_type, formatted_file_name, lic_url=None, pssh=None, keys=None, subtitles=None):
    if source_type == "mpd":
        print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{source_url}")
        if lic_url:
            print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
        if pssh:
            print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
        for key in keys or []:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
        print_streams(get_mpd_streams(source_url))
    else:
        print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{source_url}")
        print_streams(get_m3u8_streams(source_url))
    print_external_subtitles(subtitles or [])
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")

# Function to get keys using PSSH and license URL
def get_keys(pssh, lic_url, wvd_device_path):
    try:
        pssh = PSSH(pssh)
    except binascii.Error as e:
        print(f"Could not decode PSSH data as Base64: {e}")
        return []

    try:
        device = Device.load(wvd_device_path)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, pssh)
        
        # Headers for the license request
        headers = {
            'Content-Type': 'application/octet-stream',
            'Origin': 'https://www.9now.com.au',
            'Referer': 'https://www.9now.com.au/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }

        # Make the license request
        licence = requests.post(lic_url, headers=headers, data=challenge)
        
        # Check for errors in the response
        try:
            licence.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"HTTPError: {e}")
            print(f"Response Headers: {licence.headers}")
            print(f"Response Text: {licence.text}")
            raise

        # Parse the license response
        cdm.parse_license(session_id, licence.content)
        keys = [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
        cdm.close(session_id)
        return keys
    except Exception as e:
        print(f"Error fetching keys: {e}")
    return []

# Function to process and print the download command
def get_download_command(video_url, downloads_path, wvd_device_path, mode="auto"):
    video_info = get_video_id_from_url(video_url)

    # Handle clip (5 values) vs episode (4 values)
    if len(video_info) == 5:
        series_name, season, episode, video_id, clip_title = video_info
    else:
        series_name, season, episode, video_id = video_info
        clip_title = None

    session = requests.Session()  # Use a session to maintain cookies and headers
    response = session.get(BRIGHTCOVE_API(video_id), headers=BRIGHTCOVE_HEADERS).json()
    subtitles = get_valid_external_subtitles(response)
    
    download_command = None
    formatted_file_name = None
    
    if 'sources' in response:
        sources = response['sources']
        source = next((src for src in sources if 'key_systems' in src and 'com.widevine.alpha' in src['key_systems']), None)
        if source:
            mpd_url = source['src']
            lic_url = source['key_systems']['com.widevine.alpha']['license_url']
            pssh = get_pssh(mpd_url)
            max_height = get_max_height_mpd(mpd_url)
            if pssh:
                formatted_file_name = format_base_name(series_name, season, episode, max_height, clip_title)
                if mode == "info":
                    keys = get_keys(pssh, lic_url, wvd_device_path)
                    print_9now_info(mpd_url, "mpd", formatted_file_name, lic_url, pssh, keys, subtitles)
                    return

                keys = get_keys(pssh, lic_url, wvd_device_path)
                print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
                print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
                print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
                for key in keys:
                    print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")

                download_command = build_9now_command(
                    mpd_url,
                    downloads_path,
                    formatted_file_name,
                    keys,
                    interactive=(mode == "interactive"),
                )
                print(mask_proxy_command(download_command))
                print_external_subtitles(subtitles)
        else:
            # Handling for unencrypted videos with m3u8
            unencrypted_source = next((src for src in sources if 'src' in src and 'master.m3u8' in src['src']), None)
            if unencrypted_source:
                m3u8_url = unencrypted_source['src']
                max_height = get_max_height_m3u8(m3u8_url)
                formatted_file_name = format_base_name(series_name, season, episode, max_height, clip_title)
                if mode == "info":
                    print_9now_info(m3u8_url, "m3u8", formatted_file_name, subtitles=subtitles)
                    return

                print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{m3u8_url}")
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")

                download_command = build_9now_command(
                    m3u8_url,
                    downloads_path,
                    formatted_file_name,
                    interactive=(mode == "interactive"),
                )
                print(mask_proxy_command(download_command))
                print_external_subtitles(subtitles)
            else:
                print("No suitable source found for unencrypted video")
    else:
        print("No 'sources' found in the response")
    
    if download_command:
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == 'y':
            print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
            result = subprocess.run(download_command, shell=True)
            download_ok = result.returncode == 0
            if result.returncode != 0:
                retry_result = retry_9now_proxy_download(download_command)
                download_ok = bool(retry_result and retry_result.returncode == 0)
            if download_ok:
                save_external_subtitles(subtitles, downloads_path, formatted_file_name)
                print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
        else:
            print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")


# Main execution flow
def main(video_url, downloads_path, wvd_device_path, mode="auto"):
    get_download_command(video_url, downloads_path, wvd_device_path, mode)

