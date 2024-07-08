import requests
import datetime as dt
import base64
import subprocess
import re

#   Ozivine: 10Play Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the m3u8 Manifest.
#   eg: https://10play.com.au/south-park/episodes/season-15/episode-6/tpv240705gpchj
#   Authentication: Login
#   Geo-Locking: requires an Australian IP address
#   Quality: up to 720p
#   Key Features:
#   1. Extract Video ID: Parses the 10Play video URL to extract the video id and then fetches the show/movie info from the 10Play API.
#   2. Print Download Information: Outputs the M3U8 URL required for downloading the video content.
#   3. Note: this script functions for non-encrypted video files only (10Play files are not currently encrypted).

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

# Function to extract video details and manifest URL
def extract_video_details(video_id, token):
    video_api_url = f'https://10play.com.au/api/v1/videos/{video_id}'
    auth_headers = headers.copy()
    auth_headers['Authorization'] = token

    response = requests.get(video_api_url, headers=auth_headers)
    if response.status_code == 200:
        video_data = response.json()
        if 'playbackApiEndpoint' in video_data:
            playback_url = video_data['playbackApiEndpoint']
            playback_response = requests.get(playback_url, headers=auth_headers)
            if playback_response.status_code == 200:
                playback_data = playback_response.json()
                if 'source' in playback_data:
                    return playback_data['source'], video_data
    return None, None

# Function to get the final redirected URL
def get_final_url(manifest_url):
    session = requests.Session()
    response = session.get(manifest_url, headers=headers, allow_redirects=True)
    return response.url

# Function to modify the manifest URL to get the 720p stream
def modify_manifest_url(manifest_url):
    return manifest_url.replace('-,150', '-,300,150')

# Function to format the filename based on video details
def format_file_name(video_data):
    show_name = video_data['tvShow'].replace(' ', '.')
    clip_title = video_data.get('clipTitle', '').replace(' ', '.')
    genre = video_data.get('genre', '').lower()
    season = int(video_data['season'])

    if genre == 'movies':
        formatted_file_name = f"{show_name}.720p.10Play.WEB-DL.AAC2.0.H.264"
    elif genre == 'sport':
        formatted_file_name = f"{clip_title}.S{season}.720p.10Play.WEB-DL.AAC2.0.H.264"
    else:
        episode = int(video_data['episode'])
        season_episode_tag = f"S{season:02d}E{episode:02d}"
        formatted_file_name = f"{show_name}.{season_episode_tag}.720p.10Play.WEB-DL.AAC2.0.H.264"
    
    return formatted_file_name

# Function to format and display download command
def display_download_command(manifest_url, formatted_file_name, downloads_path):
    download_command = f"""N_m3u8DL-RE "{manifest_url}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}" """
    
    print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest_url}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(download_command)
    
    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        subprocess.run(download_command, shell=True)

# Function to extract video ID from URL
def extract_video_id(url):
    match = re.search(r'/([^/]+)/?$', url)
    return match.group(1) if match else None

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
        manifest_url, video_data = extract_video_details(video_id, token)
        if manifest_url and video_data:
            final_url = get_final_url(manifest_url)
            if final_url:
                modified_manifest_url = modify_manifest_url(final_url)
                formatted_file_name = format_file_name(video_data)
                display_download_command(modified_manifest_url, formatted_file_name, downloads_path)
            else:
                print(f"{bcolors.FAIL}Failed to get final manifest URL{bcolors.ENDC}")
        else:
            print(f"{bcolors.FAIL}Failed to extract manifest URL{bcolors.ENDC}")
    else:
        print(f"{bcolors.FAIL}Login failed{bcolors.ENDC}")


