import requests
import re
import subprocess

#   Ozivine: SBS On Demand Video Downloader
#   Author: billybanana
#   Usage: enter the series/season/episode URL to retrieve the m3u8 Manifest.
#   eg: https://www.sbs.com.au/ondemand/watch/2336216643518    or https://www.sbs.com.au/ondemand/tv-series/the-responder/season-2/the-responder-s2-ep2/2336216643518
#   Authentication: None
#   Geo-Locking: requires an Australian IP address
#   Quality: up to 720p
#   Key Features:
#   1. Extract Video ID: Parses the SBS video URL to extract the video id and then fetches the show/movie info from the SBS API.
#   2. Print Download Information: Outputs the M3U8 URL required for downloading the video content.
#   3. Note: this script functions for non-encrypted video files only (SBS files are not currently encrypted).

# ANSI escape codes for colors
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

# Function to extract video ID from URL
def extract_video_id(video_url):
    match = re.search(r'/(\d+)', video_url)
    if match:
        return match.group(1)
    return None

# Function to get manifest URL from video metadata
def get_manifest_url(video_id, headers):
    # First API call to get SMIL file information
    smil_api_url = f"https://www.sbs.com.au/api/v3/video_smil?id={video_id}"
    smil_response = requests.get(smil_api_url, headers=headers)
    
    if smil_response.status_code != 200:
        print(f"Failed to fetch SMIL data, status code: {smil_response.status_code}")
        return None
    
    smil_data = smil_response.text
    m3u8_url_match = re.search(r'https://[^"]+\.m3u8[^"]*', smil_data)
    
    if m3u8_url_match:
        return m3u8_url_match.group(0)
    
    print("No m3u8 URL found in the SMIL data.")
    return None

# Function to get show information from the API
def get_show_info(video_id, headers):
    show_api_url = f"https://www.sbs.com.au/api/v3/video_stream?id={video_id}&context=tv"
    response = requests.get(show_api_url, headers=headers)
    
    if response.status_code != 200:
        print(f"Failed to fetch show info, status code: {response.status_code}")
        return None
    
    return response.json()

# Function to extract and print m3u8 URL
def extract_info(video_url):
    headers = {
        'accept': '*/*',
    }
    
    video_id = extract_video_id(video_url)
    if not video_id:
        print("Failed to extract video ID from the URL.")
        return None
    
    manifest_url = get_manifest_url(video_id, headers)
    show_info = get_show_info(video_id, headers)
    
    if manifest_url and show_info:
        entity_type = show_info['video_object']['externalRelations']['sbsondemand']['entity_type']
        if entity_type == 'MOVIE':
            movie_name = show_info['video_object']['name'].replace(' ', '.')
            formatted_file_name = f"{movie_name}.720p.SBS.WEB-DL.AAC2.0.H.264"
        else:
            series_name = show_info['video_object'].get('partOfSeries', {}).get('name', 'Unknown Series')
            video_url = show_info.get('oztamAnalyticsData', {}).get('videoUrl', '')
            if not video_url:
                video_url = show_info['video_object'].get('sbsOnDemandUrl', '')
            season_episode = re.search(r's(\d+)-ep(\d+)', video_url, re.IGNORECASE)

            if season_episode:
                season_number = season_episode.group(1).zfill(2)
                episode_number = season_episode.group(2).zfill(2)
            else:
                season_number = str(show_info['video_object'].get('partOfSeason', {}).get('seasonNumber', 0)).zfill(2)
                episode_number = str(show_info['video_object'].get('episodeNumber', 0)).zfill(2)

            season_episode_tag = f"S{season_number}E{episode_number}"
            series_name = series_name.replace(' ', '.')
            formatted_file_name = f"{series_name}.{season_episode_tag}.720p.SBS.WEB-DL.AAC2.0.H.264"
        
        return manifest_url, formatted_file_name
    return None, None

# Function to format and display download command
def display_download_command(manifest_url, formatted_file_name, downloads_path):
    download_command = f"""N_m3u8DL-RE "{manifest_url}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-dir "{downloads_path}" --save-name "{formatted_file_name}" """
    
    print(f"{bcolors.LIGHTBLUE}M3U8 URL: {bcolors.ENDC}{manifest_url}")
    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND: {bcolors.ENDC}")
    print(download_command)
    
    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == 'y':
        subprocess.run(download_command, shell=True)
    
# Main function
def main(video_url, downloads_path):
    manifest_url, formatted_file_name = extract_info(video_url)
    if not manifest_url:
        return
    
    display_download_command(manifest_url, formatted_file_name, downloads_path)
