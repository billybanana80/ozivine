import requests
import datetime as dt
import base64
import subprocess
import re
import os
import random
from urllib.parse import urljoin  

#   Ozivine: 10Play Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the m3u8 Manifest.
#   eg: https://10play.com.au/south-park/episodes/season-15/episode-6/tpv240705gpchj
#   Authentication: Login
#   Geo-Locking: requires an Australian IP address
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the 10Play video URL to extract the video id and then fetches the show/movie info from the 10Play API.
#   2. Print Download Information: Outputs the M3U8 URL required for downloading the video content.
#   3. Note: this script functions for AES_128 encrypted video files only.

# Formatting for output
class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    LIGHTBLUE = '\033[94m'
    YELLOW = '\033[93m'
    ORANGE = '\033[93m'

### Helpers ###

def fetch_text(url, timeout=15):
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    })
    r.raise_for_status()
    return r.text

def get_first_segment_url(media_m3u8_text: str, media_m3u8_url: str) -> str | None:
    """
    Return absolute URL of the first segment in a media playlist.
    """
    for line in media_m3u8_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # First non-tag line is a segment URI
        return urljoin(media_m3u8_url, line)
    return None

def _extract_segment_urls(media_m3u8_text: str, media_m3u8_url: str):
    segs = []
    for line in media_m3u8_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        segs.append(urljoin(media_m3u8_url, s))
    return segs

def _tiny_get_ok(url: str, timeout=8) -> bool:
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                     "Range": "bytes=0-1"},  # try to fetch 1–2 bytes
            stream=True,
            allow_redirects=True,
        )
        return r.status_code in (200, 206)
    except Exception:
        return False

def probe_segment_ok(m3u8_source: str, timeout=10) -> bool:
    """
    Robust probe: test ~3 segments (first/middle/near-end) with tiny GET requests.
    Works for local .m3u8 paths and remote URLs.
    """
    try:
        if os.path.exists(m3u8_source):  # local file
            with open(m3u8_source, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            base_url = ""
        else:  # remote
            r = requests.get(m3u8_source, timeout=timeout, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            })
            r.raise_for_status()
            text = r.text
            base_url = m3u8_source

        segs = _extract_segment_urls(text, base_url)
        if not segs:
            return False

        # pick indices to test
        idxs = {0, len(segs) // 2, max(0, len(segs) - 2)}
        ok = True
        for i in sorted(idxs):
            if not _tiny_get_ok(segs[i], timeout=timeout):
                ok = False
                break
        return ok
    except Exception:
        return False

def parse_master_variants(master_text: str, master_url: str):
    """
    Parse a master playlist; return list of dicts with height and absolute URL.
    """
    variants = []
    last_inf = None
    for line in master_text.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-STREAM-INF:"):
            last_inf = s
        elif last_inf and s and not s.startswith("#"):
            # parse RESOLUTION=WxH if present
            m = re.search(r"RESOLUTION=\s*(\d+)\s*x\s*(\d+)", last_inf)
            height = int(m.group(2)) if m else 0
            variants.append({
                "height": height,
                "url": urljoin(master_url, s)
            })
            last_inf = None
    # sort by height desc
    variants.sort(key=lambda v: v["height"], reverse=True)
    return variants

def pick_best_variant(master_url: str) -> tuple[str, int] | tuple[None, None]:
    """
    Return (variant_url, height) for the best available rendition.
    """
    try:
        text = fetch_text(master_url)
        variants = parse_master_variants(text, master_url)
        if not variants:
            return None, None
        top = variants[0]
        return top["url"], top["height"] or 0
    except Exception:
        return None, None

def replace_resolution_tag(save_name: str, new_height: int) -> str:
    """
    Replace .1080p. (or any .####p.) in the save-name with the detected height.
    If no tag present, append .{height}p. before the service tag.
    """
    if new_height <= 0:
        return save_name
    if re.search(r"\.(\d{3,4})p\.", save_name):
        return re.sub(r"\.(\d{3,4})p\.", f".{new_height}p.", save_name, count=1)
    # try to insert before ".10Play." or at end
    return re.sub(r"(\.10Play\.)", f".{new_height}p.\\1", save_name, count=1) \
           if ".10Play." in save_name else f"{save_name}.{new_height}p"

### End of Helpers ###

# URLs and Headers
login_url = 'https://10play.com.au/api/user/auth'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://10play.com.au',
    'Referer': 'https://10play.com.au/'
}

# Function to get bearer token
def get_bearer_token(username, password):
    timestamp = dt.datetime.now().strftime('%Y%m%d000000')
    auth_header = base64.b64encode(timestamp.encode('ascii')).decode('ascii')
    login_payload = {'email': username, 'password': password}
    login_headers = headers.copy()
    login_headers['X-Network-Ten-Auth'] = auth_header

    response = requests.post(login_url, json=login_payload, headers=login_headers)
    if response.status_code == 200:
        data = response.json()
        if 'jwt' in data:
            return 'Bearer ' + data['jwt']['accessToken']
    return None


# Function to build authorization headers
def _build_auth_headers(token: str) -> dict:
    return {
        "Authorization": token,  # already "Bearer …"
        "tp-acceptfeature": "v1/fw;v1/drm",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Origin": "https://10play.com.au",
        "Referer": "https://10play.com.au/",
    }


# Function to extract video details and manifest URL
def extract_video_details(video_id, token, episode_url):
    video_api_url = f"https://10play.com.au/api/v1/videos/{video_id}"

    # include tp-acceptfeature + bearer
    auth_headers = _build_auth_headers(token)

    # 1) Get the video doc 
    response = requests.get(video_api_url, headers=auth_headers, timeout=20)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to fetch video details ({response.status_code}){bcolors.ENDC}")
        return None, None

    video_data = response.json()

    # 2) Playback endpoint 
    playback_url = f"https://10play.com.au/api/v1/videos/playback/{video_id}?platform=tizen"
    playback_response = requests.get(playback_url, headers=auth_headers, timeout=20)
    if playback_response.status_code != 200:
        print(f"{bcolors.FAIL}Playback endpoint failed ({playback_response.status_code}){bcolors.ENDC}")
        return None, None

    # Debug only # print(f"Playback status: {playback_response.status_code}")
    # Debug only # print("Response text (first 300 chars):", playback_response.text[:300])

    # The signed DAI token lives in this response header:
    dai_auth = playback_response.headers.get("x-dai-auth")
    if not dai_auth:
        print(f"{bcolors.FAIL}Missing x-dai-auth header on playback response{bcolors.ENDC}")
        return None, None

    playback_data = playback_response.json()

    # If source is direct, short-circuit
    if playback_data.get("source") and playback_data["source"] != "https://":
        return playback_data["source"], video_data

    dai = playback_data.get("dai") or {}
    content_source_id = dai.get("contentSourceId")
    brightcove_video_id = dai.get("videoId")

    if content_source_id and brightcove_video_id:
        # 3) Resolve Google DAI with the x-dai-auth token
        manifest = get_stream_manifest(content_source_id, brightcove_video_id, dai_auth, episode_url)
        if manifest:
            return manifest, video_data
        else:
            print(f"{bcolors.FAIL}Failed to resolve stream manifest from DAI details{bcolors.ENDC}")
            return None, None

    print(f"{bcolors.FAIL}Missing videoId or contentSourceId in playback data{bcolors.ENDC}")
    return None, None


# Function to retrieve stream manifest URL 
def get_stream_manifest(content_source_id, video_id, dai_auth_token, episode_url):
    """
    Google DAI streams endpoint.
    Returns an HLS URL (string) or None.
    """
    base = f"https://pubads.g.doubleclick.net/ondemand/hls/content/{content_source_id}/vid/{video_id}/streams"

    if not dai_auth_token:
        print(f"{bcolors.FAIL}Missing auth-token (x-dai-auth) for DAI request{bcolors.ENDC}")
        return None

    ua = headers.get("User-Agent", "Mozilla/5.0")
    form = {
        "cmsid": content_source_id,
        "vid": video_id,
        "auth-token": dai_auth_token,                         # REQUIRED
        "url": episode_url,                                   
        "ua": ua,                                             
        "correlator": str(random.randint(10**12, 10**16)),    
    }
    stream_headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": episode_url,
    }

    response = requests.post(base, headers=stream_headers, data=form, timeout=20, allow_redirects=True)
    # Debug only # print(f"DAI streams status: {response.status_code} | content-type: {response.headers.get('content-type','')}")

    if response.status_code == 401:
        print(f"{bcolors.FAIL}DAI 401 Unauthorized — refresh x-dai-auth by re-calling playback{bcolors.ENDC}")
        return None

    ctype = (response.headers.get("content-type") or "").lower()
    text = response.text

    # Sometimes DAI returns an m3u8 directly
    if "application/vnd.apple.mpegurl" in ctype or text.startswith("#EXTM3U"):
        return response.url 

        # Or JSON that includes stream URLs
    if "application/json" in ctype:
        try:
            data = response.json()
        except Exception:
            print(text[:400])
            return None

        # Prefer direct manifest fields the DAI API returns
        direct = (
            data.get("stream_manifest")
            or data.get("manifest")
            or data.get("url")
        )
        if isinstance(direct, str) and direct.startswith("http"):
            return direct

        # Older/alternate shape: {"streams":[{"format":"HLS","url":"..."}]}
        streams = data.get("streams") or []
        for s in streams:
            if s.get("format", "").upper() == "HLS" and "url" in s:
                return s["url"]

        print(f"{bcolors.WARNING}No HLS URL in DAI JSON{bcolors.ENDC}")
        print(str(data)[:400])
        return None


# Function to download the master M3U8 manifest and select 960x540 variant
def download_and_select_variant(manifest_url):
    response = requests.get(manifest_url)
    if response.status_code == 200:
        manifest_content = response.text
        # Find 960x540 variant
        pattern = re.compile(r'RESOLUTION=960x540[^\n]+\n(https?://[^\s]+)', re.MULTILINE)
        match = pattern.search(manifest_content)
        if match:
            return match.group(1)
        else:
            print(f"{bcolors.FAIL}960x540 variant not found in manifest{bcolors.ENDC}")
            return None
    else:
        print(f"{bcolors.FAIL}Failed to download manifest: {response.status_code}{bcolors.ENDC}")
        return None

# Function to modify the variant M3U8 content and save with proper filename
def modify_and_save_m3u8(variant_url, downloads_path):
    response = requests.get(variant_url)
    if response.status_code == 200:
        m3u8_content = response.text

        # Replace both styles of 1500000 references with 5000000
        modified_content = m3u8_content.replace("TEN-1500000", "TEN-5000000")
        modified_content = re.sub(r"(?<=-)(1500000)(?=-\d+\.ts)", "5000000", modified_content)

        # Extract the last part of the URL for naming
        filename = variant_url.split('/')[-1]  # e.g., b26a8d0b034d10102de54935d6e484bb.m3u8
        local_m3u8_file = os.path.join(downloads_path, filename)

        # Save the modified content to the file
        with open(local_m3u8_file, 'w') as file:
            file.write(modified_content)

        return local_m3u8_file, variant_url  # Return both local path and the original variant URL
    else:
        print(f"{bcolors.FAIL}Failed to fetch the variant m3u8 file. Status: {response.status_code}{bcolors.ENDC}")
        return None, None


# Function to format the filename based on video details
def format_file_name(video_data):
    show_name = video_data['tvShow'].replace(' ', '.')
    clip_title = video_data.get('clipTitle', '').replace(' ', '.')
    genre = video_data.get('genre', '').lower()
    season = int(video_data['season'])

    if genre == 'movies':
        formatted_file_name = f"{show_name}.1080p.10Play.WEB-DL.AAC2.0.H.264"
    elif genre == 'sport':
        formatted_file_name = f"{clip_title}.S{season}.1080p.10Play.WEB-DL.AAC2.0.H.264"
    else:
        episode = int(video_data['episode'])
        season_episode_tag = f"S{season:02d}E{episode:02d}"
        formatted_file_name = f"{show_name}.{season_episode_tag}.1080p.10Play.WEB-DL.AAC2.0.H.264"
    
    return formatted_file_name

# Function to format and display download command
def display_download_command(m3u8_file_path, formatted_file_name, downloads_path, master_m3u8_url):
    use_source = m3u8_file_path   # default to local file
    final_save_name = formatted_file_name

    print(f"{bcolors.LIGHTBLUE}FHD M3U8 File: {bcolors.ENDC}{m3u8_file_path}")

    # Pre-flight probe: if it looks bad, pre-switch to master best
    preflight_failed = not probe_segment_ok(m3u8_file_path)
    if preflight_failed:
        print(f"{bcolors.WARNING}First/middle/end segment probe failed — falling back to best available from master.{bcolors.ENDC}")
        best_url, best_h = pick_best_variant(master_m3u8_url)
        if best_url:
            use_source = best_url
            if best_h:
                final_save_name = replace_resolution_tag(formatted_file_name, best_h)
            print(f"{bcolors.OKGREEN}Fallback variant:{bcolors.ENDC} {best_h or 'unknown'}p")
            print(f"{bcolors.OKBLUE}Fallback M3U8 URL:{bcolors.ENDC} {best_url}")
        else:
            print(f"{bcolors.FAIL}Could not pick a fallback variant from master.{bcolors.ENDC}")

    # Build command
    download_command = (
        f'N_m3u8DL-RE "{use_source}" '
        f'--ad-keyword redirector.googlevideo.com '
        f'--select-video best --select-audio best --select-subtitle all '
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{final_save_name}"'
    )
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
    print(download_command)

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        # First attempt
        result = subprocess.run(download_command, shell=True)
        if result.returncode != 0 and not preflight_failed:
            # Runtime fallback: if we *thought* the playlist was fine but the tool failed (404, etc.)
            print(f"{bcolors.WARNING}Downloader failed; attempting fallback from master...{bcolors.ENDC}")
            best_url, best_h = pick_best_variant(master_m3u8_url)
            if best_url:
                fallback_save = replace_resolution_tag(formatted_file_name, best_h or 720)
                fb_cmd = (
                    f'N_m3u8DL-RE "{best_url}" '
                    f'--ad-keyword redirector.googlevideo.com '
                    f'--select-video best --select-audio best --select-subtitle all '
                    f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{fallback_save}"'
                )
                print(f"{bcolors.YELLOW}FALLBACK DOWNLOAD COMMAND:{bcolors.ENDC}")
                print(fb_cmd)
                subprocess.run(fb_cmd, shell=True)
            else:
                print(f"{bcolors.FAIL}Fallback failed: could not parse master playlist.{bcolors.ENDC}")

    # Always delete local .m3u8
    if os.path.exists(m3u8_file_path):
        try:
            os.remove(m3u8_file_path)
            print(f"{bcolors.OKGREEN}Deleted temporary m3u8 file:{bcolors.ENDC} {m3u8_file_path}")
        except Exception as e:
            print(f"{bcolors.WARNING}Could not delete m3u8 file: {e}{bcolors.ENDC}")


# Function to extract video ID from URL
def extract_video_id(url):
    match = re.search(r'/([^/]+)/?$', url)
    return match.group(1) if match else None

# Main logic
def main(video_url, downloads_path, credentials):
    username, password = credentials.split(':')
    video_id = extract_video_id(video_url)
    
    if not video_id:
        print(f"{bcolors.FAIL}Invalid URL. Please enter a valid 10Play video URL.{bcolors.ENDC}")
        return

    token = get_bearer_token(username, password)
    if token:
        print(f"{bcolors.OKGREEN}Login successful, token obtained{bcolors.ENDC}")
        manifest_url, video_data = extract_video_details(video_id, token, video_url)
        if manifest_url and video_data:
            variant_url = download_and_select_variant(manifest_url)
            if variant_url:
                local_m3u8_file, original_variant_url = modify_and_save_m3u8(variant_url, downloads_path)
                if local_m3u8_file:
                    # Print the original 960x540 URL and the local path
                    print(f"{bcolors.LIGHTBLUE}MASTER M3U8 URL: {bcolors.ENDC}{manifest_url}")
                    print(f"{bcolors.LIGHTBLUE}SD M3U8 URL: {bcolors.ENDC}{original_variant_url}")
                    
                    formatted_file_name = format_file_name(video_data)
                    display_download_command(local_m3u8_file, formatted_file_name, downloads_path, manifest_url)
                else:
                    print(f"{bcolors.FAIL}Failed to modify and save the variant M3U8{bcolors.ENDC}")
            else:
                print(f"{bcolors.FAIL}Failed to find 960x540 variant{bcolors.ENDC}")
        else:
            print(f"{bcolors.FAIL}Failed to extract manifest URL{bcolors.ENDC}")
    else:
        print(f"{bcolors.FAIL}Login failed{bcolors.ENDC}")