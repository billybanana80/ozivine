import requests
import re
import xml.etree.ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import subprocess
from datetime import datetime
from urllib.parse import urljoin
from services.proxy import append_downloader_proxy, mask_proxy_command

#   Ozivine: ThreeNow Video Downloader
#   Author: billybanana
#   Usage: enter the movie/series/season/episode URL to retrieve the MPD, Licence, PSSH and Decryption keys.
#   eg: TV Shows https://www.threenow.co.nz/shows/thirst-with-shay-mitchell/season-1-ep-1/1718148621037/M86965-766
#   Movies https://www.threenow.co.nz/shows/muru/muru/1692133295073/M75898-948
#   Sport https://www.threenow.co.nz/shows/crc---dunlop-super2-series/season-2023-ep-1/1704225434697/M82229-702
#   News & Current Affairs https://www.threenow.co.nz/shows/kia-ora%252C-good-evening/kia-ora%252C-good-evening/S4015-502/M62280-758
#   Authentication: None
#   Geo-Locking: requires a New Zealand IP address
#   Quality: up to 720p
#   Key Features:
#   1. Extract Video ID: Parses the ThreeNow URL to extract the series name, season, and episode number, and then fetches the video ID from the ThreeNow API.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for encrypted video files only.

# Brightcove constants
BRIGHTCOVE_KEY = 'BCpkADawqM2NDYVFYXV66rIDrq6i9YpFSTom-hlJ_pdoGkeWuItRDsn1Bhm7QVyQvFIF0OExqoywBvX5-aAFaxYHPlq9st-1mQ73ZONxFHTx0N7opvkHJYpbd_Hi1gJuPP5qCFxyxB8oevg-'
BRIGHTCOVE_ACCOUNT = '3812193411001'
BRIGHTCOVE_HEADERS = {
    "BCOV-POLICY": BRIGHTCOVE_KEY,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.threenow.co.nz",
    "Referer": "https://www.threenow.co.nz/"
}

# ANSI escape codes for colors
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
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    ORANGE = '\033[38;5;208m'

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

# Get the Brightcove Video ID from the video URL
def get_video_info(video_url):
    # Extract show_id and videoId from the URL
    match = re.search(r'shows/[^/]+/(?:[^/]+/)*([^/]+)/([^/]+)$', video_url)
    if not match:
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

# Print all the required information and download command
def get_download_command(video_url, downloads_path, wvd_device_path, mode="auto"):
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
                print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_filename}.mkv")
                return

            download_command = build_download_command(manifest_url, downloads_path, formatted_filename, mode=mode)
            print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
            print(mask_proxy_command(download_command))

            user_input = input("Do you wish to download? Y or N: ").strip().lower()
            if user_input == 'y':
                subprocess.run(download_command, shell=True)
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
                    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_filename}.mkv")
                    return
                
                download_command = build_download_command(manifest_url, downloads_path, formatted_filename, keys, mode)
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                print(mask_proxy_command(download_command))

                user_input = input("Do you wish to download? Y or N: ").strip().lower()
                if user_input == 'y':
                    subprocess.run(download_command, shell=True)
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
                    print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_filename}.mkv")
                    return

                download_command = build_download_command(manifest_url, downloads_path, formatted_filename, mode=mode)
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                print(mask_proxy_command(download_command))

                user_input = input("Do you wish to download? Y or N: ").strip().lower()
                if user_input == 'y':
                    subprocess.run(download_command, shell=True)
    except Exception as e:
        print(f"Error: {e}")

def main(video_url, downloads_path, wvd_device_path, mode="auto"):
    get_download_command(video_url, downloads_path, wvd_device_path, mode)

