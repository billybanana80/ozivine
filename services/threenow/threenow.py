import requests
import json
import os
import re
import xml.etree.ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import subprocess
from datetime import datetime
import time
from urllib.parse import unquote, urljoin, urlparse
from rich.console import Console
from rich.rule import Rule
from rich.text import Text
from colors import bcolors
import icons
from filename_utils import safe_windows_filename
from services.proxy import append_downloader_proxy, mask_proxy_command


# Brightcove constants
BRIGHTCOVE_KEY = 'BCpkADawqM2NDYVFYXV66rIDrq6i9YpFSTom-hlJ_pdoGkeWuItRDsn1Bhm7QVyQvFIF0OExqoywBvX5-aAFaxYHPlq9st-1mQ73ZONxFHTx0N7opvkHJYpbd_Hi1gJuPP5qCFxyxB8oevg-'
BRIGHTCOVE_ACCOUNT = '3812193411001'
TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "temp"))
EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "export"))
console = Console()
BRIGHTCOVE_HEADERS = {
    "BCOV-POLICY": BRIGHTCOVE_KEY,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.threenow.co.nz",
    "Referer": "https://www.threenow.co.nz/"
}

def clean_title(value):
    value = re.sub(r'[,\-]', ' ', str(value or ""))
    value = re.sub(r'\s+', '.', value.strip())
    value = re.sub(r'\.+', '.', value)
    return value.strip('.')

def season_episode_tag(season, episode):
    try:
        return f"S{int(season):02}E{int(episode):02}"
    except (TypeError, ValueError):
        return None

def get_url_season_episode(video_url):
    match = re.search(r'/season-(\d+)-ep-(\d+)(?:/|$)', video_url, re.IGNORECASE)
    if not match:
        return None
    return season_episode_tag(match.group(1), match.group(2))

def episode_with_season(episode, season):
    episode = dict(episode)
    episode.setdefault("seasonNumber", season.get("seasonNumber"))
    episode.setdefault("season", season.get("seasonNumber"))
    return episode

def get_short_movie_video_info(video_url):
    path_parts = [part for part in urlparse(video_url).path.split("/") if part]
    if len(path_parts) != 3 or path_parts[0] != "shows":
        return None

    show_slug, show_id = path_parts[1], path_parts[2]
    data = fetch_show_catalogue(show_id)
    genres = [str(genre).lower() for genre in data.get("genres") or []]
    episodes = data.get("episodes") or []

    if "movies" not in genres or len(episodes) != 1:
        return None

    episode = dict(episodes[0])
    episode.setdefault("showId", show_id)
    episode.setdefault("showTitle", data.get("name") or format_show_title(show_slug))
    episode.setdefault("name", data.get("name") or episode.get("name") or format_show_title(show_slug))
    return episode

# Get the Brightcove Video ID from the video URL
def get_video_info(video_url):
    # Extract show_id and videoId from the URL
    match = re.search(r'shows/[^/]+/(?:[^/]+/)*([^/]+)/([^/]+)$', video_url)
    if not match:
        movie_info = get_short_movie_video_info(video_url)
        if movie_info:
            return movie_info

        path_parts = [part for part in urlparse(video_url).path.split("/") if part]
        if len(path_parts) == 3 and path_parts[0] == "shows":
            raise ValueError("THREENOW_SERIES_URL_NEEDS_FLAG")
        raise ValueError("Could not extract show_id and videoId from the URL.\n Enter the full video URL please:\n eg: https://www.threenow.co.nz/shows/thirst-with-shay-mitchell/season-1-ep-1/1718148621037/M86965-766")
    
    show_id, video_id = match.groups()

    # Print the extracted show_id and video_id for debugging
    # print(f"Extracted show_id: {show_id}")
    # print(f"Extracted video_id: {video_id}")
    
    api_url = f"https://now-api.fullscreen.nz/v5/shows/{show_id}"
    
    # Print the api_url for debugging
    # print(f"API URL: {api_url}")
    
    response = requests.get(api_url)
    response.raise_for_status()
    data = response.json()
    
    # Check if genres indicate a movie or current affairs or comedy
    if "movie" in data.get("genres", []) or "current-affairs" in data.get("genres", []) or "comedy" in data.get("genres", []):
        # Use easyWatch section if it exists
        if "easyWatch" in data and "externalMediaId" in data["easyWatch"]:
            if data["easyWatch"]["videoId"] == video_id:
                return data["easyWatch"]
        # Otherwise, use the episodes section
        for episode in data.get("episodes", []):
            if episode.get("videoId") == video_id or episode.get("externalMediaId") == video_id:
                return episode
        # Additionally, check within seasons for current affairs or comedy
        for season in data.get("seasons", []):
            for episode in season.get("episodes", []):
                if episode.get("videoId") == video_id or episode.get("externalMediaId") == video_id:
                    return episode_with_season(episode, season)
    else:
        for season in data.get("seasons", []):
            for episode in season.get("episodes", []):
                if episode.get("videoId") == video_id or episode.get("externalMediaId") == video_id:
                    return episode_with_season(episode, season)
    
    raise ValueError("Could not find the video ID in the API response.")

# Get additional video information for filename formatting
def get_additional_video_info(show_id, video_id):
    api_url = f"https://now-api.fullscreen.nz/v5/shows/{show_id}/{video_id}"
    response = requests.get(api_url)
    response.raise_for_status()
    return response.json()

# Get the video information from the Brightcove API
def get_playback_info(bc_video_id):
    url = f"https://edge.api.brightcove.com/playback/v1/accounts/{BRIGHTCOVE_ACCOUNT}/videos/{bc_video_id}"
    # print(f"Brightcove video ID (bc_video_id): {bc_video_id}")  # Print the bc_video_id for debugging
    response = requests.get(url, headers=BRIGHTCOVE_HEADERS)
    response.raise_for_status()
    return response.json()

# Get the manifest URL (MPD or M3U8)
def get_manifest_url(playback_info):
    dash_source = None
    hls_source = None

    for source in playback_info['sources']:
        if source.get('type') == 'application/dash+xml' and 'playready' not in source['src']:
            dash_source = (source['src'], source['key_systems']['com.widevine.alpha']['license_url'] if 'key_systems' in source and 'com.widevine.alpha' in source['key_systems'] else None)
        elif source.get('type') == 'application/x-mpegURL':
            hls_source = (source['src'], None)
    
    if dash_source:
        return dash_source
    elif hls_source:
        return hls_source
    else:
        raise Exception("Manifest URL not found in playback info")

# Extract the PSSH and Licence from the MPD
def get_pssh_and_license(url_mpd):
    response = requests.get(url_mpd)
    response.raise_for_status()
    content = response.content
    if not content:
        raise ValueError("MPD content is empty or invalid")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"XML parsing error: {e}")
        print(f"MPD content: {content}")
        raise

    pssh_elem = None
    license_url = None

    for elem in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}ContentProtection"):
        if elem.attrib.get('schemeIdUri') == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed":
            pssh_elem = elem.find("{urn:mpeg:cenc:2013}pssh")
            if pssh_elem is not None:
                license_url = elem.attrib.get('{urn:brightcove:2015}licenseAcquisitionUrl')
                break

    if pssh_elem is not None and license_url is not None:
        return pssh_elem.text.strip(), license_url
    else:
        raise ValueError("Could not find the correct ContentProtection element in the MPD content.")

# Licence challenge to obtain the decryption keys
def get_keys(pssh, lic_url, wvd_device_path):
    pssh_obj = PSSH(pssh)
    device = Device.load(wvd_device_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    try:
        challenge = cdm.get_license_challenge(session_id, pssh_obj)

        headers = {
             'Accept': '*/*',
        }

        licence_response = requests.post(lic_url, headers=headers, data=challenge)
        licence_response.raise_for_status()

        cdm.parse_license(session_id, licence_response.content)
        keys = [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
    except requests.exceptions.HTTPError as e:
        print(f"HTTPError: {e}")
        print(f"License response content: {licence_response.content}")
        raise
    except Exception as e:
        print(f"Unexpected error: {e}")
        raise
    finally:
        cdm.close(session_id)

    return keys

# Get the best video height from the MPD
def get_best_video_height(url_mpd):
    response = requests.get(url_mpd)
    response.raise_for_status()
    content = response.content
    if not content:
        raise ValueError("MPD content is empty or invalid")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"XML parsing error: {e}")
        print(f"MPD content: {content}")
        raise
    
    heights = []
    for adaptation_set in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}AdaptationSet"):
        for representation in adaptation_set.findall("{urn:mpeg:dash:schema:mpd:2011}Representation"):
            if 'height' in representation.attrib:
                heights.append(int(representation.attrib['height']))
    if heights:
        max_height = max(heights)
        if max_height >= 1080:
            return "1080p"
        elif max_height >= 720:
            return "720p"
        else:
            return "SD"
    return "720p"

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
    print(f"{'#':>3}  {'Type':<4} {'Resolution':<11} {'Bitrate':<16} {'Codec':<18} {'Lang':<6}")
    print(f"{'--':>3}  {'----':<4} {'----------':<11} {'----------------':<16} {'------------------':<18} {'------':<6}")
    for index, stream in enumerate(streams, start=1):
        print(
            f"{index:>3}  "
            f"{stream['type']:<4} "
            f"{stream['resolution']:<11} "
            f"{stream['bitrate']:<16} "
            f"{stream['codec']:<18} "
            f"{stream['lang']:<6}"
        )

def get_mpd_streams(url_mpd):
    response = requests.get(url_mpd)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    streams = []

    for adaptation_set in root.iter():
        if not adaptation_set.tag.endswith("AdaptationSet"):
            continue

        content_type = (adaptation_set.attrib.get("contentType") or "").lower()
        mime_type = (adaptation_set.attrib.get("mimeType") or "").lower()
        lang = adaptation_set.attrib.get("lang") or "-"

        for representation in adaptation_set:
            if not representation.tag.endswith("Representation"):
                continue

            rep_mime_type = (representation.attrib.get("mimeType") or "").lower()
            rep_content = f"{content_type} {mime_type} {rep_mime_type}"
            codecs = representation.attrib.get("codecs") or adaptation_set.attrib.get("codecs") or "unknown codecs"
            bandwidth = representation.attrib.get("bandwidth")
            bitrate = f"{int(bandwidth) // 1000} Kbps" if bandwidth and bandwidth.isdigit() else "unknown bitrate"
            width = representation.attrib.get("width")
            height = representation.attrib.get("height")

            if "video" in rep_content or width or height:
                stream_type = "Vid"
                resolution = f"{width or '?'}x{height or '?'}"
            elif "audio" in rep_content:
                stream_type = "Aud"
                resolution = "-"
            elif "text" in rep_content or "subtitle" in rep_content or codecs.lower() in {"stpp", "wvtt"}:
                stream_type = "Sub"
                resolution = "-"
            else:
                continue

            streams.append({
                "type": stream_type,
                "resolution": resolution,
                "bitrate": bitrate,
                "codec": codecs,
                "lang": lang,
            })

    return sorted(streams, key=stream_sort_key)

def parse_m3u8_attributes(value):
    attrs = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value):
        attrs[match.group(1)] = match.group(2).strip().strip('"')
    return attrs

def get_m3u8_streams(m3u8_url):
    response = requests.get(m3u8_url)
    response.raise_for_status()
    streams = []
    last_attrs = None

    for line in response.text.splitlines():
        value = line.strip()
        if value.startswith("#EXT-X-STREAM-INF:"):
            last_attrs = parse_m3u8_attributes(value.split(":", 1)[1])
            continue

        if not last_attrs or not value or value.startswith("#"):
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

def build_download_command(manifest_url, downloads_path, formatted_filename, keys=None, mode="auto"):
    selectors = "" if mode == "interactive" else "--select-video best --select-audio best --select-subtitle all "
    download_command = (
        f'N_m3u8DL-RE "{manifest_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_filename}"'
    )
    if keys:
        download_command += " --key " + " --key ".join(keys)
    return append_downloader_proxy(download_command)

def get_video_info_season_episode(video_info):
    season = (
        video_info.get("seasonNumber")
        or video_info.get("season")
        or video_info.get("series")
    )
    episode = (
        video_info.get("episode")
        or video_info.get("episodeNumber")
        or video_info.get("ep")
    )
    return season_episode_tag(season, episode)

# Format the filename based on the type of content
def get_formatted_filename(show_id, video_id, best_height, video_info=None, video_url=""):
    show_info = get_additional_video_info(show_id, video_id)
    show_title = show_info['showTitle']
    name = show_info['name']
    
    show_title = clean_title(show_title)
    
    # Handle different types of content
    if re.match(r'Season \d+ Ep \d+', name):
        season_episode = re.sub(r'Season (\d+) Ep (\d+)', lambda m: f"S{int(m.group(1)):02}E{int(m.group(2)):02}", name)
        return f"{show_title}.{season_episode}.{best_height}.ThreeNow.WEB-DL.AAC2.0.H.264"
    elif re.match(r'Season \d{4} Ep \d+', name):
        season_episode = re.sub(r'Season (\d{4}) Ep (\d+)', lambda m: f"S{int(m.group(1))}E{int(m.group(2)):02}", name)
        return f"{show_title}.{season_episode}.{best_height}.ThreeNow.WEB-DL.AAC2.0.H.264"
    elif re.match(r'\w+ \d+ \w+ \d{4}', name):
        date_str = datetime.strptime(name, '%A %d %B %Y').strftime('%Y%m%d')
        return f"{show_title}.{date_str}.{best_height}.ThreeNow.WEB-DL.AAC2.0.H.264"
    else:
        season_episode = None
        if video_info:
            season_episode = get_video_info_season_episode(video_info)
        season_episode = season_episode or get_url_season_episode(video_url)
        if season_episode:
            return f"{show_title}.{season_episode}.{best_height}.ThreeNow.WEB-DL.AAC2.0.H.264"
        return f"{show_title}.{best_height}.ThreeNow.WEB-DL.AAC2.0.H.264"

def clean_info_value(value):
    if value in (None, "", "Not Available"):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()

def format_info_date(value):
    if value in (None, "", "Not Available"):
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return f"{parsed.day} {parsed.strftime('%B %Y')}"
    except Exception:
        return str(value)

def print_info_metadata(video_info):
    if not video_info:
        return

    show_title = clean_info_value(video_info.get("showTitle"))
    episode_title = clean_info_value(video_info.get("title") or video_info.get("name"))
    date_aired = format_info_date(video_info.get("airedDate") or video_info.get("availableDate"))
    description = clean_info_value(video_info.get("synopsis") or video_info.get("description"))

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

# Print all the required information and download command
def get_download_command(video_url, downloads_path, wvd_device_path, mode="auto", auto_download=False):
    try:
        video_info = get_video_info(video_url)
        show_id = video_info['showId']
        video_id = video_info['videoId']
        
        playback_info = get_playback_info(video_info['externalMediaId'])
        manifest_url, lic_url = get_manifest_url(playback_info)
        
        if manifest_url.endswith("master.m3u8"):
            # Handling HLS playlist
            formatted_filename = get_formatted_filename(show_id, video_id, "720p", video_info, video_url)
            print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest_url}")

            if mode == "info":
                print_streams(get_m3u8_streams(manifest_url))
                print_info_metadata(video_info)
                print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_filename}.mkv")
                return

            download_command = build_download_command(manifest_url, downloads_path, formatted_filename, mode=mode)
            print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
            print(mask_proxy_command(download_command))

            user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
            if user_input == 'y':
                print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
                result = subprocess.run(download_command, shell=True)
                if result.returncode == 0:
                    print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
            else:
                print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
        else:
            # Handling DASH manifest
            try:
                pssh, lic_url = get_pssh_and_license(manifest_url)
                best_height = get_best_video_height(manifest_url)
                formatted_filename = get_formatted_filename(show_id, video_id, best_height, video_info, video_url)
                
                print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{manifest_url}")
                print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
                print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
                
                keys = get_keys(pssh, lic_url, wvd_device_path)
                for key in keys:
                    print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

                if mode == "info":
                    print_streams(get_mpd_streams(manifest_url))
                    print_info_metadata(video_info)
                    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_filename}.mkv")
                    return
                
                download_command = build_download_command(manifest_url, downloads_path, formatted_filename, keys, mode)
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                print(mask_proxy_command(download_command))

                user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
                if user_input == 'y':
                    print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
                    result = subprocess.run(download_command, shell=True)
                    if result.returncode == 0:
                        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
                else:
                    print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
            except ValueError as e:
                # Fallback to HLS if DASH content is not encrypted
                for source in playback_info['sources']:
                    if source.get('type') == 'application/x-mpegURL':
                        manifest_url = source['src']
                        break
                formatted_filename = get_formatted_filename(show_id, video_id, "720p", video_info, video_url)
                print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest_url}")

                if mode == "info":
                    print_streams(get_m3u8_streams(manifest_url))
                    print_info_metadata(video_info)
                    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_filename}.mkv")
                    return

                download_command = build_download_command(manifest_url, downloads_path, formatted_filename, mode=mode)
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                print(mask_proxy_command(download_command))

                user_input = "y" if auto_download else input("Do you wish to download? Y or N: ").strip().lower()
                if user_input == 'y':
                    print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
                    result = subprocess.run(download_command, shell=True)
                    if result.returncode == 0:
                        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
                else:
                    print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
    except Exception as e:
        if str(e) == "THREENOW_SERIES_URL_NEEDS_FLAG":
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} ThreeNow series URLs need a flag.{bcolors.ENDC}")
            print(f"{bcolors.YELLOW}{icons.ICON_INFO} Use -l to list episodes or -d with a selector to download from a series.{bcolors.ENDC}")
            return
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} {e}{bcolors.ENDC}")

def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def format_show_title(slug):
    show_title = unquote(slug or "").replace("-", " ").replace(":", ": ").strip().title()
    show_title = re.sub(r"\s+", " ", show_title)
    return re.sub(r"\bNz\b", "NZ", show_title)

def parse_show_url(series_url):
    series_url = unquote(series_url).split("?", 1)[0].rstrip("/")
    match = re.search(r"/shows/([^/]+)/([^/?#]+)$", series_url)
    if not match:
        raise ValueError("Could not determine ThreeNow show ID from the URL. Use a show URL with -l, for example https://www.threenow.co.nz/shows/blackshore/1728601222588")
    clean_series_url = re.sub(r"/[^/?#]+$", "", series_url)
    return match.group(1), match.group(2), clean_series_url

def fetch_show_catalogue(show_id):
    response = requests.get(f"https://now-api.fullscreen.nz/v5/shows/{show_id}", timeout=20)
    response.raise_for_status()
    return response.json()

def build_episode_url(clean_series_url, season_number, episode_number, show_id, video_id):
    return f"{clean_series_url}/season-{season_number}-ep-{episode_number}/{show_id}/{video_id}"

def collect_episode_details(series_url):
    show_slug, show_id, clean_series_url = parse_show_url(series_url)
    show_data = fetch_show_catalogue(show_id)
    show_title = show_data.get("name") or format_show_title(show_slug)
    episodes = []

    for season in show_data.get("seasons", []) or []:
        season_number = season.get("seasonNumber") or season.get("order") or season.get("name") or "1"
        season_label = f"Season {season_number}"
        for episode in season.get("episodes", []) or []:
            video_id = episode.get("videoId") or episode.get("externalMediaId")
            episode_number = episode.get("episode") or episode.get("episodeNumber")
            if not video_id or not episode_number:
                continue

            title = episode.get("name") or f"Season {season_number} Ep {episode_number}"
            video_url = build_episode_url(clean_series_url, season_number, episode_number, show_id, video_id)
            episodes.append({
                "Video URL": video_url,
                "Video ID": video_id,
                "Show Title": show_title,
                "Season": season_number,
                "Season Label": season_label,
                "Episode": episode_number,
                "Episode Label": str(episode_number),
                "Sort Season": parse_int(season_number) or 0,
                "Sort Episode": parse_int(episode_number) or 0,
                "Title": title,
                "Description": episode.get("synopsis") or "",
                "Date Aired": episode.get("airedDate") or episode.get("availableDate") or "",
                "Thumbnail": (episode.get("images") or {}).get("videoTile") or "",
            })

    episodes.sort(key=lambda item: (item.get("Sort Season") or 0, item.get("Sort Episode") or 0, item.get("Title") or ""))
    episode_data = {
        "Episode Summary": [
            f"{episode['Season Label']} Episode {episode['Episode Label']} - {episode['Title']}"
            for episode in episodes
        ],
        "Episode Details": episodes,
    }
    return show_slug, episode_data

def save_episode_list_json(show_slug, episode_data):
    os.makedirs(TEMP_DIR, exist_ok=True)
    output_path = os.path.join(TEMP_DIR, f"threenow_{safe_windows_filename(show_slug)}_episodes.json")
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(episode_data, file, ensure_ascii=False, indent=4)
    return output_path

def export_episode_list_text(show_slug, episodes):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(EXPORT_DIR, f"threenow_{safe_windows_filename(show_slug)}_export_{timestamp}.txt")

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
        print(f"{bcolors.WARNING}No playable ThreeNow episodes found.{bcolors.ENDC}")
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
    console.print(Rule(Text.assemble(("ThreeNow Series: ", f"bold {header_style}"), (series_title, "bold white")), style=header_style))
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
    series_title = episodes[0].get("Show Title") if episodes else format_show_title(show_slug)
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
        series_title = episodes[0].get("Show Title") if episodes else format_show_title(show_slug)
        raise LookupError(f"No ThreeNow episodes found for selector {normalized} in {series_title}.")

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

def download_selected_episodes(series_url, selector, downloads_path, wvd_device_path):
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
        main(episode["Video URL"], downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, auto_download=True)

def main(video_url, downloads_path, wvd_device_path, mode="auto", export_list=False, download_selector=None, auto_download=False):
    if mode == "list":
        list_show_episodes(video_url, export_list)
        return

    if mode == "download":
        download_selected_episodes(video_url, download_selector, downloads_path, wvd_device_path)
        return

    get_download_command(video_url, downloads_path, wvd_device_path, mode, auto_download)

