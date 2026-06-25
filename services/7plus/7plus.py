import requests
import json
import re
import subprocess
from http.cookiejar import MozillaCookieJar
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from lxml import etree
import base64
import binascii
import os
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from services.proxy import append_downloader_proxy, mask_proxy_command

#   Ozivine: 7Plus Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the MPD, Licence, PSSH and Decryption keys.
#   eg: https://7plus.com.au/the-front-bar?episode-id=FBAR24-021
#   Authentication: Cookies
#   Geo-Locking: requires an Australian IP address
#   Quality: up to 720p
#   Key Features:
#   1. Extract Video ID: Parses the 7Plus URL to extract the series name, season, and episode number, and then fetches the Brightcove video ID from the 9Now API.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for both encrypted and non-encrypted video files.

# === Debug print control ======================================================
# Set DEBUG_ALL=True to re-enable ALL print() calls across this module.
# Leave it False to silence everything except explicit _PRINT(...) calls below.
import builtins
DEBUG_ALL = False

# Keep a handle to the real print
_PRINT = builtins.print

# Shadow print with a no-op when DEBUG_ALL is False
if not DEBUG_ALL:
    def print(*args, **kwargs):  # noqa: A001 - intentionally shadow built-in in this module
        return
# =============================================================================

# Formatting for output
class bcolors:
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    LIGHTBLUE = '\033[94m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    GREEN = '\033[92m'
    ORANGE = '\033[38;5;208m'

# URLs and Headers
BASE_URL = "https://7plus.com.au"
PLATFORM_VERSION = "1.0.106518"

def _default_headers(referer_path="/", auth_token=None, conn_close=False):
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}{referer_path}",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "x-client-capabilities": "drm-auth",
        "Connection": "keep-alive",
    }
    if conn_close:
        h["Connection"] = "close"
    if auth_token:
        h["Authorization"] = f"Bearer {auth_token}"
    return h

def _session_with_retries(total=3, backoff=0.5, pool_maxsize=20):
    s = requests.Session()
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        status=total,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD", "OPTIONS"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def get_authenticated_session(video_url, cookies_path):
    """
    Loads cookies, performs Gigya -> id_token -> 7plus auth flow,
    and returns (session, auth_token).
    """

    # Reuse your retry-capable session factory + browser-y headers
    session = _session_with_retries()

    # Load your exported cookies
    cookies = MozillaCookieJar(cookies_path)
    cookies.load(ignore_discard=True, ignore_expires=True)
    session.cookies = cookies

    # Touch the page once to refresh cookie flags
    headers = _default_headers("/", auth_token=None)
    try:
        session.get(video_url, headers=headers, timeout=(8, 25))
    except Exception:
        pass

    # Find Gigya APIKey + login_token from glt_<APIKEY> cookie
    api_key, login_token = None, None
    for c in cookies:
        if c.name.startswith('glt_'):
            api_key = c.name[4:]
            login_token = c.value
            break
    if not api_key or not login_token:
        raise RuntimeError("Failed to find Gigya cookies (glt_*). Export cookies while logged in.")

    # Gigya -> id_token
    login_url = "https://login.7plus.com.au/accounts.getJWT"
    login_params = {
        "APIKey": api_key,
        "sdk": "js_latest",
        "login_token": login_token,
        "authMode": "cookie",
        "pageURL": "https://7plus.com.au/",
        "sdkBuild": "12471",
        "format": "json",
    }
    r = session.get(login_url, params=login_params, headers=headers, timeout=(8, 25))
    r.raise_for_status()
    id_token = r.json().get("id_token")
    if not id_token:
        raise RuntimeError("No id_token returned by Gigya.")

    # id_token -> 7plus auth token (Bearer)
    auth_url = "https://7plus.com.au/auth/token"
    r = session.post(
        auth_url,
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps({"idToken": id_token, "platformId": "web", "regSource": "7plus"}),
        timeout=(8, 25),
    )
    r.raise_for_status()
    auth_token = r.json().get("token")
    if not auth_token:
        raise RuntimeError("No auth token returned by /auth/token.")

    return session, auth_token

def get_pssh(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to load MPD, status code: {response.status_code}{bcolors.ENDC}")
        return None
    mpd_xml = etree.fromstring(response.content)
    pssh_elements = mpd_xml.xpath('.//cenc:pssh', namespaces={'cenc': 'urn:mpeg:cenc:2013'})
    if not pssh_elements:
        print(f"{bcolors.FAIL}Failed to find PSSH in MPD{bcolors.ENDC}")
        return None
    return pssh_elements[0].text

# Function to extract video details
def extract_info(video_url, cookies_path, session=None, auth_token=None):
    # Ensure we have an authenticated session/token
    if session is None or auth_token is None:
        session, auth_token = get_authenticated_session(video_url, cookies_path)

    headers = _default_headers("/", auth_token)

    # Playback endpoint + params 
    media_url = 'https://videoservice.swm.digital/playback'
    path, episode_id = re.search(
        r'https?://(?:www\.)?7plus\.com\.au/(?P<path>[^?]+\?.*?\bepisode-id=(?P<id>[^&#]+))',
        video_url
    ).groups()
    media_params = {
        'appId': '7plus',
        'deviceType': 'web',
        'platformType': 'web',
        'accountId': 5303576322001,
        'referenceId': 'ref:' + episode_id,
        'deliveryId': 'csai',
        'videoType': 'vod',
    }

    # First attempt (keep-alive)
    try:
        r = session.get(media_url, params=media_params, headers=headers, timeout=(8, 25))
        r.raise_for_status()
    except Exception:
        # Fallback: force Connection: close (helps when server drops keep-alive)
        r = session.get(
            media_url, params=media_params,
            headers=_default_headers("/", auth_token, conn_close=True),
            timeout=(8, 25)
        )
        r.raise_for_status()

    media_resp = r.json()

    media = media_resp.get('media', {})
    sources = media.get('sources', [])
    mpd_url = None
    license_url = None
    m3u8_url = None

    for source in sources:
        if source.get('type') == 'application/dash+xml' and "playready" not in source.get('src'):
            mpd_url = source.get('src')
            key_systems = source.get('key_systems', {})
            widevine = key_systems.get('com.widevine.alpha', {})
            license_url = widevine.get('license_url')
            break
        elif source.get('type') == 'application/x-mpegURL' and 'master.m3u8' in source.get('src') and "fairplay" not in source.get('src'):
            m3u8_url = source.get('src')
            break

    if mpd_url and license_url:
        pssh = get_pssh(mpd_url)
        if not pssh:
            print("Failed to extract PSSH from MPD")
            return None
        return {
            'formats': [{'url': mpd_url, 'ext': 'mpd', 'pssh': pssh}],
            'license_url': license_url,
        }
    elif m3u8_url:
        return {
            'formats': [{'url': m3u8_url, 'ext': 'm3u8'}]
        }
    else:
        print("No suitable source found for video")
        return None

# Function to get decryption keys
def get_keys(pssh, lic_url, wvd_device_path):
    try:
        pssh = PSSH(pssh)
    except binascii.Error as e:
        print(f"Could not decode PSSH data as Base64: {e}")
        return []

    device = Device.load(wvd_device_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    challenge = cdm.get_license_challenge(session_id, pssh)
    
    headers = {
        'Content-Type': 'application/dash+xml',
        'Origin': 'https://7plus.com.au',
        'Referer': 'https://7plus.com.au',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    }

    licence = requests.post(lic_url, headers=headers, data=challenge)
    
    try:
        licence.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"HTTPError: {e}")
        print(f"Response Headers: {licence.headers}")
        print(f"Response Text: {licence.text}")
        raise

    cdm.parse_license(session_id, licence.content)
    keys = [f"{str(key.kid).replace('-', '')}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
    cdm.close(session_id)

    return keys

# Function to get the maximum video resolution from the MPD manifest
def get_resolution_from_mpd(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to load MPD, status code: {response.status_code}{bcolors.ENDC}")
        return None

    mpd_xml = etree.fromstring(response.content)
    representations = mpd_xml.xpath('//default:Representation', namespaces={'default': 'urn:mpeg:dash:schema:mpd:2011'})
    if not representations:
        print(f"{bcolors.FAIL}Failed to find Representations in MPD{bcolors.ENDC}")
        return None

    best_representation = representations[-1]
    height = best_representation.attrib.get('height')
    return f"{height}p" if height else None

# Function to get the maximum video resolution from the M3U8 manifest
def get_resolution_from_m3u8(m3u8_url):
    response = requests.get(m3u8_url)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to load M3U8, status code: {response.status_code}{bcolors.ENDC}")
        return None
    lines = response.text.split('\n')
    resolutions = [re.search(r'RESOLUTION=(\d+x\d+)', line) for line in lines if line.startswith('#EXT-X-STREAM-INF')]
    resolutions = [res.group(1) for res in resolutions if res]
    if not resolutions:
        print(f"{bcolors.FAIL}Failed to find RESOLUTION in M3U8{bcolors.ENDC}")
        return None
    best_resolution = max(resolutions, key=lambda r: int(r.split('x')[1]))
    return f"{best_resolution.split('x')[1]}p"

def get_mpd_streams(mpd_url):
    streams = []
    response = requests.get(mpd_url)
    if response.status_code != 200:
        _PRINT(f"{bcolors.FAIL}Failed to load MPD, status code: {response.status_code}{bcolors.ENDC}")
        return streams

    mpd_xml = etree.fromstring(response.content)
    adaptation_sets = mpd_xml.xpath('//default:AdaptationSet', namespaces={'default': 'urn:mpeg:dash:schema:mpd:2011'})
    for adaptation in adaptation_sets:
        mime_type = adaptation.attrib.get("mimeType", "")
        content_type = adaptation.attrib.get("contentType", "")
        language = adaptation.attrib.get("lang", "")
        if "video" in mime_type or content_type == "video":
            stream_type = "video"
        elif "audio" in mime_type or content_type == "audio":
            stream_type = "audio"
        elif "text" in mime_type or "ttml" in mime_type or content_type == "text":
            stream_type = "subtitle"
        else:
            stream_type = "stream"

        representations = adaptation.xpath('./default:Representation', namespaces={'default': 'urn:mpeg:dash:schema:mpd:2011'})
        for rep in representations:
            width = rep.attrib.get("width")
            height = rep.attrib.get("height")
            bandwidth = rep.attrib.get("bandwidth")
            streams.append({
                "type": stream_type,
                "resolution": f"{width}x{height}" if width and height else "",
                "bandwidth": int(bandwidth) if str(bandwidth or "").isdigit() else 0,
                "codecs": rep.attrib.get("codecs") or adaptation.attrib.get("codecs", ""),
                "language": language,
            })
    return sorted(streams, key=lambda item: (item["type"] != "video", -item["bandwidth"]))

def get_m3u8_streams(m3u8_url):
    streams = []
    response = requests.get(m3u8_url)
    if response.status_code != 200:
        _PRINT(f"{bcolors.FAIL}Failed to load M3U8, status code: {response.status_code}{bcolors.ENDC}")
        return streams

    pending = None
    for line in response.text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            resolution = re.search(r"RESOLUTION=(\d+x\d+)", line)
            bandwidth = re.search(r"BANDWIDTH=(\d+)", line)
            codecs = re.search(r'CODECS="([^"]+)"', line)
            pending = {
                "type": "video",
                "resolution": resolution.group(1) if resolution else "",
                "bandwidth": int(bandwidth.group(1)) if bandwidth else 0,
                "codecs": codecs.group(1) if codecs else "",
                "language": "",
            }
        elif pending and line and not line.startswith("#"):
            streams.append(pending)
            pending = None
    return sorted(streams, key=lambda item: item["bandwidth"], reverse=True)

def print_streams(streams):
    if not streams:
        _PRINT(f"\n{bcolors.WARNING}No stream variants found.{bcolors.ENDC}")
        return

    _PRINT(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    header = f"  {'#':>2}  {'Type':<4} {'Resolution':<10} {'Bitrate':<16} {'Codec':<18} {'Lang':<5}"
    divider = f"  {'-' * 2}  {'-' * 4} {'-' * 10} {'-' * 16} {'-' * 18} {'-' * 5}"
    _PRINT(header)
    _PRINT(divider)
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
        _PRINT(f"  {idx:>2}  {label:<4} {resolution:<10} {bitrate:<16} {codecs:<18} {language:<5}")

def build_7plus_command(url, downloads_path, formatted_file_name, keys=None, interactive=False):
    selectors = "" if interactive else "--select-video best --select-audio best --select-subtitle all "
    download_command = (
        f'N_m3u8DL-RE "{url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}"'
    )
    download_command = append_downloader_proxy(download_command)
    if keys:
        download_command += " --key " + " --key ".join(keys)
    return download_command

def print_7plus_info(source_url, source_type, formatted_file_name, lic_url=None, pssh=None, keys=None):
    if source_type == "mpd":
        _PRINT(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{source_url}")
        if lic_url:
            _PRINT(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
        if pssh:
            _PRINT(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
        for key in keys or []:
            _PRINT(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
        print_streams(get_mpd_streams(source_url))
    else:
        _PRINT(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{source_url}")
        print_streams(get_m3u8_streams(source_url))
    _PRINT(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")

def season_episode_from_episode_id(episode_id):
    match = re.search(r'^[A-Z]+(?P<season>\d{2,4})-(?P<episode>\d+)$', episode_id or "", re.IGNORECASE)
    if not match:
        return ""

    season = match.group("season")
    episode = match.group("episode")

    if len(season) == 2 and int(season) >= 20:
        season = f"20{season}"
    elif len(season) == 2:
        season = season.zfill(2)

    return f"S{season}E{int(episode):02d}"

# Function to format and display download command
def get_download_command(info, show_title, season_episode_tag, downloads_path, wvd_device_path, mode="auto"):
    formats = info.get('formats')
    if not formats:
        print(f"{bcolors.FAIL}No formats found in info{bcolors.ENDC}")
        return

    format_info = formats[0]
    url = format_info.get('url')
    ext = format_info.get('ext')
    resolution = None

    if ext == 'mpd':
        pssh = format_info.get('pssh')
        lic_url = info.get('license_url')
        if url and lic_url and pssh:
            keys = get_keys(pssh, lic_url, wvd_device_path)
            resolution = get_resolution_from_mpd(url)
            if not resolution:
                resolution = "best"

            formatted_file_name = f"{show_title}"
            if season_episode_tag:
                formatted_file_name += f".{season_episode_tag}"
            formatted_file_name += f".{resolution}.7PLUS.WEB-DL.AAC2.0.H.264"
            if mode == "info":
                print_7plus_info(url, "mpd", formatted_file_name, lic_url, pssh, keys)
                return
            download_command = build_7plus_command(url, downloads_path, formatted_file_name, keys, interactive=(mode == "interactive"))
        
        # -- VISIBLE OUTPUT (encrypted MPD) -----------------------------------
        _PRINT(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{url}")
        _PRINT(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
        _PRINT(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
        for key in keys:
            _PRINT(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
        _PRINT(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
        _PRINT(mask_proxy_command(download_command))
        # ---------------------------------------------------------------------
        
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == 'y':
            subprocess.run(download_command, shell=True)
    elif ext == 'm3u8':
        if url:
            resolution = get_resolution_from_m3u8(url)
            if not resolution:
                resolution = "best"
            formatted_file_name = f"{show_title}"
            if season_episode_tag:
                formatted_file_name += f".{season_episode_tag}"
            formatted_file_name += f".{resolution}.7PLUS.WEB-DL.AAC2.0.H.264"
            if mode == "info":
                print_7plus_info(url, "m3u8", formatted_file_name)
                return
            download_command = build_7plus_command(url, downloads_path, formatted_file_name, interactive=(mode == "interactive"))
            
            # -- VISIBLE OUTPUT (unencrypted m3u8) -----------------------------
            _PRINT(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{url}")
            _PRINT(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
            _PRINT(mask_proxy_command(download_command))
            # ------------------------------------------------------------------
            
            user_input = input("Do you wish to download? Y or N: ").strip().lower()
            if user_input == 'y':
                subprocess.run(download_command, shell=True)
        else:
            print(f"{bcolors.FAIL}Failed to retrieve necessary information for download{bcolors.ENDC}")       

# Main logic
def main(video_url, downloads_path, wvd_device_path, cookies_path, mode="auto"): 

    # 1) Probe playback (unchanged) — add progress around it
    print(f"{bcolors.OKBLUE}[STEP] Calling extract_info (videoservice)…{bcolors.ENDC}")
    info = extract_info(video_url, cookies_path)
    if not info:
        print(f"{bcolors.FAIL}[ERR] extract_info returned no info — aborting{bcolors.ENDC}")
        return
    print(f"{bcolors.OKGREEN}✅ extract_info succeeded{bcolors.ENDC}")

    # 2) Parse show_name + episode_id from the URL (unchanged logic)
    try:
        path, episode_id = re.search(
            r'https?://(?:www\.)?7plus\.com\.au/(?P<path>[^?]+\?.*?\bepisode-id=(?P<id>[^&#]+))',
            video_url
        ).groups()
    except Exception as e:
        print(f"{bcolors.FAIL}[ERR] Could not parse show_name/episode_id: {e}{bcolors.ENDC}")
        return
    show_name = path.split('?')[0]
    print(f"{bcolors.OKBLUE}[PARSE] show_name={show_name}, episode_id={episode_id}{bcolors.ENDC}")

    # 3) Build a session + load cookies (minimal, same libraries you already use)
    print(f"{bcolors.OKBLUE}[STEP] Preparing session & loading cookies…{bcolors.ENDC}")
    session = requests.Session()
    cookies = MozillaCookieJar(cookies_path)
    try:
        cookies.load(ignore_discard=True, ignore_expires=True)
        print(f"{bcolors.OKGREEN}✅ Loaded cookies: {len(list(cookies))} items from {cookies_path}{bcolors.ENDC}")
    except Exception as e:
        print(f"{bcolors.FAIL}[ERR] Failed to load cookies: {e}{bcolors.ENDC}")
        return
    session.cookies = cookies

    # Browser-y headers 
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/140.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Origin": "https://7plus.com.au",
        "Referer": f"https://7plus.com.au/{show_name}",
        "x-client-capabilities": "drm-auth",
    }

    # 4) Touch page (keeps cookie jar fresh)
    try:
        print(f"{bcolors.OKBLUE}[HTTP] GET {video_url}{bcolors.ENDC}")
        tr = session.get(video_url, headers=headers, timeout=(8, 25))
        print(f"{bcolors.OKBLUE}[HTTP] -> {tr.status_code} ({len(tr.content)} bytes){bcolors.ENDC}")
    except Exception as e:
        print(f"{bcolors.WARNING}[WARN] Touch failed: {e}{bcolors.ENDC}")

    # 5) Find Gigya API key / login_token from cookies
    api_key, login_token = None, None
    for c in cookies:
        if c.name.startswith('glt_'):
            api_key = c.name[4:]
            login_token = c.value
            break
    if not api_key or not login_token:
        print(f"{bcolors.FAIL}[ERR] Failed to find Gigya API key/login_token (glt_*) in cookies{bcolors.ENDC}")
        return
    print(f"{bcolors.OKGREEN}✅ Found Gigya API key (masked): {api_key[:6]}…{bcolors.ENDC}")

    # 6) Gigya -> id_token
    login_url = 'https://login.7plus.com.au/accounts.getJWT'
    login_params = {
        'APIKey': api_key,
        'sdk': 'js_latest',
        'login_token': login_token,
        'authMode': 'cookie',
        'pageURL': 'https://7plus.com.au/',
        'sdkBuild': '12471',
        'format': 'json',
    }
    try:
        print(f"{bcolors.OKBLUE}[HTTP] GET {login_url}{bcolors.ENDC}")
        r = session.get(login_url, params=login_params, headers=headers, timeout=(8, 25))
        print(f"{bcolors.OKBLUE}[HTTP] -> {r.status_code} ({len(r.content)} bytes){bcolors.ENDC}")
        r.raise_for_status()
        id_token = r.json().get('id_token')
        if not id_token:
            print(f"{bcolors.FAIL}[ERR] No id_token in Gigya response{bcolors.ENDC}")
            return
        print(f"{bcolors.OKGREEN}✅ Received id_token{bcolors.ENDC}")
    except Exception as e:
        print(f"{bcolors.FAIL}[ERR] Gigya JWT call failed: {e}{bcolors.ENDC}")
        return

    # 7) id_token -> 7plus auth token
    auth_url = 'https://7plus.com.au/auth/token'
    try:
        print(f"{bcolors.OKBLUE}[HTTP] POST {auth_url}{bcolors.ENDC}")
        r = session.post(
            auth_url,
            headers={**headers, 'Content-Type': 'application/json'},
            data=json.dumps({'idToken': id_token, 'platformId': 'web', 'regSource': '7plus'}),
            timeout=(8, 25)
        )
        print(f"{bcolors.OKBLUE}[HTTP] -> {r.status_code} ({len(r.content)} bytes){bcolors.ENDC}")
        r.raise_for_status()
        auth_token = r.json().get('token')
        if not auth_token:
            print(f"{bcolors.FAIL}[ERR] No auth token in /auth/token response{bcolors.ENDC}")
            return
        print(f"{bcolors.OKGREEN}✅ Received auth token (masked): {auth_token[:12]}…{bcolors.ENDC}")
    except Exception as e:
        print(f"{bcolors.FAIL}[ERR] Auth token exchange failed: {e}{bcolors.ENDC}")
        return

    # 8) Show API (episode input) WITH Authorization + latest platform-version
    show_api_url = (
        f"https://component-cdn.swm.digital/content/{show_name}"
        f"?episode-id={episode_id}"
        f"&platform-id=web&market-id=29&platform-version={PLATFORM_VERSION}&api-version=4.9&signedup=true"
    )
    show_headers = {**headers, "Authorization": f"Bearer {auth_token}"}
    try:
        print(f"{bcolors.OKBLUE}[HTTP] GET {show_api_url}{bcolors.ENDC}")
        sr = session.get(show_api_url, headers=show_headers, timeout=(8, 25))
        print(f"{bcolors.OKBLUE}[HTTP] -> {sr.status_code} ({len(sr.content)} bytes){bcolors.ENDC}")
        sr.raise_for_status()
        show_response = sr.json()
    except Exception as e:
        print(f"{bcolors.FAIL}[ERR] Show API call failed: {e}{bcolors.ENDC}")
        return

    # 9) Filename bits (unchanged)
    try:
        show_title = show_response['title'].replace(" ", ".")
        alt_tag = show_response['pageMetaData']['objectGraphImage']['altTag']
        print(f"{bcolors.OKGREEN}✅ Parsed show metadata — title={show_title}{bcolors.ENDC}")
    except Exception as e:
        print(f"{bcolors.FAIL}[ERR] Parsing show metadata failed: {e}{bcolors.ENDC}")
        return

    season_episode_tag = ""
    m = re.search(r'Season (\d+) Episode (\d+)', alt_tag)
    if m:
        season, episode = m.groups()
        season_episode_tag = f"S{season.zfill(2)}E{episode.zfill(2)}"
        print(f"{bcolors.OKBLUE}[PARSE] season_episode_tag={season_episode_tag}{bcolors.ENDC}")
    else:
        season_episode_tag = season_episode_from_episode_id(episode_id)
        if season_episode_tag:
            print(f"{bcolors.OKBLUE}[PARSE] season_episode_tag={season_episode_tag} from episode-id{bcolors.ENDC}")
        else:
            print(f"{bcolors.WARNING}[WARN] Could not find season/episode metadata; continuing without tag{bcolors.ENDC}")

    # 10) Kick off download command (unchanged)
    print(f"{bcolors.OKBLUE}[STEP] Building download command…{bcolors.ENDC}")
    get_download_command(info, show_title, season_episode_tag, downloads_path, wvd_device_path, mode)
    
