import requests
import datetime as dt
import base64
import json
import subprocess
import re
import os
import random
import time
import hmac
import hashlib
from urllib.parse import urljoin, urlparse
import yaml
from rich.console import Console
from rich.rule import Rule
from rich.text import Text
from colors import bcolors
import icons
from filename_utils import safe_windows_filename
from services.proxy import append_downloader_proxy, mask_proxy_command


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

def parse_m3u8_attributes(value: str):
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value):
        key = match.group(1)
        raw_value = match.group(2).strip()
        attrs[key] = raw_value.strip('"')
    return attrs

def get_master_streams(master_url: str):
    try:
        text = fetch_text(master_url)
    except Exception:
        return []

    streams = []
    last_attrs = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-STREAM-INF:"):
            last_attrs = parse_m3u8_attributes(s.split(":", 1)[1])
            continue

        if not last_attrs or not s or s.startswith("#"):
            continue

        bandwidth = last_attrs.get("BANDWIDTH") or last_attrs.get("AVERAGE-BANDWIDTH")
        resolution = last_attrs.get("RESOLUTION") or "-"
        codecs = last_attrs.get("CODECS") or "unknown codecs"
        bitrate = f"{int(bandwidth) // 1000} Kbps" if bandwidth and bandwidth.isdigit() else "unknown bitrate"
        streams.append({
            "type": "Vid",
            "resolution": resolution,
            "bitrate": bitrate,
            "codec": codecs,
            "lang": "-",
        })
        last_attrs = None

    return sorted(streams, key=stream_sort_key)

def stream_sort_key(stream):
    type_order = {"Vid": 0, "Aud": 1, "Sub": 2}
    height = 0
    bitrate = 0

    resolution_match = re.search(r"x(\d+)", stream["resolution"])
    if resolution_match:
        height = int(resolution_match.group(1))

    bitrate_match = re.search(r"(\d+)", stream["bitrate"])
    if bitrate_match:
        bitrate = int(bitrate_match.group(1))

    return (type_order.get(stream["type"], 9), -height, -bitrate, stream["codec"], stream["lang"])

def print_streams(streams):
    if not streams:
        print(f"\n{bcolors.YELLOW}Available streams: {bcolors.ENDC}No streams found")
        return

    print(f"\n{bcolors.YELLOW}Available streams:{bcolors.ENDC}")
    print(f"{'#':>3}  {'Type':<4} {'Resolution':<11} {'Bitrate':<16} {'Codec':<32} {'Lang':<6}")
    print(f"{'--':>3}  {'----':<4} {'----------':<11} {'----------------':<16} {'--------------------------------':<32} {'------':<6}")
    for index, stream in enumerate(streams, start=1):
        print(
            f"{index:>3}  "
            f"{stream['type']:<4} "
            f"{stream['resolution']:<11} "
            f"{stream['bitrate']:<16} "
            f"{stream['codec']:<32} "
            f"{stream['lang']:<6}"
        )

def collect_external_subtitles(master_url):
    try:
        text = fetch_text(master_url)
    except Exception:
        return []

    subtitles = []
    seen_urls = set()
    for line in text.splitlines():
        if "#EXT-X-MEDIA:" not in line:
            continue

        attrs = parse_m3u8_attributes(line.split(":", 1)[1])
        media_type = (attrs.get("TYPE") or "").upper()
        uri = attrs.get("URI")
        if media_type not in {"SUBTITLES", "CLOSED-CAPTIONS"} or not uri:
            continue

        subtitle_url = urljoin(master_url, uri)
        if subtitle_url in seen_urls:
            continue

        seen_urls.add(subtitle_url)
        subtitles.append({
            "url": subtitle_url,
            "language": (attrs.get("LANGUAGE") or "und").lower(),
            "name": attrs.get("NAME") or attrs.get("LANGUAGE") or "Subtitle",
            "kind": media_type.lower(),
            "extension": "srt",
        })

    return subtitles

def print_external_subtitles(subtitles):
    if not subtitles:
        return

    print(f"\n{bcolors.YELLOW}External subtitles:{bcolors.ENDC}")
    print(f"{'#':>3}  {'Lang':<6} {'Kind':<15} {'Format':<7} {'Name':<20}")
    print(f"{'--':>3}  {'------':<6} {'---------------':<15} {'-------':<7} {'--------------------':<20}")
    for index, subtitle in enumerate(subtitles, start=1):
        print(
            f"{index:>3}  "
            f"{subtitle.get('language', '-'):<6} "
            f"{subtitle.get('kind', '-'):<15} "
            f"{subtitle.get('extension', '-'):<7} "
            f"{subtitle.get('name', '-'):<20}"
        )

def extract_vtt_cues(vtt_text):
    text = vtt_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if "-->" in line:
            return "\n".join(lines[index:]).strip()
    return ""

def download_vtt_playlist(subtitle_url):
    try:
        playlist = fetch_text(subtitle_url, timeout=20)
    except Exception:
        return None

    parts = []
    for line in playlist.splitlines():
        segment = line.strip()
        if not segment or segment.startswith("#"):
            continue

        try:
            segment_text = fetch_text(urljoin(subtitle_url, segment), timeout=20)
        except Exception:
            continue

        cues = extract_vtt_cues(segment_text)
        if cues:
            parts.append(cues)

    if not parts:
        return None

    return "WEBVTT\n\n" + "\n\n".join(parts).strip() + "\n"

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

def subtitle_filename(base_name, subtitle, index, used_names):
    base_name = safe_windows_filename(base_name)
    language = re.sub(r"[^A-Za-z0-9]+", "", subtitle.get("language") or "und") or "und"
    extension = subtitle.get("extension") or "vtt"
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
    for index, subtitle in enumerate(subtitles, start=1):
        print(f"{bcolors.OKCYAN}{icons.ICON_WAITING} Processing subtitle:{bcolors.ENDC} {subtitle.get('language', 'und')} {subtitle.get('name', 'Subtitle')}")
        vtt_content = download_vtt_playlist(subtitle["url"])
        content = vtt_to_srt(vtt_content or "")
        if not content:
            print(f"{bcolors.WARNING}Subtitle skipped: no usable cues found{bcolors.ENDC}")
            continue

        filename = subtitle_filename(formatted_file_name, subtitle, index, used_names)
        path = os.path.join(downloads_path, filename)
        with open(path, "w", encoding="utf-8-sig", newline="") as file:
            file.write(content)
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Subtitle saved:{bcolors.ENDC} {path}")

def get_available_streams(master_url: str):
    streams = [
        {
            "type": "Vid",
            "resolution": "1920x1080",
            "bitrate": "5000 Kbps",
            "codec": "H.264",
            "lang": "-",
        }
    ]
    seen = {(stream["type"], stream["resolution"], stream["bitrate"], stream["codec"], stream["lang"]) for stream in streams}

    for stream in get_master_streams(master_url):
        stream_key = (stream["type"], stream["resolution"], stream["bitrate"], stream["codec"], stream["lang"])
        if stream_key not in seen:
            streams.append(stream)
            seen.add(stream_key)

    return sorted(streams, key=stream_sort_key)

def build_action_master_m3u8(fhd_m3u8_file_path, master_m3u8_url, downloads_path):
    try:
        master_text = fetch_text(master_m3u8_url)
    except Exception:
        return None

    fhd_filename = os.path.basename(fhd_m3u8_file_path)
    base_name = os.path.splitext(fhd_filename)[0]
    action_master_path = os.path.join(downloads_path, f"{base_name}_ozivine_master.m3u8")

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"',
        fhd_filename,
    ]

    last_inf = None
    for line in master_text.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-STREAM-INF:"):
            last_inf = s
            continue
        if last_inf and s and not s.startswith("#"):
            lines.append(last_inf)
            lines.append(urljoin(master_m3u8_url, s))
            last_inf = None

    with open(action_master_path, "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")

    return action_master_path

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

def is_movie(video_data):
    return str(video_data.get("genre", "")).lower() == "movies"

### End of Helpers ###

CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"))
TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "temp"))
EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "export"))
TEN_CONFIG_URL = "https://10.com.au/api/v1/config"
TEN_API_KEY_HEX = "b918ff793563080c5821c89ee6c415c363cb36d369db1020369ac4b405a0211d"
console = Console()

# URLs and Headers
login_url = 'https://10play.com.au/api/user/auth'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://10play.com.au',
    'Referer': 'https://10play.com.au/'
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


def ensure_10play_cache(config):
    config.setdefault("credentials", {})
    config.setdefault("10play", {})
    config["10play"].setdefault("cache", {})
    config["10play"]["cache"].setdefault("login", {})
    return config


def parse_10play_credentials(credentials):
    creds = (credentials or "").strip()
    if not creds or ":" not in creds:
        raise ValueError("Missing 10 credentials. Expected username:password")

    username, password = creds.split(":", 1)
    username = username.strip()
    password = password.strip()

    if not username or not password:
        raise ValueError("Invalid 10 credentials. Expected username:password")

    return username, password


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        return None


def jwt_expiry_utc(token):
    try:
        raw_token = token.replace("Bearer ", "", 1).strip()
        parts = raw_token.split(".")
        if len(parts) != 3:
            return None

        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        data = json.loads(decoded)
        exp = data.get("exp")
        if not exp:
            return None
        return dt.datetime.fromtimestamp(exp, tz=dt.timezone.utc)
    except Exception:
        return None


def token_is_valid(token, expiry, buffer_minutes=5):
    if not token or not expiry:
        return False

    expiry_dt = parse_iso_datetime(expiry)
    if not expiry_dt:
        return False

    now = dt.datetime.now(dt.timezone.utc)
    return expiry_dt > now + dt.timedelta(minutes=buffer_minutes)


def login_10play(username, password):
    timestamp = dt.datetime.now().strftime('%Y%m%d000000')
    auth_header = base64.b64encode(timestamp.encode('ascii')).decode('ascii')
    login_payload = {'email': username, 'password': password}
    login_headers = headers.copy()
    login_headers['X-Network-Ten-Auth'] = auth_header

    response = requests.post(login_url, json=login_payload, headers=login_headers)
    if response.status_code == 200:
        data = response.json()
        if 'jwt' in data:
            token = 'Bearer ' + data['jwt']['accessToken']
            expiry_dt = jwt_expiry_utc(token)
            return {
                "token": token,
                "expiry": expiry_dt.isoformat() if expiry_dt else "",
            }
    return None


def get_bearer_token(config, credentials):
    config = ensure_10play_cache(config)
    cache = config["10play"]["cache"]["login"]

    cached_token = cache.get("token", "")
    cached_expiry = cache.get("expiry", "")
    repaired_cache = False

    if cached_token and not cached_expiry:
        expiry_dt = jwt_expiry_utc(cached_token)
        if expiry_dt:
            cached_expiry = expiry_dt.isoformat()
            cache["expiry"] = cached_expiry
            repaired_cache = True

    if token_is_valid(cached_token, cached_expiry):
        if repaired_cache:
            save_config(config)
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Using cached token{bcolors.ENDC}")
        return cached_token

    username, password = parse_10play_credentials(credentials)
    print(f"{bcolors.OKCYAN}{icons.ICON_INFO} Cached token missing/expired, logging in...{bcolors.ENDC}")

    login_data = login_10play(username, password)
    if not login_data:
        return None

    cache["token"] = login_data["token"]
    expiry_dt = jwt_expiry_utc(login_data["token"])
    cache["expiry"] = login_data["expiry"] or (expiry_dt.isoformat() if expiry_dt else "")
    save_config(config)

    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Token cache updated{bcolors.ENDC}")
    return login_data["token"]


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
        print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Failed to fetch video details ({response.status_code}){bcolors.ENDC}")
        return None, None

    video_data = response.json()

    # 2) Playback endpoint 
    playback_url = f"https://10play.com.au/api/v1/videos/playback/{video_id}?platform=tizen"
    playback_response = requests.get(playback_url, headers=auth_headers, timeout=20)
    if playback_response.status_code != 200:
        print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Playback endpoint failed ({playback_response.status_code}){bcolors.ENDC}")
        return None, None

    # Debug only # print(f"Playback status: {playback_response.status_code}")
    # Debug only # print("Response text (first 300 chars):", playback_response.text[:300])

    # The signed DAI token lives in this response header:
    dai_auth = playback_response.headers.get("x-dai-auth")
    if not dai_auth:
        print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Missing x-dai-auth header on playback response{bcolors.ENDC}")
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
            print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Failed to resolve stream manifest from DAI details{bcolors.ENDC}")
            return None, None

    print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Missing videoId or contentSourceId in playback data{bcolors.ENDC}")
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
    show_name = safe_windows_filename(str(video_data.get('tvShow') or "10").replace(' ', '.'))
    clip_title = safe_windows_filename(str(video_data.get('clipTitle') or '').replace(' ', '.'))
    genre = video_data.get('genre', '').lower()
    season = int(video_data['season'])

    if genre == 'movies':
        formatted_file_name = f"{show_name}.1080p.10Play.WEB-DL.AAC2.0.H.264"
    elif genre == 'sport':
        episode = int(video_data.get('episode') or 0)
        if episode:
            season_tag = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
            formatted_file_name = f"{show_name}.{season_tag}E{episode:02d}.1080p.10Play.WEB-DL.AAC2.0.H.264"
        else:
            formatted_file_name = f"{clip_title or show_name}.S{season}.1080p.10Play.WEB-DL.AAC2.0.H.264"
    else:
        episode = int(video_data['episode'])
        season_episode_tag = f"S{season:02d}E{episode:02d}"
        formatted_file_name = f"{show_name}.{season_episode_tag}.1080p.10Play.WEB-DL.AAC2.0.H.264"
    
    return formatted_file_name

# Function to format and display download command
def cleanup_temp_m3u8(*m3u8_file_paths):
    for m3u8_file_path in m3u8_file_paths:
        if not m3u8_file_path or not os.path.exists(m3u8_file_path):
            continue
        try:
            os.remove(m3u8_file_path)
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Deleted temporary m3u8 file:{bcolors.ENDC} {m3u8_file_path}")
        except Exception as e:
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Could not delete m3u8 file: {e}{bcolors.ENDC}")

def expected_output_exists(downloads_path, save_name):
    candidates = [
        os.path.join(downloads_path, f"{save_name}.mkv"),
        os.path.join(downloads_path, f"{save_name}.MUX.mkv"),
    ]
    return any(os.path.exists(path) for path in candidates)

def format_info_date(value):
    if value in (None, "", "Not Available"):
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            parsed = dt.datetime.utcfromtimestamp(int(value))
        else:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return f"{parsed.day} {parsed.strftime('%B %Y')}"
    except Exception:
        return str(value)

def clean_info_value(value):
    if value in (None, "", "Not Available"):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()

def print_info_metadata(video_data):
    if not video_data:
        return

    show_title = clean_info_value(video_data.get("tvShow") or video_data.get("showTitle"))
    episode_title = clean_info_value(video_data.get("title") or video_data.get("clipTitle") or video_data.get("vodTitle"))
    date_aired = format_info_date(video_data.get("published") or video_data.get("datePublished"))
    description = clean_info_value(video_data.get("description") or video_data.get("abstract"))

    rows = [
        ("Show", show_title),
        ("Title", episode_title),
        ("Date Aired", date_aired),
        ("Description", description),
    ]
    rows = [(label, value) for label, value in rows if value]
    if not rows:
        return

    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

def display_info(m3u8_file_path, formatted_file_name, master_m3u8_url, original_variant_url, subtitles=None, metadata=None):
    print(f"{bcolors.LIGHTBLUE}FHD M3U8 File: {bcolors.ENDC}{m3u8_file_path}")
    print_streams(get_available_streams(master_m3u8_url))
    print_external_subtitles(subtitles or [])
    print_info_metadata(metadata or {})
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")
    cleanup_temp_m3u8(m3u8_file_path)

def build_download_command(source, downloads_path, save_name, mode="auto"):
    selectors = "" if mode == "interactive" else "--select-video best --select-audio best --select-subtitle all "
    download_command = (
        f'N_m3u8DL-RE "{source}" '
        f'--ad-keyword redirector.googlevideo.com '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{save_name}"'
    )
    return append_downloader_proxy(download_command)

def display_master_info(master_m3u8_url, formatted_file_name, subtitles=None, metadata=None):
    print(f"{bcolors.LIGHTBLUE}MASTER M3U8 URL: {bcolors.ENDC}{master_m3u8_url}")
    print_streams(get_master_streams(master_m3u8_url))
    print_external_subtitles(subtitles or [])
    print_info_metadata(metadata or {})
    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")

def display_master_download_command(master_m3u8_url, formatted_file_name, downloads_path, mode="auto", subtitles=None, auto_download=False):
    print(f"{bcolors.LIGHTBLUE}MASTER M3U8 URL: {bcolors.ENDC}{master_m3u8_url}")
    download_command = build_download_command(master_m3u8_url, downloads_path, formatted_file_name, mode)
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
    print(mask_proxy_command(download_command))
    print_external_subtitles(subtitles or [])

    user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
        result = subprocess.run(download_command, shell=True)
        if result.returncode == 0:
            save_external_subtitles(subtitles or [], downloads_path, formatted_file_name)
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
    else:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")

def display_download_command(m3u8_file_path, formatted_file_name, downloads_path, master_m3u8_url, mode="auto", subtitles=None, auto_download=False):
    use_source = m3u8_file_path   # default to local file
    final_save_name = formatted_file_name
    action_master_path = None

    print(f"{bcolors.LIGHTBLUE}FHD M3U8 File: {bcolors.ENDC}{m3u8_file_path}")

    if mode == "interactive":
        action_master_path = build_action_master_m3u8(m3u8_file_path, master_m3u8_url, downloads_path)
        if action_master_path:
            use_source = action_master_path
            print(f"{bcolors.LIGHTBLUE}ACTION M3U8 File: {bcolors.ENDC}{action_master_path}")
        else:
            print(f"{bcolors.WARNING}Could not build action master; using FHD media playlist directly.{bcolors.ENDC}")

    # Pre-flight probe: if it looks bad, pre-switch to master best
    preflight_failed = False if mode == "interactive" else not probe_segment_ok(m3u8_file_path)
    if preflight_failed:
        print(f"{bcolors.YELLOW}{icons.ICON_INFO} Preferred 1080p stream is not available; trying next best stream instead.{bcolors.ENDC}")
        best_url, best_h = pick_best_variant(master_m3u8_url)
        if best_url:
            use_source = best_url
            if best_h:
                final_save_name = replace_resolution_tag(formatted_file_name, best_h)
            print(f"{bcolors.OKGREEN}Selected stream:{bcolors.ENDC} {best_h or 'unknown'}p")
            print(f"{bcolors.OKBLUE}Selected M3U8 URL:{bcolors.ENDC} {best_url}")
        else:
            print(f"{bcolors.FAIL}Could not pick an alternate stream from the master playlist.{bcolors.ENDC}")

    # Build command
    selectors = "" if mode == "interactive" else "--select-video best --select-audio best --select-subtitle all "
    download_command = build_download_command(use_source, downloads_path, final_save_name, mode)
    download_command = append_downloader_proxy(download_command)
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
    print(mask_proxy_command(download_command))
    print_external_subtitles(subtitles or [])

    download_ok = False
    user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        # First attempt
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
        result = subprocess.run(download_command, shell=True)
        download_ok = result.returncode == 0
        if result.returncode != 0 and not preflight_failed:
            if expected_output_exists(downloads_path, final_save_name):
                print(f"{bcolors.WARNING}Downloader returned an error after output was created; skipping fallback.{bcolors.ENDC}")
                save_external_subtitles(subtitles or [], downloads_path, final_save_name)
                cleanup_temp_m3u8(action_master_path, m3u8_file_path)
                print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
                return

            # Runtime fallback: if we *thought* the playlist was fine but the tool failed (404, etc.)
            print(f"{bcolors.YELLOW}{icons.ICON_INFO} Preferred 1080p stream is not available; trying next best stream instead.{bcolors.ENDC}")
            best_url, best_h = pick_best_variant(master_m3u8_url)
            if best_url:
                fallback_save = replace_resolution_tag(formatted_file_name, best_h or 720)
                fb_cmd = (
                    f'N_m3u8DL-RE "{best_url}" '
                    f'--ad-keyword redirector.googlevideo.com '
                    f'{selectors}'
                    f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{fallback_save}"'
                )
                fb_cmd = append_downloader_proxy(fb_cmd)
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                print(mask_proxy_command(fb_cmd))
                fallback_result = subprocess.run(fb_cmd, shell=True)
                if fallback_result.returncode == 0:
                    final_save_name = fallback_save
                    download_ok = True
            else:
                print(f"{bcolors.FAIL}Could not pick an alternate stream from the master playlist.{bcolors.ENDC}")
        if download_ok:
            save_external_subtitles(subtitles or [], downloads_path, final_save_name)
    else:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")

    # Always delete local .m3u8
    cleanup_temp_m3u8(action_master_path, m3u8_file_path)
    if download_ok:
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")


# Function to extract video ID from URL
def extract_video_id(url):
    match = re.search(r'/([^/]+)/?$', url)
    return match.group(1) if match else None

def resolve_video_id_from_page(url):
    try:
        response = requests.get(url, timeout=20, headers=headers)
        response.raise_for_status()
    except Exception:
        return None

    html = response.text
    patterns = [
        r'"urlCode"\s*:\s*"(tpv[0-9a-z]+)"',
        r'data-urlcode="(tpv[0-9a-z]+)"',
        r'/episodes/[^"]+/(tpv[0-9a-z]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    return None

def get_show_page_data(url):
    try:
        response = requests.get(url.replace("10play.com.au", "10.com.au"), timeout=20, headers=headers)
        response.raise_for_status()
    except Exception:
        return {}

    match = re.search(r"const showPageData = ({.*?});", response.text, re.DOTALL)
    if not match:
        return {}

    try:
        return json.loads(match.group(1))
    except Exception:
        return {}

def resolve_short_10_movie_video_id(url):
    data = get_show_page_data(url)
    video = data.get("video") or {}
    if str(video.get("genre") or "").lower() != "movies":
        return None
    return video.get("urlCode")

def ten_signature_header(url):
    timestamp = int(time.time())
    message = f"{timestamp}:{url}".encode("utf-8")
    api_key = bytes.fromhex(TEN_API_KEY_HEX)
    signature = hmac.new(api_key, message, hashlib.sha256).hexdigest()
    return f"{timestamp}_{signature}"

def ten_get(session, url, params=None):
    signed_headers = {
        "User-Agent": "10play/7.4.0.500325 Android UAP",
        "accept": "application/json, text/plain, */*",
        "tp-acceptfeature": "v1/fw;v1/drm;v2/live",
        "tp-platform": "UAP",
        "X-N10-SIG": ten_signature_header(url),
    }
    response = session.get(url, headers=signed_headers, params=params, timeout=20)
    response.raise_for_status()
    return response.json()

def parse_show_slug(series_url):
    parsed = urlparse(series_url.replace("10play.com.au", "10.com.au").strip())
    path_parts = [part for part in parsed.path.split("/") if part]
    return path_parts[0] if path_parts else ""

def looks_like_10_series_url(video_url):
    parsed = urlparse(video_url.replace("10play.com.au", "10.com.au").strip())
    path_parts = [part for part in parsed.path.split("/") if part]
    return "10.com.au" in parsed.netloc and len(path_parts) == 1 and not path_parts[0].lower().startswith("tpv")

def extract_show_id_from_html(session, series_url):
    html_headers = {
        "User-Agent": "Mozilla/5.0 (Ozivine)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    response = session.get(series_url.replace("10play.com.au", "10.com.au"), headers=html_headers, timeout=20)
    response.raise_for_status()

    match = re.search(r"const showPageData = ({.*?});", response.text, re.DOTALL)
    if not match:
        raise ValueError("Failed to parse showPageData from 10 page.")

    data = json.loads(match.group(1))
    show_id = (data.get("video") or {}).get("showUrlCode") or (data.get("show") or {}).get("urlCode")
    if not show_id:
        raise ValueError("showUrlCode/urlCode not found in 10 page data.")

    return show_id

def fetch_ten_endpoints(session):
    data = ten_get(session, TEN_CONFIG_URL, params={"SystemName": "tvos"})
    shows_api = data.get("showsApiEndpoint")
    videos_api = data.get("videosApiEndpoint")
    if not shows_api or not videos_api:
        raise ValueError("Ten config missing showsApiEndpoint or videosApiEndpoint.")
    return shows_api, videos_api

def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def format_date(value):
    if value is None:
        return "Not Available"
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            parsed = dt.datetime.utcfromtimestamp(int(value))
        else:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%d %B %Y")
    except Exception:
        return str(value)

def episode_sort_key(episode):
    season = parse_int(episode.get("season") or episode.get("seasonNumber"))
    episode_number = parse_int(episode.get("episode") or episode.get("episodeNumber"))
    published = episode.get("published") or episode.get("datePublished") or 0
    if isinstance(published, str) and not published.isdigit():
        try:
            published = int(dt.datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
        except Exception:
            published = 0
    return (season, episode_number, parse_int(published))

def get_season_episode_endpoints(show_data):
    seasons = show_data.get("seasons") or []
    endpoints = []
    for season in seasons:
        for menu_item in season.get("menuItems", []) or []:
            if (menu_item.get("menuTitle") or "").lower() == "episodes" and menu_item.get("apiEndpoint"):
                endpoints.append(menu_item["apiEndpoint"])
                break
    return endpoints

def find_video_id(episode):
    for key in ("id", "urlCode", "videoId", "assetId"):
        value = episode.get(key)
        if isinstance(value, str) and value.startswith("tpv"):
            return value
    for value in episode.values():
        if isinstance(value, str) and value.startswith("tpv"):
            return value
    return None

def get_episode_url(show_slug, episode, season, episode_number, video_id):
    episode_path = episode.get("cardLink") or episode.get("webUrl") or episode.get("url")
    if episode_path:
        if episode_path.startswith("http"):
            return episode_path.replace("10play.com.au", "10.com.au")
        return urljoin("https://10.com.au", episode_path)

    if season and episode_number:
        return f"https://10.com.au/{show_slug}/episodes/season-{season}/episode-{episode_number}/{video_id}"
    return f"https://10.com.au/video/{video_id}"

def collect_episode_details(series_url):
    series_url = series_url.replace("10play.com.au", "10.com.au").split("?", 1)[0]
    show_slug = parse_show_slug(series_url)
    if not show_slug:
        raise ValueError("Could not determine 10 show slug from the URL.")

    with requests.Session() as session:
        show_id = extract_show_id_from_html(session, series_url)
        shows_api, videos_api = fetch_ten_endpoints(session)
        show_data = ten_get(session, f"{shows_api.rstrip('/')}/{show_id}")
        if isinstance(show_data, list):
            show_data = show_data[0] if show_data else {}

        endpoints = get_season_episode_endpoints(show_data)
        if not endpoints:
            raise ValueError("Could not find any 10 episode endpoints for this show.")

        raw_episodes = []
        for endpoint in endpoints:
            data = ten_get(session, endpoint)
            if isinstance(data, dict):
                raw_episodes.extend(data.get("items") or data.get("results") or [])
            elif isinstance(data, list):
                raw_episodes.extend(data)

        raw_episodes = sorted(raw_episodes, key=episode_sort_key)
        details = []
        summaries = []
        seen_ids = set()
        for episode in raw_episodes:
            video_id = find_video_id(episode)
            if not video_id or video_id in seen_ids:
                continue

            raw_title = (
                episode.get("subtitle")
                or episode.get("vodTitle")
                or episode.get("title")
            )
            raw_season = episode.get("season") or episode.get("seasonNumber")
            raw_episode = episode.get("episode") or episode.get("episodeNumber")
            needs_enrichment = not (raw_title and raw_season and raw_episode)
            video_data = ten_get(session, f"{videos_api.rstrip('/')}/{video_id}") if needs_enrichment else {}
            show_title = (
                video_data.get("tvShow")
                or episode.get("tvShow")
                or show_data.get("title")
                or show_slug.replace("-", " ").title()
            )
            season = parse_int(video_data.get("season") or raw_season)
            episode_number = parse_int(video_data.get("episode") or raw_episode)
            title = (
                video_data.get("subtitle")
                or video_data.get("vodTitle")
                or video_data.get("title")
                or raw_title
                or f"Episode {episode_number}"
            )
            description = (
                video_data.get("description")
                or video_data.get("abstract")
                or episode.get("description")
                or episode.get("abstract")
                or "Not Available"
            )
            raw_date = video_data.get("published") or episode.get("published") or episode.get("datePublished")
            thumbnail = (
                video_data.get("imageUrl")
                or video_data.get("posterImageUrl")
                or (episode.get("cardImage") or {}).get("url")
                or "Not Available"
            )
            season_label = f"Season {season}" if season else "Episodes"
            episode_url = get_episode_url(show_slug, episode, season, episode_number, video_id)

            details.append({
                "Video URL": episode_url,
                "Video ID": video_id,
                "Show Title": show_title,
                "Title": title,
                "Season": season,
                "Season Label": season_label,
                "Episode": episode_number,
                "Episode Label": str(episode_number or "-"),
                "Date Aired": format_date(raw_date),
                "Description": description,
                "Thumbnail": thumbnail,
                "Sort Episode": episode_number,
            })
            summaries.append(f"{show_title} {season_label} Episode {episode_number} ID: {video_id}")
            seen_ids.add(video_id)

    return show_slug, {
        "Episode Summary": summaries,
        "Episode Details": details,
    }

def save_episode_list_json(show_slug, episode_data):
    os.makedirs(TEMP_DIR, exist_ok=True)
    output_path = os.path.join(TEMP_DIR, f"10play_{safe_windows_filename(show_slug)}_episodes.json")
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(episode_data, file, ensure_ascii=False, indent=4)
    return output_path

def export_episode_list_text(show_slug, episodes):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(EXPORT_DIR, f"10_{safe_windows_filename(show_slug)}_export_{timestamp}.txt")

    with open(output_path, "w", encoding="utf-8") as file:
        for episode in episodes:
            label = episode.get("Season Label") or "Episodes"
            episode_number = episode.get("Episode Label") or episode.get("Episode") or "-"
            title = episode.get("Title") or "-"
            url = episode.get("Video URL") or "-"
            file.write(f"{label} Episode {episode_number} - {title}\n")
            file.write(f"{url}\n")

    return output_path

def print_episode_list(series_title, episodes):
    if not episodes:
        print(f"{bcolors.WARNING}No playable 10 episodes found.{bcolors.ENDC}")
        return

    tree_style = "grey70"
    label_style = "bold grey70"
    header_style = "bright_blue"
    groups = {}
    for episode in episodes:
        label = episode.get("Season Label") or "Episodes"
        groups.setdefault(label, []).append(episode)

    group_labels = sorted(groups, key=lambda label: parse_int(re.search(r"\d+", label).group(0)) if re.search(r"\d+", label) else 0)
    for group_episodes in groups.values():
        group_episodes.sort(key=lambda item: item.get("Sort Episode") or item.get("Episode") or 0)

    season_summary = ",  ".join(f"{label}({len(groups[label])})" for label in group_labels)
    console.print(Rule(Text.assemble(("10 Series: ", f"bold {header_style}"), (series_title, "bold white")), style=header_style))
    console.print()
    console.print(
        Text.assemble(
            (f"{len(group_labels)} Seasons", label_style),
            (f",  {season_summary}" if season_summary else "", "white"),
        )
    )

    for group_index, label in enumerate(group_labels):
        if group_index > 0:
            console.print(Text("│", style=tree_style))

        group_is_last = group_index == len(group_labels) - 1
        group_branch = "└─" if group_is_last else "├─"
        group_child_prefix = "   " if group_is_last else "│  "
        group_episodes = groups[label]
        console.print(Text.assemble((f"{group_branch} ", tree_style), (f"{label}: ", label_style), (f"{len(group_episodes)} episodes", "white")))

        for index, episode in enumerate(group_episodes):
            is_last = index == len(group_episodes) - 1
            branch = "└─" if is_last else "├─"
            url_branch = "  " if is_last else "│ "
            console.print(
                Text.assemble(
                    (group_child_prefix, tree_style),
                    (f"{branch} ", tree_style),
                    (f"{episode.get('Episode Label') or '-'}. ", label_style),
                    (episode.get("Title") or "-", "white"),
                )
            )
            console.print(
                Text.assemble(
                    (group_child_prefix, tree_style),
                    (f"{url_branch} ", tree_style),
                    (episode.get("Video URL") or "-", "bright_blue"),
                )
            )

def list_show_episodes(series_url, export_list=False):
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    show_slug, episode_data = collect_episode_details(series_url)
    episodes = episode_data["Episode Details"]
    series_title = episodes[0].get("Show Title") if episodes else show_slug.replace("-", " ").title()
    output_path = save_episode_list_json(show_slug, episode_data)

    try:
        console.print()
        print_episode_list(series_title, episodes)
        print(f"\n{bcolors.OKGREEN}{icons.ICON_SUCCESS} Found {len(episodes)} episode(s){bcolors.ENDC}")
        if export_list:
            export_path = export_episode_list_text(show_slug, episodes)
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Exported list: {export_path}{bcolors.ENDC}")
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)

def parse_selector_part(selector_part):
    match = re.fullmatch(r"s(?P<season>\d{2}|\d{4})(?:e(?P<episode>\d{2}))?", selector_part)
    if not match:
        raise ValueError(
            "Download selector must be sXXeXX, sXXXXeXX, sXX, sXXXX, or a matching range. "
            "Examples: s01e01, s2026e01, s01, s2026, s01e03-s02e02, s01-s03"
        )

    return {
        "season": int(match.group("season")),
        "episode": int(match.group("episode")) if match.group("episode") else None,
    }

def parse_download_selector(selector):
    selector = str(selector or "").strip().lower()
    if "-" not in selector:
        part = parse_selector_part(selector)
        return {
            "type": "single_episode" if part["episode"] is not None else "single_season",
            "start": part,
            "end": part,
        }

    range_parts = selector.split("-", 1)
    if not range_parts[0] or not range_parts[1]:
        raise ValueError(
            "Download range must include both start and end selectors. "
            "Examples: s01e03-s02e02 or s01-s03"
        )

    start = parse_selector_part(range_parts[0])
    end = parse_selector_part(range_parts[1])
    start_has_episode = start["episode"] is not None
    end_has_episode = end["episode"] is not None

    if start_has_episode != end_has_episode:
        raise ValueError("Download range must use two episode selectors or two season selectors.")

    if start_has_episode:
        if (start["season"], start["episode"]) > (end["season"], end["episode"]):
            raise ValueError("Download episode range start must be before the end selector.")
        return {"type": "episode_range", "start": start, "end": end}

    if start["season"] > end["season"]:
        raise ValueError("Download season range start must be before the end selector.")
    return {"type": "season_range", "start": start, "end": end}

def format_selector_part(part):
    season = part["season"]
    season_label = f"s{season:04d}" if season >= 1000 else f"s{season:02d}"
    if part["episode"] is not None:
        return f"{season_label}e{part['episode']:02d}"
    return season_label

def format_download_selector(parsed_selector):
    if parsed_selector["start"] == parsed_selector["end"]:
        return format_selector_part(parsed_selector["start"])
    return f"{format_selector_part(parsed_selector['start'])}-{format_selector_part(parsed_selector['end'])}"

def format_queue_selector(season, episode=None):
    season_label = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
    if episode is not None:
        return f"{season_label}E{episode:02d}"
    return season_label

def warn_if_partial_range_match(parsed_selector, selected):
    if parsed_selector["type"] == "episode_range":
        requested_start = (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
        requested_end = (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
        matched_start = (int(selected[0].get("Season") or 0), int(selected[0].get("Episode") or 0))
        matched_end = (int(selected[-1].get("Season") or 0), int(selected[-1].get("Episode") or 0))
        if matched_start > requested_start or matched_end < requested_end:
            matched_label = f"{format_queue_selector(*matched_start)}-{format_queue_selector(*matched_end)}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched {matched_label}.{bcolors.ENDC}")

    if parsed_selector["type"] == "season_range":
        requested_start = parsed_selector["start"]["season"]
        requested_end = parsed_selector["end"]["season"]
        matched_seasons = sorted({int(item.get("Season") or 0) for item in selected})
        if matched_seasons[0] > requested_start or matched_seasons[-1] < requested_end:
            matched_label = f"{format_queue_selector(matched_seasons[0])}-{format_queue_selector(matched_seasons[-1])}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched seasons {matched_label}.{bcolors.ENDC}")

def get_series_episodes(series_url):
    show_slug, episode_data = collect_episode_details(series_url)
    return show_slug, episode_data["Episode Details"]

def select_episodes(series_url, selector):
    parsed_selector = parse_download_selector(selector)
    show_slug, episodes = get_series_episodes(series_url)
    selected = []
    for item in episodes:
        season = int(item.get("Season") or 0)
        episode = int(item.get("Episode") or 0)
        if episode <= 0:
            continue

        if parsed_selector["type"] == "single_episode":
            keep = season == parsed_selector["start"]["season"] and episode == parsed_selector["start"]["episode"]
        elif parsed_selector["type"] == "single_season":
            keep = season == parsed_selector["start"]["season"]
        elif parsed_selector["type"] == "episode_range":
            keep = (
                (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
                <= (season, episode)
                <= (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
            )
        else:
            keep = parsed_selector["start"]["season"] <= season <= parsed_selector["end"]["season"]

        if keep:
            selected.append(item)

    if not selected:
        normalized = format_download_selector(parsed_selector)
        series_title = episodes[0].get("Show Title") if episodes else show_slug.replace("-", " ").title()
        raise LookupError(f"No 10 episodes found for selector {normalized} in {series_title}.")

    selected.sort(key=lambda item: (int(item.get("Season") or 0), int(item.get("Episode") or 0)))
    warn_if_partial_range_match(parsed_selector, selected)
    return selected

def print_download_queue(episodes):
    console.print()
    console.print(Text("Download queue:", style="bold bright_blue"))
    for episode in episodes:
        season = int(episode.get("Season") or 0)
        episode_number = int(episode.get("Episode") or 0)
        season_label = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
        console.print(
            Text.assemble(
                (f"{season_label}E{episode_number:02d} ", "bold grey70"),
                (episode.get("Title") or "-", "white"),
            )
        )

def download_selected_episodes(series_url, selector, downloads_path, credentials):
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    try:
        episodes = select_episodes(series_url, selector)
    except LookupError as error:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} {error}{bcolors.ENDC}")
        return
    print_download_queue(episodes)

    user_input = input(f"\nDownload {len(episodes)} episode(s)? Y or N: ").strip().lower()
    if user_input != "y":
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
        return

    for index, episode in enumerate(episodes, start=1):
        print(f"\n{bcolors.LIGHTBLUE}{icons.ICON_INFO} Downloading {index}/{len(episodes)}: {episode.get('Title') or episode.get('Video URL')}{bcolors.ENDC}")
        main(episode["Video URL"], downloads_path, credentials, mode="auto", export_list=False, download_selector=None, auto_download=True)

# Main logic
def main(video_url, downloads_path, credentials, mode="auto", export_list=False, download_selector=None, auto_download=False):
    if mode == "list":
        list_show_episodes(video_url, export_list)
        return

    if mode == "download":
        download_selected_episodes(video_url, download_selector, downloads_path, credentials)
        return

    config = load_config()
    video_id = extract_video_id(video_url)
    
    if not video_id:
        print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Invalid URL. Please enter a valid 10 video URL.{bcolors.ENDC}")
        return

    token = get_bearer_token(config, credentials)
    if token:
        if not video_id.lower().startswith("tpv"):
            is_short_show_page = looks_like_10_series_url(video_url)
            resolved_video_id = resolve_short_10_movie_video_id(video_url)
            if not resolved_video_id and not is_short_show_page:
                resolved_video_id = resolve_video_id_from_page(video_url)
            if resolved_video_id:
                video_id = resolved_video_id
                print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Resolved page to video ID: {video_id}{bcolors.ENDC}")
            elif is_short_show_page:
                print(f"{bcolors.WARNING}{icons.ICON_WARNING} 10 series URLs need a flag.{bcolors.ENDC}")
                print(f"{bcolors.YELLOW}{icons.ICON_INFO} Use -l to list episodes or -d with a selector to download from a series.{bcolors.ENDC}")
                return

        manifest_url, video_data = extract_video_details(video_id, token, video_url)
        if manifest_url and video_data:
            formatted_file_name = format_file_name(video_data)
            subtitles = collect_external_subtitles(manifest_url)

            if is_movie(video_data):
                best_url, best_h = pick_best_variant(manifest_url)
                if best_url and best_h:
                    formatted_file_name = replace_resolution_tag(formatted_file_name, best_h)

                if mode == "info":
                    display_master_info(manifest_url, formatted_file_name, subtitles, video_data)
                else:
                    display_master_download_command(manifest_url, formatted_file_name, downloads_path, mode, subtitles, auto_download)
                return

            variant_url = download_and_select_variant(manifest_url)
            if variant_url:
                local_m3u8_file, original_variant_url = modify_and_save_m3u8(variant_url, downloads_path)
                if local_m3u8_file:
                    # Print the original 960x540 URL and the local path
                    print(f"{bcolors.LIGHTBLUE}MASTER M3U8 URL: {bcolors.ENDC}{manifest_url}")
                    print(f"{bcolors.LIGHTBLUE}SD M3U8 URL: {bcolors.ENDC}{original_variant_url}")
                    if mode == "info":
                        display_info(local_m3u8_file, formatted_file_name, manifest_url, original_variant_url, subtitles, video_data)
                    else:
                        display_download_command(local_m3u8_file, formatted_file_name, downloads_path, manifest_url, mode, subtitles, auto_download)
                else:
                    print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Failed to modify and save the variant M3U8{bcolors.ENDC}")
            else:
                print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Failed to find 960x540 variant{bcolors.ENDC}")
        else:
            print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Failed to extract manifest URL{bcolors.ENDC}")
    else:
        print(f"{bcolors.FAIL}{icons.ICON_FAILURE} Login failed{bcolors.ENDC}")
