import requests
import re
import base64
import binascii
import subprocess
from xml.etree import ElementTree as ET
from pywidevine import Cdm, Device, PSSH

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

# Define color formatting
class bcolors:
    LIGHTBLUE = '\033[94m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    ENDC = '\033[0m'
    ORANGE = '\033[38;5;208m'

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
def get_mpd_url(video_id):
    api_url = f"https://api.iview.abc.net.au/v3/video/{video_id}"
    response = requests.get(api_url)
    if response.status_code == 200:
        data = response.json()
        if '_embedded' in data and 'playlist' in data['_embedded']:
            for playlist in data['_embedded']['playlist']:
                if 'streams' in playlist and 'mpegdash' in playlist['streams']:
                    mpegdash_streams = playlist['streams']['mpegdash']
                    for quality in ['1080', '720', 'sd']:
                        if quality in mpegdash_streams and video_id in mpegdash_streams[quality]:
                            return mpegdash_streams[quality].replace('720.mpd', '1080.mpd')
        return None
    else:
        return None

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

# Main execution flow
def main(video_url, downloads_path, wvd_device_path):
    client_id = "1d4b5cba-42d2-403e-80e7-34565cdf772d"
    jwt_url = "https://api.iview.abc.net.au/v3/token/jwt"
    drm_url = "https://api.iview.abc.net.au/v3/token/drm/{video_id}"

    video_id = get_video_id(video_url)
    if video_id:
        mpd_url = get_mpd_url(video_id)
        if mpd_url:
            pssh = extract_pssh(mpd_url)
            if pssh:
                license_keys = get_license(pssh, video_id, client_id, jwt_url, drm_url, wvd_device_path)
                if license_keys:
                    formatted_keys = format_keys(license_keys)
                    # Get formatted file name
                    formatted_file_name = get_show_info(video_id)
                    
                    # Print the requested information
                    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
                    print(f"{bcolors.RED}License URL: {bcolors.ENDC}https://wv-keyos.licensekeyserver.com/")
                    print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
                    for key in formatted_keys:
                        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
                    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                    download_command = f"""N_m3u8DL-RE "{mpd_url}" --select-video res=1080 --select-audio all --select-subtitle all -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}" --key """ + ' --key '.join(formatted_keys)
                    print(download_command)
                    
                    if download_command:
                        user_input = input("Do you wish to download? Y or N: ").strip().lower()
                        if user_input == 'y':
                            subprocess.run(download_command, shell=True)
                else:
                    print("Failed to get license keys")
            else:
                print("Failed to extract PSSH")
        else:
            print("Failed to get MPD URL")
    else:
        print("Invalid URL")
