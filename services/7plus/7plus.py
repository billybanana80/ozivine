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

def extract_info(video_url, cookies_path):
    session = requests.Session()

    cookies = MozillaCookieJar(cookies_path)
    cookies.load(ignore_discard=True, ignore_expires=True)
    session.cookies = cookies

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "*/*",
        "Referer": "https://7plus.com.au/",
        "Origin": "https://7plus.com.au",
    }

    response = session.get(video_url, headers=headers)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to load the video page, status code: {response.status_code}{bcolors.ENDC}")
        return None

    api_key = None
    login_token = None
    for cookie in cookies:
        if cookie.name.startswith('glt_'):
            api_key = cookie.name[4:]
            login_token = cookie.value
            break

    if not api_key:
        print(f"{bcolors.FAIL}Failed to find API key in cookies{bcolors.ENDC}")
        return None

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
    login_resp = session.get(login_url, params=login_params, headers=headers).json()
    id_token = login_resp.get('id_token')

    if not id_token:
        print(f"{bcolors.FAIL}Failed to retrieve ID token from login response{bcolors.ENDC}")
        return None

    auth_url = 'https://7plus.com.au/auth/token'
    auth_data = json.dumps({
        'idToken': id_token,
        'platformId': 'web',
        'regSource': '7plus',
    }).encode()
    auth_resp = session.post(auth_url, headers={'Content-Type': 'application/json'}, data=auth_data).json()
    auth_token = auth_resp.get('token')

    if not auth_token:
        print(f"{bcolors.FAIL}Failed to retrieve auth token{bcolors.ENDC}")
        return None

    path, episode_id = re.search(r'https?://(?:www\.)?7plus\.com\.au/(?P<path>[^?]+\?.*?\bepisode-id=(?P<id>[^&#]+))', video_url).groups()

    media_url = 'https://videoservice.swm.digital/playback'
    media_params = {
        'appId': '7plus',
        'deviceType': 'web',
        'platformType': 'web',
        'accountId': 5303576322001,
        'referenceId': 'ref:' + episode_id,
        'deliveryId': 'csai',
        'videoType': 'vod',
    }
    headers['Authorization'] = f'Bearer {auth_token}'
    media_resp = session.get(media_url, params=media_params, headers=headers).json()
    
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

def get_download_command(info, show_title, season_episode_tag, downloads_path, wvd_device_path):
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
            download_command = f"""N_m3u8DL-RE "{url}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}" """
            if keys:
                download_command += " --key " + " --key ".join(keys)
        
        print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{url}")
        print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
        print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
        for key in keys:
            print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
        print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
        print(download_command)
        
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
            download_command = f"""N_m3u8DL-RE "{url}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}" """
            
            print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{url}")
            print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
            print(download_command)
            
            user_input = input("Do you wish to download? Y or N: ").strip().lower()
            if user_input == 'y':
                subprocess.run(download_command, shell=True)
        else:
            print(f"{bcolors.FAIL}Failed to retrieve necessary information for download{bcolors.ENDC}")       

def main(video_url, downloads_path, wvd_device_path, cookies_path):
    info = extract_info(video_url, cookies_path)
    if not info:
        return

    path, episode_id = re.search(r'https?://(?:www\.)?7plus\.com\.au/(?P<path>[^?]+\?.*?\bepisode-id=(?P<id>[^&#]+))', video_url).groups()
    show_name = path.split('?')[0]
    show_api_url = f"https://component-cdn.swm.digital/content/{show_name}?episode-id={episode_id}&platform-id=web&market-id=29&platform-version=1.0.95129&api-version=4.9&signedup=true"
    
    show_response = requests.get(show_api_url).json()
    show_title = show_response['title'].replace(" ", ".")
    alt_tag = show_response['pageMetaData']['objectGraphImage']['altTag']
    
    season_episode_tag = ""
    season_episode_match = re.search(r'Season (\d+) Episode (\d+)', alt_tag)
    if season_episode_match:
        season, episode = season_episode_match.groups()
        season_episode_tag = f"S{season.zfill(2)}E{episode.zfill(2)}"
    
    get_download_command(info, show_title, season_episode_tag, downloads_path, wvd_device_path)
