import requests
import re
import xml.etree.ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import subprocess
from datetime import datetime

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

# Get the Brightcove Video ID from the video URL
def get_video_info(video_url):
    # Extract show_id and videoId from the URL
    match = re.search(r'shows/[^/]+/(?:[^/]+/)*([^/]+)/([^/]+)$', video_url)
    if not match:
        raise ValueError("Could not extract show_id and videoId from the URL.\n Enter the full video URL please:\n eg: https://www.threenow.co.nz/shows/thirst-with-shay-mitchell/season-1-ep-1/1718148621037/M86965-766")
    
    show_id, video_id = match.groups()
    
    api_url = f"https://now-api.fullscreen.nz/v5/shows/{show_id}"
    
    response = requests.get(api_url)
    response.raise_for_status()
    data = response.json()
    
    # Check if genres indicate a movie or current affairs
    if "movie" in data.get("genres", []) or "current-affairs" in data.get("genres", []):
        # Use easyWatch section if it exists
        if "easyWatch" in data and "externalMediaId" in data["easyWatch"] and data["easyWatch"]["videoId"] == video_id:
            return data["easyWatch"]
        # Otherwise, use the episodes section
        for episode in data.get("episodes", []):
            if episode.get("videoId") == video_id:
                return episode
        # Additionally, check within seasons for current affairs
        for season in data.get("seasons", []):
            for episode in season.get("episodes", []):
                if episode.get("videoId") == video_id:
                    return episode
    else:
        for season in data.get("seasons", []):
            for episode in season.get("episodes", []):
                if episode.get("videoId") == video_id:
                    return episode
    
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
    response = requests.get(url, headers=BRIGHTCOVE_HEADERS)
    response.raise_for_status()
    return response.json()

# Get the manifest MPD 
def get_mpd_url(playback_info):
    for source in playback_info['sources']:
        if source.get('type') == 'application/dash+xml' and 'playready' not in source['src']:
            return source['src'], source['key_systems']['com.widevine.alpha']['license_url']
    raise Exception("MPD URL not found in playback info")

# Extract the PSSH and Licence from the MPD
def get_pssh_and_license(url_mpd):
    response = requests.get(url_mpd)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    # Extract the correct ContentProtection element
    for elem in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}ContentProtection"):
        if elem.attrib.get('schemeIdUri') == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed":
            pssh = elem.find("{urn:mpeg:cenc:2013}pssh")
            if pssh is not None:
                license_url = elem.attrib['{urn:brightcove:2015}licenseAcquisitionUrl']
                return pssh.text.strip(), license_url

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
    root = ET.fromstring(response.content)
    
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
    return "720p"  # Default to 720p if no height found

# Format the filename based on the type of content
def get_formatted_filename(show_id, video_id, best_height):
    show_info = get_additional_video_info(show_id, video_id)
    show_title = show_info['showTitle']
    name = show_info['name']
    
    # Remove spaces, commas, and dashes, and replace with dots
    show_title = re.sub(r'[ ,\-]', '.', show_title)
    
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
        return f"{show_title}.{best_height}.ThreeNow.WEB-DL.AAC2.0.H.264"

# Print all the required information and download command
def get_download_command(video_url, downloads_path, wvd_device_path):
    video_info = get_video_info(video_url)
    show_id = video_info['showId']
    video_id = video_info['videoId']
    
    playback_info = get_playback_info(video_info['externalMediaId'])
    mpd_url, lic_url = get_mpd_url(playback_info)
    pssh, lic_url = get_pssh_and_license(mpd_url)
    
    best_height = get_best_video_height(mpd_url)
    
    formatted_filename = get_formatted_filename(show_id, video_id, best_height)
    
    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
    print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
    print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
    
    keys = get_keys(pssh, lic_url, wvd_device_path)
    for key in keys:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
    
    download_command = f"""N_m3u8DL-RE "{mpd_url}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_filename}" --key """ + ' --key '.join(keys)
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
    print(download_command)

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        subprocess.run(download_command, shell=True)

def main(video_url, downloads_path, wvd_device_path):
    get_download_command(video_url, downloads_path, wvd_device_path)

