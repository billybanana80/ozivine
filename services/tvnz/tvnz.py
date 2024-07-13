import requests
import re
import json
import yaml
from xml.etree import ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
import base64
import binascii
import subprocess
import os
from time import time

#   Ozivine: TVNZ Video Downloader
#   Author: billybanana
#   Usage: enter the movie/series/season/episode URL to retrieve the MPD, Licence, PSSH and Decryption keys.
#   eg: TV Shows https://www.tvnz.co.nz/shows/boiling-point/episodes/s1-e1 or Movies https://www.tvnz.co.nz/shows/legally-blonde/movie/s1-e1 or Sport https://www.tvnz.co.nz/sport/football/uefa-euro/spain-v-france-semi-finals-highlights
#   Authentication: Login
#   Geo-Locking: requires a New Zealand IP address
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the TVNZ URL to extract the series name, season, and episode number, and then fetches the Brightcove or MediaKind video ID from the TVNZ API.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for both encrypted and non-encrypted video files (majority of TVZN content is encrypted).

# Load configuration from config.yaml
def load_config(config_path):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

BRIGHTCOVE_KEY = 'BCpkADawqM0IurzupiJKMb49WkxM__ngDMJ3GOQBhN2ri2Ci_lHwDWIpf4sLFc8bANMc-AVGfGR8GJNgxGqXsbjP1gHsK2Fpkoj6BSpwjrKBnv1D5l5iGPvVYCo'
BRIGHTCOVE_ACCOUNT = '963482467001'
BRIGHTCOVE_HEADERS = {
    "BCOV-POLICY": BRIGHTCOVE_KEY,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.tvnz.co.nz",
    "Referer": "https://www.tvnz.co.nz/"
}
BRIGHTCOVE_API = lambda video_id: f"https://edge.api.brightcove.com/playback/v1/accounts/{BRIGHTCOVE_ACCOUNT}/videos/{video_id}"
TOKEN_URL = 'https://login.tvnz.co.nz/v1/token'

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

class TVNZAPI:
    def __init__(self, config):
        self.session = requests.Session()
        self.token = None
        self.token_expires = 0
        self.config = config

    def _refresh_token(self):
        self.token = None
        self.token_expires = 0

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Origin": "https://www.tvnz.co.nz",
            "Referer": "https://www.tvnz.co.nz/",
        }
        response = self.session.get(TOKEN_URL, headers=headers)
        response.raise_for_status()
        self.token = response.text.strip()
        self.token_expires = time() + 3600  # Assuming token expires in 1 hour
        self.session.headers.update({'Authorization': f'Bearer {self.token}'})

    def login(self, email, password):
        login_url = "https://login.tvnz.co.nz/co/authenticate"
        payload = {
            "client_id": "tp5hyPrFuXLJV0jgRWy5l7lEtJlPN98R",
            "credential_type": "password",
            "password": password,
            "username": email,
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Origin": "https://www.tvnz.co.nz",
            "Referer": "https://www.tvnz.co.nz/",
            "auth0Client": "eyJuYW1lIjoiYXV0aDAuanMiLCJ2ZXJzaW9uIjoiOS4xMC4yIn0="
        }
        response = self.session.post(login_url, json=payload, headers=headers)
        response.raise_for_status()
        login_data = response.json()
        
        authorize_url = "https://login.tvnz.co.nz/authorize"
        params = {
            "client_id": "tp5hyPrFuXLJV0jgRWy5l7lEtJlPN98R",
            "response_type": "token",
            "redirect_uri": "https://www.tvnz.co.nz/login",
            "audience": "tvnz-apis",
            "state": base64.b64encode(os.urandom(24)).decode(),
            "response_mode": "web_message",
            "login_ticket": login_data["login_ticket"],
            "prompt": "none",
            "auth0Client": "eyJuYW1lIjoiYXV0aDAuanMiLCJ2ZXJzaW9uIjoiOS4xMC4yIn0="
        }
        
        response = self.session.get(authorize_url, params=params, headers=headers)
        response.raise_for_status()
        
        match = re.search(r'authorizationResponse = {type: "authorization_response",response: (.*?)};', response.text)
        if not match:
            raise ValueError("Authorization response not found.")
        
        auth_response = json.loads(match.group(1))
        if "error" in auth_response:
            raise ValueError(f"Authorization error: {auth_response['error_description']}")
        
        self.token = auth_response["access_token"]
        self.token_expires = time() + 3600  # Assuming token expires in 1 hour
        self.session.headers.update({'Authorization': f'Bearer {self.token}'})

        print(f"{bcolors.OKGREEN}Login successful, token obtained{bcolors.ENDC}")

    def get_video_id_from_url(self, video_url):
        if time() > self.token_expires:
            self._refresh_token()
            
        match = re.search(r'sport/([^/]+)/([^/]+)/([^/]+)', video_url)
        if match:
            category, subcategory, video_slug = match.groups()
            api_url = f"https://apis-public-prod.tech.tvnz.co.nz/api/v1/web/play/page/sport/{category}/{subcategory}/{video_slug}"
        else:
            match = re.search(r'shows/([^/]+)/(episodes|movie)/s(\d+)-e(\d+)', video_url)
            if not match:
                raise ValueError("Could not extract video information from the URL.")
            series_name, content_type, season, episode = match.groups()
            api_url = f"https://apis-public-prod.tech.tvnz.co.nz/api/v1/web/play/page/shows/{series_name}/{content_type}/s{season}-e{episode}"
        
        response = self.session.get(api_url)
        response.raise_for_status()
        data = response.json()
        
        if "sport" in video_url:
            video_id = self.find_video_id_in_sport(data)
        else:
            video_id = self.find_video_id_in_show(data, season, episode)
        
        if video_id:
            return video_id
        else:
            raise ValueError("Could not find the video ID in the API response.")

    def find_video_id_in_sport(self, data):
        if isinstance(data, dict):
            if data.get("media", {}).get("source") == "brightcove":
                return f"brightcove:{data['media']['id']}"
            if data.get("media", {}).get("source") == "mediakind":
                return f"mediakind:{data['media']['id']}"
            for key, value in data.items():
                result = self.find_video_id_in_sport(value)
                if result:
                    return result
        elif isinstance(data, list):
            for item in data:
                result = self.find_video_id_in_sport(item)
                if result:
                    return result
        return None

    def find_video_id_in_show(self, data, season, episode):
        def find_video_id(data, season, episode):
            if isinstance(data, dict):
                if data.get("seasonNumber") == season and data.get("episodeNumber") == episode:
                    return data.get("publisherMetadata", {}).get("brightcoveVideoId")
                for key, value in data.items():
                    result = find_video_id(value, season, episode)
                    if result:
                        return result
            elif isinstance(data, list):
                for item in data:
                    result = find_video_id(item, season, episode)
                    if result:
                        return result
            return None
        
        return find_video_id(data, season, episode)

    def get_pssh(self, url_mpd):
        response = self.session.get(url_mpd)
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
        return None

    def get_keys(self, pssh, lic_url, authorization_token=None):
        try:
            pssh = PSSH(pssh)
        except binascii.Error as e:
            print(f"Could not decode PSSH data as Base64: {e}")
            return []

        device = Device.load(self.config['wvd_device_path'])
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, pssh)
        
        headers = {
            'Content-Type': 'application/octet-stream',
            'Origin': 'https://www.tvnz.co.nz',
            'Referer': 'https://www.tvnz.co.nz/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }

        if authorization_token:
            headers['Authorization'] = f'Bearer {authorization_token}'

        licence = self.session.post(lic_url, headers=headers, data=challenge)
        
        try:
            licence.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"HTTPError: {e}")
            print(f"Response Headers: {licence.headers}")
            print(f"Response Text: {licence.text}")
            raise

        cdm.parse_license(session_id, licence.content)
        keys = [f"{key.kid.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id) if key.type == 'CONTENT']
        cdm.close(session_id)
        return keys

    def get_highest_resolution(self, url_mpd):
        response = self.session.get(url_mpd)
        root = ET.fromstring(response.content)
        adaptation_sets = root.findall(".//{urn:mpeg:dash:schema:mpd:2011}AdaptationSet")

        max_height = 0
        for adaptation in adaptation_sets:
            representations = adaptation.findall("{urn:mpeg:dash:schema:mpd:2011}Representation")
            for representation in representations:
                height = int(representation.get("height", 0))
                if height > max_height:
                    max_height = height
        
        if max_height >= 1080:
            return "1080p"
        elif max_height >= 720:
            return "720p"
        else:
            return "SD"

    def get_highest_resolution_mediakind(self, url_mpd):
        response = self.session.get(url_mpd)
        root = ET.fromstring(response.content)
        representation_sets = root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation")

        max_height = 0
        for representation in representation_sets:
            height = int(representation.get("height", 0))
            if height > max_height:
                max_height = height

        if max_height >= 1080:
            return "1080p"
        elif max_height >= 720:
            return "720p"
        else:
            return "SD"

    def get_secondary_authorization_token(self, video_id):
        token_url = f"https://apis-public-prod.tvnz.io/playback/v1/{video_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        response = self.session.get(token_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        encryption = data.get("encryption")
        if encryption and "drmToken" in encryption:
            return encryption["drmToken"]
        else:
            raise ValueError("Secondary token not found in response")

def handle_mediakind_sport_video(api, video_url):
    # print("Entered handle_mediakind_sport_video function") # debugging only
    match = re.search(r'sport/([^/]+)/([^/]+)/([^/]+)', video_url)
    if match:
        # print("Regex match successful") # debugging only
        category, subcategory, video_slug = match.groups()
        api_url = f"https://apis-public-prod.tech.tvnz.co.nz/api/v1/web/play/page/sport/{category}/{subcategory}/{video_slug}"
        # print(f"Constructed API URL: {api_url}") # debugging only

        response = api.session.get(api_url)
        response.raise_for_status()
        data = response.json()

        def find_mediakind_id(data):
            if isinstance(data, dict):
                if data.get("media", {}).get("source") == "mediakind":
                    return data["media"]["id"]
                for key, value in data.items():
                    result = find_mediakind_id(value)
                    if result:
                        return result
            elif isinstance(data, list):
                for item in data:
                    result = find_mediakind_id(item)
                    if result:
                        return result
            return None

        video_id = find_mediakind_id(data)
        if video_id:
            # print(f"Found Mediakind video ID: {video_id}") # debugging only
            mpd_url = f"https://replay.vod-tvnz-prod.tvnz.io/dash-enc/{video_id}/manifest.mpd"
            # print(f"Manifest URL: {mpd_url}") # debugging only
            pssh = api.get_pssh(mpd_url)
            # print(f"PSSH: {pssh}") # debugging only
            
            # Fetch secondary token
            secondary_token = api.get_secondary_authorization_token(video_id)
            # print(f"Secondary Authorization token: {secondary_token}") # debugging only

            lic_url = "https://apis-public-prod.tvnz.io/license/v1/wv"

            if pssh:
                keys = api.get_keys(pssh, lic_url, secondary_token)
                # print(f"Keys: {keys}") # debugging only
                # Print the requested information
                print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
                print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
                print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
                for key in keys:
                    print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
                resolution = api.get_highest_resolution_mediakind(mpd_url)
                formatted_file_name = f"{subcategory}.{video_slug}".replace("-", ".").title() + f".{resolution}.TVNZ.WEB-DL.AAC2.0.H.264"
                download_command = f"""N_m3u8DL-RE "{mpd_url}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-name "{formatted_file_name}" --key """ + ' --key '.join(keys)
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                print(download_command)

                user_input = input("Do you wish to download? Y or N: ").strip().lower()
                if user_input == 'y':
                    subprocess.run(download_command, shell=True)                
            else:
                print(f"{bcolors.FAIL}Failed to extract PSSH data{bcolors.ENDC}")
        else:
            print(f"{bcolors.FAIL}Failed to find Mediakind video ID{bcolors.ENDC}")
    else:
        print(f"{bcolors.FAIL}Regex match failed for the URL: {video_url}{bcolors.ENDC}")

def get_download_command(video_url, config):
    api = TVNZAPI(config)
    credentials = config['credentials']['tvnz'].split(":")
    email, password = credentials[0], credentials[1]
    api.login(email, password)
    video_id = api.get_video_id_from_url(video_url)
    if video_id and isinstance(video_id, str) and video_id.startswith("mediakind:"):
        handle_mediakind_sport_video(api, video_url)
        return
    else:
        video_id = video_id.split(":")[-1] if isinstance(video_id, str) else video_id
        session = requests.Session()  
        response = session.get(BRIGHTCOVE_API(video_id), headers=BRIGHTCOVE_HEADERS).json()
    
    download_command = None

    if "sport" in video_url:
        match = re.search(r'sport/([^/]+)/([^/]+)/([^/]+)', video_url)
        category, subcategory, title = match.groups()
        formatted_file_name = f"{subcategory}.{title}".replace("-", ".").title() + ".{resolution}.TVNZ.WEB-DL.AAC2.0.H.264"
    else:
        match = re.search(r'shows/([^/]+)/(episodes|movie)/s(\d+)-e(\d+)', video_url)
        if match:
            series_name, content_type, season, episode = match.groups()
            series_name = series_name.replace('-', ' ').title().replace(' ', '.')
            if content_type == 'episodes':
                formatted_file_name = f"{series_name}.S{int(season):02}E{int(episode):02}.{{resolution}}.TVNZ.WEB-DL.AAC2.0.H.264"
            else:
                formatted_file_name = f"{series_name}.{{resolution}}.TVNZ.WEB-DL.AAC2.0.H.264"
        else:
            raise ValueError("Invalid video URL format.")
    
    if 'sources' in response:
        sources = response['sources']
        source = next((src for src in sources if 'key_systems' in src and 'com.widevine.alpha' in src['key_systems']), None)
        if source:
            mpd_url = source['src']
            resolution = api.get_highest_resolution(mpd_url)
            formatted_file_name = formatted_file_name.format(resolution=resolution)
            lic_url = source['key_systems']['com.widevine.alpha']['license_url']
            pssh = api.get_pssh(mpd_url)
            if pssh:
                keys = api.get_keys(pssh, lic_url)
                # Print the requested information
                print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
                print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
                print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")
                for key in keys:
                    print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")
                download_command = f"""N_m3u8DL-RE "{mpd_url}" --select-video best --select-audio best --select-subtitle all -mt -M format=mkv --save-name "{formatted_file_name}" --key """ + ' --key '.join(keys)
                print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
                print(download_command)            
            else:
                print(f"{bcolors.FAIL}Failed to extract PSSH data{bcolors.ENDC}")
        else:
            print(f"{bcolors.FAIL}No Widevine-protected source found{bcolors.ENDC}")
    else:
        print(f"{bcolors.FAIL}No 'sources' found in the response{bcolors.ENDC}")
    
    if download_command:
        user_input = input("Do you wish to download? Y or N: ").strip().lower()
        if user_input == 'y':
            subprocess.run(download_command, shell=True)

def main(video_url, downloads_path, credentials, config):
    get_download_command(video_url, config)


