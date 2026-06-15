import requests
import re
import json
import subprocess
from xml.etree import ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import base64
import binascii
import datetime

from helpers.colors import bcolors
from helpers.subtitles import sanitize_filename, describe, embed_subtitles

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
    year_episode_match = re.search(r'9now\.com\.au/([^/]+)/(\d{4})/episode-(\d+)', video_url)
    special_episode_match = re.search(r'9now\.com\.au/([^/]+)/special/episode-(\d+)', video_url)

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
        season_slug = clip_match.group('season')  # e.g. season-20252026
        clip_slug = clip_match.group('clip')  # e.g. clip-cmeop4x...

        # Fetch the season landing page, find the clip by its link path
        season_page = _get_season_page(series_name, season_slug)
        clips = _extract_clips_from_page(season_page)

        # Sort newest -> oldest, then find index
        clips_sorted = sorted(clips, key=_clip_sort_key, reverse=True)

        # The webUrl will look like "/{series}/{season}/{clip-slug}"
        target_path_tail = f"/{series_name}/{season_slug}/{clip_slug}".lower()

        found = None
        for idx, c in enumerate(clips_sorted, start=1):
            web_url = ((c.get('link') or {}).get('webUrl') or '').lower()
            if web_url.endswith(target_path_tail):
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


# Function to collect subtitle tracks from a Brightcove playback API response.
# Brightcove returns subtitles as a "text_tracks" list alongside "sources".
# Each entry has: src (URL), srclang (BCP-47), label, kind, mime_type, default.
# We only want WebVTT captions/subtitles tracks (not chapters or metadata).
def collect_subtitles(response):
    out = []
    for t in response.get("text_tracks", []):
        # Brightcove uses "text/webvtt" in practice; also accept "text/vtt".
        if t.get("mime_type", "").lower() not in ("text/vtt", "text/webvtt"):
            continue
        if t.get("kind", "").lower() not in ("captions", "subtitles"):
            continue
        # Skip thumbnail sprite tracks (no srclang, labelled "thumbnails").
        if not t.get("srclang"):
            continue
        out.append({
            "url": t["src"],
            "lang": t.get("srclang", "und"),
            "name": t.get("label", ""),
            "default": bool(t.get("default")),
            "forced": False,
        })
    return out


# Function to process and print the download command
def get_download_command(video_url, downloads_path, wvd_device_path):
    video_info = get_video_id_from_url(video_url)

    # Handle clip (5 values) vs episode (4 values)
    if len(video_info) == 5:
        series_name, season, episode, video_id, clip_title = video_info
    else:
        series_name, season, episode, video_id = video_info
        clip_title = None

    session = requests.Session()  # Use a session to maintain cookies and headers
    response = session.get(BRIGHTCOVE_API(video_id), headers=BRIGHTCOVE_HEADERS).json()

    # Collect subtitle tracks from the Brightcove response (not in the MPD/m3u8).
    subtitles = collect_subtitles(response)

    download_command = None

    if 'sources' in response:
        sources = response['sources']
        source = next((src for src in sources if 'key_systems' in src and 'com.widevine.alpha' in src['key_systems']), None)
        if source:
            mpd_url = source['src']
            lic_url = source['key_systems']['com.widevine.alpha']['license_url']
            pssh = get_pssh(mpd_url)
            max_height = get_max_height_mpd(mpd_url)
            if pssh:
                keys = get_keys(pssh, lic_url, wvd_device_path)
                # Print the requested information
                print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
                print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
                print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
                for key in keys:
                    print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
                if subtitles:
                    print(f"{bcolors.OKCYAN}Subtitles to embed: {bcolors.ENDC}{describe(subtitles)}")
                else:
                    print(f"{bcolors.WARNING}No subtitles available for this title.{bcolors.ENDC}")
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")

                # Build safe base filename
                base_name = series_name.title().replace('-', '.').replace(' ', '.').replace('_', '.').replace('/', '.').replace(':', '.')
                if clip_title:
                    formatted_file_name = sanitize_filename(f"{base_name}.{clip_title}.{season}{episode}.{max_height}p.9NOW.WEB-DL.AAC2.0.H.264")
                else:
                    formatted_file_name = sanitize_filename(f"{base_name}.{season}{episode}.{max_height}p.9NOW.WEB-DL.AAC2.0.H.264")

                # Subtitles are not in the MPD; handled separately via embed_subtitles.
                download_command = f"""N_m3u8DL-RE "{mpd_url}" --select-video best --select-audio best -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}" --key """ + ' --key '.join(
                    keys)
                print(download_command)
        else:
            # Handling for unencrypted videos (dynamic delivery / Boltdns HLS).
            # Boltdns URLs don't contain 'master.m3u8', so select by MIME type.
            # Prefer HLS v5 (https) > v4 > v3, falling back to any HLS source.
            hls_sources = [
                src for src in sources
                if (src.get("type") or "").lower() == "application/x-mpegurl"
                   and src.get("src", "").startswith("https")
            ]
            # Sort so higher HLS version numbers come first (v5 > v4 > v3).
            hls_sources.sort(key=lambda s: s.get("src", ""), reverse=True)
            unencrypted_source = hls_sources[0] if hls_sources else None
            if unencrypted_source:
                m3u8_url = unencrypted_source['src']
                max_height = get_max_height_m3u8(m3u8_url)
                # Print the download command for unencrypted videos
                print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{m3u8_url}")
                if subtitles:
                    print(f"{bcolors.OKCYAN}Subtitles to embed: {bcolors.ENDC}{describe(subtitles)}")
                else:
                    print(f"{bcolors.WARNING}No subtitles available for this title.{bcolors.ENDC}")
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")

                # Build safe base filename
                base_name = series_name.title().replace('-', '.').replace(' ', '.').replace('_', '.').replace('/', '.').replace(':', '.')
                if clip_title:
                    formatted_file_name = sanitize_filename(f"{base_name}.{clip_title}.{season}{episode}.{max_height}p.9NOW.WEB-DL.AAC2.0.H.264")
                else:
                    formatted_file_name = sanitize_filename(f"{base_name}.{season}{episode}.{max_height}p.9NOW.WEB-DL.AAC2.0.H.264")

                # Subtitles come from the Brightcove text_tracks response, not the
                # m3u8 manifest; handled separately via embed_subtitles below.
                download_command = f"""N_m3u8DL-RE "{m3u8_url}" --select-video best --select-audio best -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}" """
                print(download_command)
            else:
                print("No suitable source found for unencrypted video")
    else:
        print("No 'sources' found in the response")

    if download_command:
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == 'y':
            subprocess.run(download_command, shell=True)
            if subtitles:
                embed_subtitles(downloads_path, formatted_file_name, subtitles)


# Main execution flow
def main(video_url, downloads_path, wvd_device_path):
    get_download_command(video_url, downloads_path, wvd_device_path)
