import sys
import importlib
import yaml

#   Ozivine: Downloader for Australian FTA services
#   Author: billybanana
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the respective video URL to extract the movie or series name, season, and episode number.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for both encrypted and non-encrypted video files.

# Define color formatting
class bcolors:
    LIGHTBLUE = '\033[94m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    ENDC = '\033[0m'
    ORANGE = '\033[38;5;208m'

def load_config():
    with open('config.yaml', 'r') as file:
        return yaml.safe_load(file)

def main():
    config = load_config()
    downloads_path = config.get('downloads_path')
    wvd_device_path = config.get('wvd_device_path')
    cookies_path = config.get('cookies_path')

    video_url = input(f"{bcolors.ORANGE}Enter the video URL: {bcolors.ENDC}")

    if video_url.startswith("https://www.9now.com.au"):
        service_module = "services.9now.9now"
        print(f"{bcolors.ORANGE}Ozivine..........initiating 9Now{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path)
    elif video_url.startswith("https://7plus.com.au"):
        service_module = "services.7plus.7plus"
        print(f"{bcolors.ORANGE}Ozivine..........initiating 7Plus{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path, cookies_path)
    elif video_url.startswith("https://www.sbs.com.au"):
        service_module = "services.sbs.sbs"
        print(f"{bcolors.ORANGE}Ozivine..........initiating SBS{bcolors.ENDC}")
        args = (video_url, downloads_path)
    elif video_url.startswith("https://iview.abc.net.au"):
        service_module = "services.abciview.abc"
        print(f"{bcolors.ORANGE}Ozivine..........initiating ABC iView{bcolors.ENDC}")
        args = (video_url, downloads_path, wvd_device_path)
    else:
        print(f"{bcolors.RED}Unsupported URL. Please enter a valid video URL from 9Now, 7Plus, SBS, or ABC iView.{bcolors.ENDC}")
        sys.exit(1)

    try:
        service = importlib.import_module(service_module)
        service.main(*args)
    except Exception as e:
        print(f"{bcolors.RED}Error importing or running the service module: {e}{bcolors.ENDC}")

if __name__ == "__main__":
    main()
