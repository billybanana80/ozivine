import requests
import datetime as dt
import base64
import subprocess
import re
import os

#   Ozivine: 10Play Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the m3u8 Manifest.
#   eg: https://10play.com.au/airport-24-7/episodes/season-1/episode-1/tpv250603pulhk
#   Authentication: Login
#   Geo-Locking: requires an Australian IP address
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the 10Play video URL to extract the video id and then fetches the show/movie info from the 10Play API.
#   2. Print Download Information: Outputs the M3U8 URL required for downloading the video content.
#   3. Note: this script functions for AES_128 encrypted video files only.

# Formatting for output
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
    YELLOW = '\033[93m'
    ORANGE = '\033[93m'

# URLs and Headers
login_url = 'https://10play.com.au/api/user/auth'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://10play.com.au',
    'Referer': 'https://10play.com.au/'
}

# Function to get bearer token
def get_bearer_token(username, password):
    timestamp = dt.datetime.now().strftime('%Y%m%d000000')
    auth_header = base64.b64encode(timestamp.encode('ascii')).decode('ascii')
    login_payload = {'email': username, 'password': password}
    login_headers = headers.copy()
    login_headers['X-Network-Ten-Auth'] = auth_header

    response = requests.post(login_url, json=login_payload, headers=login_headers)
    if response.status_code == 200:
        data = response.json()
        if 'jwt' in data:
            return 'Bearer ' + data['jwt']['accessToken']
    return None

# Function to extract video details and videoId
def extract_video_details(video_id, token):
    video_api_url = f'https://10play.com.au/api/v1/videos/{video_id}'
    auth_headers = headers.copy()
    auth_headers['Authorization'] = token

    response = requests.get(video_api_url, headers=auth_headers)
    if response.status_code == 200:
        video_data = response.json()
        if 'playbackApiEndpoint' in video_data:
            playback_url = video_data['playbackApiEndpoint'] + "?platform=web"
            playback_response = requests.get(playback_url, headers=auth_headers)
            if playback_response.status_code == 200:
                playback_data = playback_response.json()

                # Get the videoId (this is the key information we need now)
                if 'dai' in playback_data and 'videoId' in playback_data['dai']:
                    return playback_data['dai']['videoId'], video_data  # Also return video_data for filename formatting
                else:
                    print(f"{bcolors.FAIL}Missing videoId in playback data{bcolors.ENDC}")
            else:
                print(f"{bcolors.FAIL}Failed to fetch playback data{bcolors.ENDC}")
        else:
            print(f"{bcolors.FAIL}playbackApiEndpoint missing in video data{bcolors.ENDC}")
    else:
        print(f"{bcolors.FAIL}Failed to fetch video details{bcolors.ENDC}")
    return None, None

# Function to extract video ID from URL
def extract_video_id(url):
    match = re.search(r'/([^/]+)/?$', url)
    return match.group(1) if match else None

# Function to retrieve manifest URL using the new config endpoint with correct headers
def get_manifest(video_id):
    CONFIG = "https://vod.ten.com.au/config/androidapps-v2"
    config = requests.get(CONFIG).json()
    url = config["endpoints"]["videos"]["server"] + config["endpoints"]["videos"]["methods"]["getVideobyIDs"]
    url = url.replace("[ids]", video_id).replace("[state]", "AU") # Add video id and state (geolocation)
    
    # Use the correct user-agent to get the right manifest
    manifest_headers = {
        "User-Agent": "Mobile Safari/537.36 10play/6.2.2 UAP"
    }
    content = requests.get(url, headers=manifest_headers).json()
    if content and 'items' in content and content['items']:
        items = content['items'][0]
        hls_url = requests.head(items.get("HLSURL"), allow_redirects=True, headers=manifest_headers).url # Follow redirects to get real URL
        real_url = hls_url.replace(",150,", ",500,300,150,") # Replace bitrate for higher resolution
        return (real_url, manifest_headers)
    else:
        print(f"{bcolors.FAIL}No items found in the video response.{bcolors.ENDC}")
        return (None, None)

# Function to get the maximum video resolution from the manifest
def get_max_resolution(manifest_url, headers):
    try:
        response = requests.get(manifest_url, headers=headers)
        resolutions = re.findall(r'RESOLUTION=(\d+)x(\d+)', response.text)
        if not resolutions:
            print(f"{bcolors.WARNING}No resolutions found in manifest.{bcolors.ENDC}")
            return "unknown"
        max_res = max((int(h), int(w)) for w, h in resolutions)
        return f"{max_res[0]}p"
    except Exception as e:
        print(f"{bcolors.FAIL}Error fetching or parsing manifest: {e}{bcolors.ENDC}")
        return "unknown"

# Function to format the filename based on video details
def format_file_name(video_data, resolution):
    show_name = video_data['tvShow'].replace(' ', '.')
    clip_title = video_data.get('clipTitle', '').replace(' ', '.')
    genre = video_data.get('genre', '').lower()
    season = int(video_data['season'])

    if genre == 'movies':
        return f"{show_name}.{resolution}.10Play.WEB-DL.AAC2.0.H.264"
    elif genre == 'sport':
        return f"{clip_title}.S{season}.{resolution}.10Play.WEB-DL.AAC2.0.H.264"
    else:
        episode = int(video_data['episode'])
        season_episode_tag = f"S{season:02d}E{episode:02d}"
        return f"{show_name}.{season_episode_tag}.{resolution}.10Play.WEB-DL.AAC2.0.H.264"

# Function to format and display download command
def display_download_command(manifest_url, formatted_file_name, downloads_path):
    download_command = f'''N_m3u8DL-RE "{manifest_url}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}" '''
    print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest_url}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(download_command)

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        subprocess.run(download_command, shell=True)

# Main logic
def main(video_url, downloads_path, credentials):
    username, password = credentials.split(':')
    video_id = extract_video_id(video_url)

    if not video_id:
        print(f"{bcolors.FAIL}Invalid URL. Please enter a valid 10Play video URL.{bcolors.ENDC}")
        return

    token = get_bearer_token(username, password)
    if token:
        print(f"{bcolors.OKGREEN}Login successful, token obtained{bcolors.ENDC}")
        extracted_video_id, video_data = extract_video_details(video_id, token)

        if extracted_video_id:
            manifest_result = get_manifest(extracted_video_id)
            if manifest_result:
                manifest_url, manifest_headers = manifest_result
                if manifest_url:
                    resolution = get_max_resolution(manifest_url, manifest_headers)
                    formatted_file_name = format_file_name(video_data, resolution)
                    display_download_command(manifest_url, formatted_file_name, downloads_path)
                else:
                    print(f"{bcolors.FAIL}Failed to retrieve manifest URL{bcolors.ENDC}")
            else:
                print(f"{bcolors.FAIL}Failed to get manifest data{bcolors.ENDC}")
        else:
            print(f"{bcolors.FAIL}Failed to extract video ID{bcolors.ENDC}")
    else:
        print(f"{bcolors.FAIL}Login failed{bcolors.ENDC}")
