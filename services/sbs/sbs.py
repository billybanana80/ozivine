import base64
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
import requests
import yaml

from helpers.colors import bcolors
from helpers.subtitles import sanitize_filename, describe, embed_subtitles

#   Ozivine: SBS On Demand Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the m3u8 Manifest.
#   eg: https://www.sbs.com.au/ondemand/watch/2336216643518	 or https://www.sbs.com.au/ondemand/tv-series/the-responder/season-2/the-responder-s2-ep2/2336216643518
#   Authentication: Username/password
#   Geo-Locking: requires an Australian IP address
#   Quality: up to 720p
#   Key Features:
#   1. Extract Video ID: Parses the SBS video URL to extract the video id and then fetches the show/movie info from the SBS API.
#   2. Print Download Information: Outputs the M3U8 URL required for downloading the video content.
#   3. Note: this script functions for non-encrypted video files only (SBS files are not currently encrypted).
#   4. Note: you will need a free SBS account to obtain username/password credentials.


SBS_LOGIN_URL = "https://auth.sbs.com.au/login"
SBS_PLAYBACK_URL = "https://playback.pr.sbsod.com/stream/{video_id}"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config.yaml")
CONFIG_PATH = os.path.abspath(CONFIG_PATH)

# Subtitle behaviour: True downloads only English tracks (English / English CC),
# False downloads every subtitle track SBS offers (Arabic, Korean, Vietnamese, etc).
ENGLISH_ONLY = True


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


# Check for cached token
def ensure_sbs_cache(config):
    config.setdefault("credentials", {})
    config.setdefault("sbs", {})
    config["sbs"].setdefault("cache", {})
    config["sbs"]["cache"].setdefault("login", {})
    return config


# Obtain login credentials from config
def parse_sbs_credentials(credentials):
    creds = (credentials or "").strip()
    if not creds or ":" not in creds:
        raise ValueError("Missing SBS credentials. Expected username:password")

    username, password = creds.split(":", 1)
    username = username.strip()
    password = password.strip()

    if not username or not password:
        raise ValueError("Invalid SBS credentials. Expected username:password")

    return username, password


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# Define token expiry
def jwt_expiry_utc(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        data = json.loads(decoded)
        exp = data.get("exp")
        if not exp:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        return None


# Check if token is valid
def token_is_valid(token, expiry, buffer_minutes=5):
    if not token or not expiry:
        return False

    expiry_dt = parse_iso_datetime(expiry)
    if not expiry_dt:
        return False

    now = datetime.now(timezone.utc)
    return expiry_dt > now + timedelta(minutes=buffer_minutes)


def mask_value(value):
    if not value:
        return "NONE"
    if len(value) <= 20:
        return value
    return f"{value[:10]}...{value[-10:]}"


# Login request
def sbs_login(username, password):
    headers = {
        "user-agent": (
            "okhttp/4.10.0"
        ),
    }

    payload = {
        "email": username,
        "password": password,
        "deviceName": "Android TV",
    }

    response = requests.post(SBS_LOGIN_URL, headers=headers, json=payload, timeout=20)

    # print(f"[DEBUG] login status: {response.status_code}")
    # print(f"[DEBUG] login content-type: {response.headers.get('content-type', '')}")
    # print(f"[DEBUG] login preview: {response.text[:500]}")

    if response.status_code != 200:
        raise RuntimeError(f"SBS login failed, status code: {response.status_code}")

    try:
        data = response.json()
    except Exception as e:
        raise RuntimeError(f"SBS login did not return valid JSON: {e}")

    access_token = data.get("accessToken")
    id_token = data.get("idToken")

    if not access_token:
        raise RuntimeError("SBS login response did not contain accessToken")

    expiry_dt = jwt_expiry_utc(access_token)

    return {
        "token": access_token,
        "id_token": id_token or "",
        "expiry": expiry_dt.isoformat() if expiry_dt else "",
    }


# Function to retrieve access token
def get_sbs_access_token(config, credentials):
    config = ensure_sbs_cache(config)
    cache = config["sbs"]["cache"]["login"]

    cached_token = cache.get("token", "")
    cached_expiry = cache.get("expiry", "")

    if token_is_valid(cached_token, cached_expiry):
        print(f"{bcolors.OKGREEN}✅ Using cached token{bcolors.ENDC}")
        return cached_token

    username, password = parse_sbs_credentials(credentials)
    print(f"{bcolors.OKCYAN}Cached token missing/expired, logging in...{bcolors.ENDC}")

    login_data = sbs_login(username, password)

    cache["token"] = login_data["token"]
    cache["id_token"] = login_data["id_token"]
    cache["expiry"] = login_data["expiry"]

    save_config(config)

    print(f"{bcolors.OKGREEN}✅ Token cache updated{bcolors.ENDC}")
    return login_data["token"]


# Function to extract video ID from URL
def extract_video_id(video_url):
    match = re.search(r"/(\d+)", video_url)
    return match.group(1) if match else None


# Function to get show information from playback catalogue
def get_playback_data(video_id, access_token):
    url = SBS_PLAYBACK_URL.format(video_id=video_id)

    headers = {
        "authorization": f"Bearer {access_token}",
        "user-agent": (
            "okhttp/4.10.0"
        ),
    }

    payload = {
        "deviceClass": "androidtv",
        "advertising": {
            "headerBidding": True,
            "telariaID": "",
            "ozTamSessionID": "",
            "subtitle": "",
            "resume": True,
        },
        "streamOptions": {
            "audio": "demuxed"
        },
        "streamProviders": ["GoogleDAI", "HLS"],
    }

    response = requests.post(url, headers=headers, json=payload, timeout=20)

    # print(f"[DEBUG] playback status: {response.status_code}")
    # print(f"[DEBUG] playback content-type: {response.headers.get('content-type', '')}")
    # print(f"[DEBUG] playback preview: {response.text[:1000]}")

    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch playback data, status code: {response.status_code}")

    try:
        return response.json()
    except Exception as e:
        raise RuntimeError(f"Playback endpoint did not return valid JSON: {e}")


# Function to get the HLS stream provider (carries both the manifest URL and the
# subtitle track list).
def find_hls_provider(playback_data):
    for provider in playback_data.get("streamProviders", []):
        if provider.get("type") == "HLS" and provider.get("url"):
            return provider
    return None


# Function to collect the wanted subtitle tracks from the playback data.
# SBS does not put subtitles in the HLS manifest; it returns them as a "textTracks"
# list on the HLS stream provider. Each row looks like:
#   {"name": "English (CC)", "type": "CAPTION"/"SUBTITLE", "url": "...VTT",
#    "lang": "en", "format": "WebVTT", "attributes": ["AUTOSELECT=YES", "DEFAULT=YES"]}
def collect_subtitles(provider, english_only=True):
    subs = []
    for row in provider.get("textTracks", []) or []:
        url = row.get("url")
        lang = (row.get("lang") or "und").strip()
        if not url:
            continue
        if english_only and not lang.lower().startswith("en"):
            continue
        # Attributes are HLS-style KEY=VALUE strings, e.g. "DEFAULT=YES".
        attribs = [str(a).upper() for a in row.get("attributes", [])]
        subs.append({
            "url": url,
            "lang": lang,
            "name": (row.get("name") or "").strip(),
            "default": "DEFAULT=YES" in attribs,
            "forced": "FORCED=YES" in attribs,
        })
    return subs


# Function to get max resolution from manifest
def get_max_height_m3u8(url_m3u8):
    try:
        response = requests.get(url_m3u8, timeout=20)
        response.raise_for_status()

        max_height = 0
        for line in response.text.splitlines():
            match = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if match:
                height = int(match.group(1))
                max_height = max(max_height, height)

        return max_height
    except Exception as e:
        print(f"Error fetching max height from m3u8: {e}")
        return 0


# Function to build the video file name
def build_filename(playback_data, video_height):
    entity_type = playback_data.get("entityType", "")
    title = (playback_data.get("title") or "Unknown Title").replace(" ", ".")
    series_title = (playback_data.get("seriesTitle") or "").replace(" ", ".")
    season_number = str(playback_data.get("seasonNumber", 0)).zfill(2)
    episode_number = str(playback_data.get("episodeNumber", 0)).zfill(2)

    resolution_tag = f"{video_height or 720}p"

    if entity_type == "MOVIE" or not series_title:
        name = f"{title}.{resolution_tag}.SBS.WEB-DL.AAC2.0.H.264"
    else:
        name = f"{series_title}.S{season_number}E{episode_number}.{title}.{resolution_tag}.SBS.WEB-DL.AAC2.0.H.264"

    return sanitize_filename(name)


# Function to extract and print m3u8 URL
def extract_info(video_url, access_token):
    video_id = extract_video_id(video_url)
    if not video_id:
        print("Failed to extract video ID from the URL.")
        return None, None, []

    playback_data = get_playback_data(video_id, access_token)

    provider = find_hls_provider(playback_data)
    if not provider:
        print("No HLS manifest URL found in playback data.")
        print(json.dumps(playback_data, indent=2)[:4000])
        return None, None, []

    manifest_url = provider["url"]
    subtitles = collect_subtitles(provider, english_only=ENGLISH_ONLY)

    video_height = get_max_height_m3u8(manifest_url)
    formatted_file_name = build_filename(playback_data, video_height)
    return manifest_url, formatted_file_name, subtitles


# Function to format and display download command
def display_download_command(manifest_url, formatted_file_name, downloads_path, subtitles):
    # Subtitles are handled separately (downloaded + muxed below); SBS does not put
    # them in the HLS manifest, so no --select-subtitle flag is needed here.
    download_command = (
        f'N_m3u8DL-RE "{manifest_url}" '
        f'--select-video best --select-audio best '
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}"'
    )

    print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest_url}")
    if subtitles:
        print(f"{bcolors.OKCYAN}Subtitles to embed: {bcolors.ENDC}{describe(subtitles)}")
    else:
        print(f"{bcolors.WARNING}No subtitles available for this title.{bcolors.ENDC}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(download_command)

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == "y":
        subprocess.run(download_command, shell=True)
        if subtitles:
            embed_subtitles(downloads_path, formatted_file_name, subtitles)


# Main function
def main(video_url, downloads_path, credentials):
    config = load_config()
    access_token = get_sbs_access_token(config, credentials)

    manifest_url, formatted_file_name, subtitles = extract_info(video_url, access_token)
    if not manifest_url:
        return

    display_download_command(manifest_url, formatted_file_name, downloads_path, subtitles)
