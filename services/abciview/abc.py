import requests
import re
import base64
import binascii
import subprocess
import os
from xml.etree import ElementTree as ET
from pywidevine import Cdm, Device, PSSH
from colors import bcolors
import icons
from services.proxy import append_downloader_proxy, mask_proxy_command

#   Ozivine: ABC iView Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the MPD, Licence, PSSH and Decryption keys.
#   eg: https://iview.abc.net.au/video/LE2427H007S00
#   Authentication: None
#   Geo-Locking: requires an Australian IP address
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the ABC iView URL to extract the series name, season, and episode number.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for encrypted video files only (ABC iView files are all currently encrypted).

def get_video_id(url):
    match = re.search(r'video/([A-Z0-9]+)', url)
    return match.group(1) if match else None

def get_jwt_token(client_id, jwt_url):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    response = requests.post(jwt_url, data={"clientId": client_id}, headers=headers)
    if response.status_code == 200:
        return response.json().get("token")
    else:
        return None

def get_license_data(video_id, drm_url, jwt_token):
    headers = {
        'Authorization': f"Bearer {jwt_token}"
    }
    response = requests.get(drm_url.format(video_id=video_id), headers=headers)
    if response.status_code == 200:
        data = response.json()
        if data["status"] == "ok":
            custom_data = data["license"]
            license_url = "https://wv-keyos.licensekeyserver.com/"
            return license_url, custom_data
        else:
            return None, None
    else:
        return None, None

# Function to get 1080p MPD URL
def get_video_data(video_id):
    api_url = f"https://api.iview.abc.net.au/v3/video/{video_id}"
    response = requests.get(api_url)
    if response.status_code == 200:
        return response.json()
    return None

def get_mpd_candidates(video_id):
    data = get_video_data(video_id)
    candidates = []
    seen_urls = set()

    if data and '_embedded' in data and 'playlist' in data['_embedded']:
        for playlist in data['_embedded']['playlist']:
            if 'streams' not in playlist or 'mpegdash' not in playlist['streams']:
                continue

            mpegdash_streams = playlist['streams']['mpegdash']
            source_url = None
            for quality in ['1080', '720', 'sd']:
                if quality in mpegdash_streams and video_id in mpegdash_streams[quality]:
                    source_url = mpegdash_streams[quality]
                    break

            if not source_url:
                continue

            upgraded_url = source_url.replace('720.mpd', '1080.mpd')
            for label, url in [('1080', upgraded_url), ('source', source_url)]:
                if url not in seen_urls:
                    candidates.append({'label': label, 'url': url})
                    seen_urls.add(url)

            break

    return candidates

def get_mpd_url(video_id):
    candidates = get_mpd_candidates(video_id)
    return candidates[0]['url'] if candidates else None

def get_mpd_streams(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code != 200:
        return []

    root = ET.fromstring(response.content)
    streams = []
    for adaptation_set in root.iter():
        if not adaptation_set.tag.endswith('AdaptationSet'):
            continue

        content_type = adaptation_set.attrib.get('contentType', '').lower()
        mime_type = adaptation_set.attrib.get('mimeType', '').lower()
        lang = adaptation_set.attrib.get('lang', '-')

        for representation in adaptation_set:
            if not representation.tag.endswith('Representation'):
                continue

            rep_mime_type = representation.attrib.get('mimeType', '').lower()
            rep_content = f"{content_type} {mime_type} {rep_mime_type}"
            codecs = representation.attrib.get('codecs') or adaptation_set.attrib.get('codecs') or 'unknown codecs'
            bandwidth = representation.attrib.get('bandwidth')
            bitrate = f"{int(bandwidth) // 1000} Kbps" if bandwidth and bandwidth.isdigit() else "unknown bitrate"
            width = representation.attrib.get('width')
            height = representation.attrib.get('height')

            if 'video' in rep_content or width or height:
                stream_type = 'Vid'
                resolution = f"{width or '?'}x{height or '?'}"
            elif 'audio' in rep_content:
                stream_type = 'Aud'
                resolution = '-'
            elif 'text' in rep_content or 'subtitle' in rep_content or codecs.lower() in {'stpp', 'wvtt'}:
                stream_type = 'Sub'
                resolution = '-'
            else:
                continue

            streams.append({
                'type': stream_type,
                'resolution': resolution,
                'bitrate': bitrate,
                'codec': codecs,
                'lang': lang or '-'
            })

    return streams

def get_available_streams(candidates):
    streams = []
    seen = set()
    for candidate in candidates:
        for stream in get_mpd_streams(candidate['url']):
            stream_key = (
                stream['type'],
                stream['resolution'],
                stream['bitrate'],
                stream['codec'],
                stream['lang']
            )
            if stream_key not in seen:
                streams.append(stream)
                seen.add(stream_key)
    return sorted(streams, key=stream_sort_key)

def stream_sort_key(stream):
    type_order = {'Vid': 0, 'Aud': 1, 'Sub': 2}
    height = 0
    bitrate = 0

    resolution_match = re.search(r'x(\d+)', stream['resolution'])
    if resolution_match:
        height = int(resolution_match.group(1))

    bitrate_match = re.search(r'(\d+)', stream['bitrate'])
    if bitrate_match:
        bitrate = int(bitrate_match.group(1))

    return (type_order.get(stream['type'], 9), -height, -bitrate, stream['codec'], stream['lang'])

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

def collect_subtitles(video_id):
    data = get_video_data(video_id)
    playlist = (data or {}).get("_embedded", {}).get("playlist", [])
    program = next((item for item in playlist if item.get("type") == "program"), None)
    if not program:
        return []

    captions = program.get("captions") or {}
    url = captions.get("src-vtt")
    if not url or str(captions.get("live", "0")) == "1":
        return []

    return [{
        "url": url,
        "language": "en",
        "name": "English",
        "kind": "captions",
        "extension": "srt",
    }]

def print_external_subtitles(subtitles):
    if not subtitles:
        return

    print(f"\n{bcolors.YELLOW}External subtitles:{bcolors.ENDC}")
    print(f"{'#':>3}  {'Lang':<6} {'Kind':<10} {'Format':<7} {'Name':<20}")
    print(f"{'--':>3}  {'------':<6} {'----------':<10} {'-------':<7} {'--------------------':<20}")
    for index, subtitle in enumerate(subtitles, start=1):
        print(
            f"{index:>3}  "
            f"{subtitle.get('language', '-'):<6} "
            f"{subtitle.get('kind', '-'):<10} "
            f"{subtitle.get('extension', '-'):<7} "
            f"{subtitle.get('name', '-'):<20}"
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
    for index, subtitle in enumerate(subtitles, start=1):
        print(f"{bcolors.OKCYAN}{icons.ICON_WAITING} Processing subtitle:{bcolors.ENDC} {subtitle.get('language', 'und')} {subtitle.get('name', 'Subtitle')}")
        try:
            response = requests.get(subtitle["url"], timeout=20)
            response.raise_for_status()
            content = vtt_to_srt(response.text)
        except Exception:
            content = None

        if not content:
            print(f"{bcolors.WARNING}Subtitle skipped: no usable cues found{bcolors.ENDC}")
            continue

        filename = subtitle_filename(formatted_file_name, subtitle, index, used_names)
        path = os.path.join(downloads_path, filename)
        with open(path, "w", encoding="utf-8-sig", newline="") as file:
            file.write(content)
        print(f"{bcolors.GREEN}{icons.ICON_SUCCESS} Subtitle saved:{bcolors.ENDC} {path}")

# Function to get PSSH from MPD URL
def extract_pssh(mpd_url):
    response = requests.get(mpd_url)
    if response.status_code == 200:
        mpd_content = response.content
        root = ET.fromstring(mpd_content)
        for elem in root.iter():
            if 'ContentProtection' in elem.tag and 'urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed' in elem.attrib.values():
                pssh = elem.find('{urn:mpeg:cenc:2013}pssh').text
                return pssh
    return None

# Function to get the show information for file name formatting
def get_show_info(video_id):
    show_info_url = f'https://api.iview.abc.net.au/v3/video/{video_id}'
    response = requests.get(show_info_url)
    if response.status_code == 200:
        data = response.json()
        show_title = data.get("showTitle", "UnknownShow").replace(" ", ".")
        title = data.get("title", "")
        status_title = data.get("status", {}).get("title", "")
        
        if status_title == "MOVIE":
            formatted_title = f"{show_title}.1080p.ABCiView.WEB-DL.AAC2.0.H.264"
        else:
            match = re.search(r'Series (\d+) Episode (\d+)', title)
            if match:
                season = match.group(1).zfill(2)
                episode = match.group(2).zfill(2)
                formatted_title = f"{show_title}.S{season}E{episode}.1080p.ABCiView.WEB-DL.AAC2.0.H.264"
            else:
                formatted_title = f"{show_title}.{title.replace(' ', '.')}.1080p.ABCiView.WEB-DL.AAC2.0.H.264"
        return formatted_title
    return "video"

# Function to get keys using PSSH and license URL
def get_license(pssh, video_id, client_id, jwt_url, drm_url, wvd_device_path):
    jwt_token = get_jwt_token(client_id, jwt_url)
    if not jwt_token:
        return None

    license_url, custom_data = get_license_data(video_id, drm_url, jwt_token)
    if not license_url:
        return None

    # Headers for the license request
    headers = {
        'Accept': '*/*',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Content-Length': str(len(pssh)),
        'Host': 'wv-keyos.licensekeyserver.com',
        'Origin': 'https://iview.abc.net.au',
        'Referer': f'https://iview.abc.net.au/video/{video_id}',
        'sec-ch-ua': '"Not/A)Brand";v="8", "Chromium";v="126", "Microsoft Edge";v="126"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'cross-site',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
        'customdata': custom_data
    }

    # Make the license request
    device = Device.load(wvd_device_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    challenge = cdm.get_license_challenge(session_id, PSSH(pssh))

    response = requests.post(license_url, headers=headers, data=challenge)
    # Parse the license response
    if response.status_code == 200:
        cdm.parse_license(session_id, response.content)
        keys = cdm.get_keys(session_id)
        return keys
    else:
        return None

def format_keys(keys):
    formatted_keys = []
    for key in keys:
        formatted_keys.append(f"{key.kid.hex}:{key.key.hex()}")
    return formatted_keys

def build_download_command(mpd_url, downloads_path, formatted_file_name, formatted_keys, mode):
    selectors = "" if mode == "interactive" else "--select-video res=1080 --select-audio all --select-subtitle all "
    keys = " --key " + " --key ".join(formatted_keys)
    download_command = (
        f'N_m3u8DL-RE "{mpd_url}" '
        f'{selectors}'
        f'-mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}"'
        f'{keys}'
    )
    return append_downloader_proxy(download_command)

# Main execution flow
def main(video_url, downloads_path, wvd_device_path, mode="auto"):
    client_id = "1d4b5cba-42d2-403e-80e7-34565cdf772d"
    jwt_url = "https://api.iview.abc.net.au/v3/token/jwt"
    drm_url = "https://api.iview.abc.net.au/v3/token/drm/{video_id}"

    video_id = get_video_id(video_url)
    if video_id:
        candidates = get_mpd_candidates(video_id)
        mpd_url = candidates[0]['url'] if candidates else None
        if mpd_url:
            pssh = extract_pssh(mpd_url)
            if pssh:
                license_keys = get_license(pssh, video_id, client_id, jwt_url, drm_url, wvd_device_path)
                if license_keys:
                    formatted_keys = format_keys(license_keys)
                    # Get formatted file name
                    formatted_file_name = get_show_info(video_id)
                    subtitles = collect_subtitles(video_id)
                    
                    # Print the requested information
                    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
                    print(f"{bcolors.RED}License URL: {bcolors.ENDC}https://wv-keyos.licensekeyserver.com/")
                    print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
                    for key in formatted_keys:
                        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

                    if mode == "info":
                        print_streams(get_available_streams(candidates))
                        print_external_subtitles(subtitles)
                        print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")
                        return

                    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                    download_command = build_download_command(mpd_url, downloads_path, formatted_file_name, formatted_keys, mode)
                    print(mask_proxy_command(download_command))
                    print_external_subtitles(subtitles)
                    
                    if download_command:
                        user_input = input("Do you wish to download? Y or N: ").strip().lower()
                        if user_input == 'y':
                            print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
                            result = subprocess.run(download_command, shell=True)
                            if result.returncode == 0:
                                save_external_subtitles(subtitles, downloads_path, formatted_file_name)
                                print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
                        else:
                            print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
                else:
                    print("Failed to get license keys")
            else:
                print("Failed to extract PSSH")
        else:
            print("Failed to get MPD URL")
    else:
        print("Invalid URL")
