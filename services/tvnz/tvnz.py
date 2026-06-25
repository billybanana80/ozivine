import base64
import binascii
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
import jwt
import requests
import urllib3
import yaml

from services.proxy import append_downloader_proxy, mask_proxy_command
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

#   Ozivine: TVNZ Video Downloader
#   Author: billybanana
#   Usage: enter the movie/series/season/episode URL to retrieve the MPD, Licence, PSSH and Decryption keys.
#   eg: TV Shows https://www.tvnz.co.nz/player/tvepisode/tauranga-hilltop or Movies https://www.tvnz.co.nz/player/movie/the-creator or Sport https://www.tvnz.co.nz/player/event/spb-race-2-ssp-race-2-sbk-race-2
#   Authentication: Tokens
#   Geo-Locking: requires a New Zealand IP address
#   Quality: up to 1080p
#   Key Features:
#   1. Extract Video ID: Parses the TVNZ URL to extract the series name, season, and episode number, and then fetches the metadata from the TVNZ API.
#   2. Extract PSSH: Retrieves and parses the MPD file to extract the PSSH data necessary for Widevine decryption.
#   3. Fetch Decryption Keys: Uses the PSSH and license URL to request and retrieve the Widevine decryption keys.
#   4. Print Download Information: Outputs the MPD URL, license URL, PSSH, and decryption keys required for downloading and decrypting the video content.
#   5. Note: this script functions for both encrypted and non-encrypted video files (majority of TVZN content is encrypted).
#   6. Note: TVNZ no longer uses a username/password or cookies to authenticate your account. They are now using your browser's local storage, so they need to be extracted once before being cached for future use.
#   It is recommneded to have a separate user account for this script and not share the same account with your browser and the sessions cannot be shared between the two.

DOWNLOAD_DIR = None
WVD_DEVICE_PATH = None
LOCAL_STORAGE_PATH = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

ENDPOINTS = {
    "authorize": "https://watch-cdn.edge-api.tvnz.co.nz/media/content/authorize",
    "register": "https://watch-cdn.edge-api.tvnz.co.nz/device/app/register",
    "refresh": "https://rest-prod-tvnz.evergentpd.com/tvnz/refreshToken",
    "catalog": "https://data-store-cdn.cms-api.tvnz.co.nz/content/urn/resource/catalog",
    "entitlements": "https://rest-prod-tvnz.evergentpd.com/tvnz/getEntitlements",
    "contact": "https://rest-prod-tvnz.evergentpd.com/tvnz/getContact",
    "oauth": "https://watch-cdn.edge-api.tvnz.co.nz/oauth2/token",
}

WEB_CLIENT = {
    "client_id": "webclient-ui-app",
    "client_secret": "f99d00b8-5b20-4c27-983d-d2895f3e9fec",
}

CONTACT = {
    "channelPartnerID": "TVNZ_NZ",
    "apiUser": "qpapiuser",
    "apiPassword": "Tv9z@pi2026$",
}

CATALOG_PARAMS = {
    "reg": "nz",
    "dt": "web",
    "client": "tvnz-tvnz-web",
    "pf": "Regular",
    "allowpg": "true",
}

class bcolors:
    OKGREEN = "\033[92m"
    LIGHTBLUE = "\033[94m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"


def first_name(value):
    if isinstance(value, list) and value:
        item = value[0]

        if isinstance(item, dict):
            n = item.get("n", "")
            if isinstance(n, list) and n:
                return n[0]
            return n

        if isinstance(item, str):
            return item

    if isinstance(value, dict):
        n = value.get("n", "")
        if isinstance(n, list) and n:
            return n[0]
        return n

    if isinstance(value, str):
        return value

    return ""


def safe_name(text):
    if isinstance(text, list):
        text = text[0] if text else ""

    if isinstance(text, dict):
        text = first_name(text)

    text = str(text or "TVNZ")

    text = re.sub(r"[^\w\s.-]", "", text)
    text = re.sub(r"\s+", ".", text.strip())
    text = text.replace("-", ".")
    text = re.sub(r"\.+", ".", text)

    return text.strip(".").title()


def extract_title_path(video_url):
    """
    Supports:
      https://www.tvnz.co.nz/tvepisode/tauranga-hilltop
      https://www.tvnz.co.nz/player/tvepisode/tauranga-hilltop
      https://www.tvnz.co.nz/movie/the-creator
      https://www.tvnz.co.nz/event/bula-fc-v-auckland-fc
      https://www.tvnz.co.nz/sporthighlight/sheep-dog-trials
      
    """
    video_url = video_url.strip()

    if video_url.startswith("/"):
        path = video_url
    else:
        path = urlparse(video_url).path

    path = path.rstrip("/")
    path = path.replace("/player/", "/")

    match = re.match(
        r"^/(tvepisode|movie|event|sporthighlight|newsclip|sportclip)/([^/]+)$",
        path,
    )
    if not match:
        raise ValueError(
            "Unsupported TVNZ URL. Expected /tvepisode/, /movie/, /event/, "
            "/sporthighlight/, /newsclip/, or /sportclip/ URL."
        )

    return path


class TVNZAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Origin": "https://tvnz.co.nz",
            "Referer": "https://tvnz.co.nz/",
        })

        self.access_token = None
        self.refresh_token = None
        self.device_ref = None
        self.contact_id = None
        self.xauthorization = None
        self.oauth_token = None
        self.secret = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def load_local_storage(self):
        path = LOCAL_STORAGE_PATH
        if not path:
            raise ValueError('Missing config value: tvnz.local_storage')

        if not os.path.exists(path):
            raise FileNotFoundError(f"TVNZ local storage file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # Normal expected format:
        # { "accessToken": "...", "refreshToken": "...", "deviceref": "..." }
        data = raw

        # Fallback for some browser/local-storage exporters:
        # [ {"key": "accessToken", "value": "..."}, ... ]
        if isinstance(raw, list):
            data = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                key = item.get("key") or item.get("name")
                value = item.get("value")
                if key:
                    data[key] = value

        self.access_token = data.get("accessToken")
        self.refresh_token = data.get("refreshToken")
        self.device_ref = data.get("deviceref")

        missing = [
            name for name, value in {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "deviceref": self.device_ref,
            }.items()
            if not value
        ]

        if missing:
            raise ValueError(f"Missing required local storage field(s): {', '.join(missing)}")

        print(f"{bcolors.OKGREEN}✅ Loaded TVNZ local storage tokens{bcolors.ENDC}")

    def refresh_user_tokens_if_needed(self):
        """
        Uses refreshToken if accessToken has expired.
        Writes refreshed values back into the same local_storage JSON if possible.
        """
        try:
            decoded = jwt.decode(self.access_token, options={"verify_signature": False})
            exp = int(decoded.get("exp", 0))
        except Exception:
            exp = 0

        if exp > int(time.time()) + 120:
            return

        print(f"{bcolors.YELLOW}Access token expired or close to expiry. Refreshing...{bcolors.ENDC}")

        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
        }

        payload = {
            "RefreshTokenRequestMessage": {
                **CONTACT,
                "refreshToken": self.refresh_token,
            }
        }

        r = self.session.post(ENDPOINTS["refresh"], headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        msg = data.get("RefreshTokenResponseMessage", {})
        if msg.get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to refresh TVNZ user tokens: {data}")

        self.access_token = msg["accessToken"]
        self.refresh_token = msg["refreshToken"]

        # Best-effort writeback for dict-style local_storage.json
        path = LOCAL_STORAGE_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)

            if isinstance(raw, dict):
                raw["accessToken"] = self.access_token
                raw["refreshToken"] = self.refresh_token
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(raw, f, indent=4)

                print(f"{bcolors.OKGREEN}✅ Refreshed tokens written back to local storage JSON{bcolors.ENDC}")
        except Exception as e:
            print(f"{bcolors.YELLOW}Token refreshed, but could not update local storage file: {e}{bcolors.ENDC}")

    def get_contact_id(self):
        headers = {
            "accept": "application/json, text/plain, */*",
            "authorization": f"Bearer {self.access_token}",
            "content-type": "application/json",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
        }

        payload = {
            "GetContactRequestMessage": {
                **CONTACT
            }
        }

        r = self.session.post(ENDPOINTS["contact"], headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        if data.get("GetContactResponseMessage", {}).get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to get contact ID: {data}")

        self.contact_id = data["GetContactResponseMessage"]["contactMessage"][0]["contactID"]
        print(f"{bcolors.OKGREEN}✅ Contact ID obtained{bcolors.ENDC}")

    def get_entitlements(self):
        headers = {
            "accept": "application/json, text/plain, */*",
            "authorization": f"Bearer {self.access_token}",
            "content-type": "application/json",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
        }

        payload = {
            "GetEntitlementsRequestMessage": {
                "contactID": self.contact_id,
                **CONTACT,
                "returnUpgradableFlag": "true",
                "returnProductAttributes": "true",
            }
        }

        r = self.session.post(ENDPOINTS["entitlements"], headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        msg = data.get("GetEntitlementsResponseMessage", {})
        if msg.get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to get entitlements: {data}")

        self.xauthorization = msg.get("ovatToken")
        if not self.xauthorization:
            raise ValueError(f"x-authorization token missing: {data}")

        print(f"{bcolors.OKGREEN}✅ Entitlement token obtained{bcolors.ENDC}")

    def get_oauth_token(self):
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
        }

        payload = {
            **WEB_CLIENT,
            "grant_type": "client_credentials",
            "audience": "edge-service",
            "scope": "offline openid",
        }

        r = self.session.post(ENDPOINTS["oauth"], headers=headers, data=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

        self.oauth_token = data.get("access_token")
        if not self.oauth_token:
            raise ValueError(f"OAuth token missing: {data}")

        print(f"{bcolors.OKGREEN}✅ OAuth token obtained{bcolors.ENDC}")

    def register_app(self):
        headers = {
            "accept": "*/*",
            "authorization": f"Bearer {self.oauth_token}",
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
            "x-authorization": self.xauthorization,
            "x-client-id": "tvnz-tvnz-web",
        }

        r = self.session.post(
            ENDPOINTS["register"],
            headers=headers,
            data=json.dumps({"uniqueId": self.device_ref}),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        self.secret = data.get("data", {}).get("secret")
        if not self.secret:
            raise ValueError(f"App registration secret missing: {data}")

        print(f"{bcolors.OKGREEN}✅ App registered{bcolors.ENDC}")

    def authenticate(self):
        self.load_local_storage()
        self.refresh_user_tokens_if_needed()
        self.get_contact_id()
        self.get_entitlements()
        self.get_oauth_token()
        self.register_app()

    # ------------------------------------------------------------------
    # Metadata / playback
    # ------------------------------------------------------------------

    def get_video_from_url(self, video_url):
        title_path = extract_title_path(video_url)

        r = self.session.get(
            ENDPOINTS["catalog"] + title_path,
            params=CATALOG_PARAMS,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        video = data.get("data")
        if not video:
            raise ValueError(f"Failed to get catalog metadata: {data}")

        print(f"{bcolors.LIGHTBLUE}Catalog Path: {bcolors.ENDC}{title_path}")
        print(f"{bcolors.LIGHTBLUE}Content ID: {bcolors.ENDC}{video.get('nu')}")
        print(f"{bcolors.LIGHTBLUE}Catalog Type: {bcolors.ENDC}{video.get('cty')}")

        return video

    def get_device_token(self):
        secret_bytes = base64.b64decode(self.secret)

        payload = {
            "deviceId": self.device_ref,
            "aud": "playback-auth-service",
            "iat": int(time.time()),
            "exp": int(time.time()) + 30,
        }

        return jwt.encode(payload, secret_bytes, algorithm="HS256")

    def authorize_playback(self, video):
        device_token = self.get_device_token()

        headers = {
            "accept": "*/*",
            "authorization": f"Bearer {self.oauth_token}",
            "content-type": "application/json",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
            "x-authorization": self.xauthorization,
            "x-client-id": "tvnz-tvnz-web",
            "x-device-id": device_token,
            "x-device-type": "web",
        }

        payload = {
            "deviceName": "web",
            "deviceId": self.device_ref,
            "contentId": video.get("nu"),
            "contentTypeId": "vod",
            "catalogType": video.get("cty"),
            "mediaFormat": "dash",
            "drm": "widevine",
            "delivery": "streaming",
            "disableSsai": "true",
            "deviceManufacturer": "web",
            "deviceModelName": "Chrome browser on Windows",
            "deviceModelNumber": "Chrome",
            "deviceOs": USER_AGENT,
            "supportedAudioCodecs": "mp4a",
            "supportedVideoCodecs": "avc,hevc,av01",
            "supportedMaxWVSecurityLevel": "L3",
            "deviceToken": device_token,
            "urlParameters": {
                "vpa": "click",
                "rdid": self.device_ref,
                "is_lat": "0",
                "npa": "0",
                "idtype": "dpid",
                "endpoint": "web",
                "endpoint-group": "desktop",
                "endpoint_detail": "desktop",
            },
        }

        r = self.session.post(ENDPOINTS["authorize"], headers=headers, json=payload, timeout=30)

        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError:
            print(f"{bcolors.FAIL}Authorize failed:{bcolors.ENDC}")
            print(r.text[:2000])
            raise

        data = r.json()

        if data.get("header", {}).get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to authorize playback: {data}")

        playback = data.get("data", {})
        mpd_url = playback.get("contentUrl")
        license_url = playback.get("licenseUrl")

        if mpd_url:
            mpd_url = mpd_url.split("?")[0]

        if not mpd_url or not license_url:
            raise ValueError(f"Playback response missing MPD/license URL: {data}")

        return mpd_url, license_url

    # ------------------------------------------------------------------
    # DASH / Widevine
    # ------------------------------------------------------------------

    def get_pssh(self, url_mpd):
        response = self.session.get(url_mpd, timeout=30)
        response.raise_for_status()

        if b"<MPD" not in response.content[:3000]:
            print(response.text[:1000])
            raise ValueError("MPD request did not return a DASH manifest. Check NZ proxy/session.")

        root = ET.fromstring(response.content)

        cps = root.findall(".//{urn:mpeg:dash:schema:mpd:2011}ContentProtection")

        # Prefer Widevine system ID.
        for elem in cps:
            scheme = (elem.attrib.get("schemeIdUri") or "").lower()
            if "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" not in scheme:
                continue

            pssh = elem.find("{urn:mpeg:cenc:2013}pssh")
            if pssh is not None and pssh.text:
                pssh_data = pssh.text.strip()
                base64.b64decode(pssh_data)
                return pssh_data

        # Fallback to first valid pssh.
        for elem in cps:
            pssh = elem.find("{urn:mpeg:cenc:2013}pssh")
            if pssh is not None and pssh.text:
                pssh_data = pssh.text.strip()
                try:
                    base64.b64decode(pssh_data)
                    return pssh_data
                except binascii.Error:
                    continue

        return None

    def get_keys(self, pssh, lic_url):
        try:
            pssh = PSSH(pssh)
        except Exception as e:
            print(f"{bcolors.FAIL}Could not parse PSSH: {e}{bcolors.ENDC}")
            return []

        device = Device.load(WVD_DEVICE_PATH)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, pssh)

        headers = {
            "accept": "*/*",
            "authorization": f"Bearer {self.oauth_token}",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
        }

        licence = self.session.post(lic_url, headers=headers, data=challenge, timeout=30)

        try:
            licence.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"{bcolors.FAIL}License request failed: {e}{bcolors.ENDC}")
            print(f"Response Headers: {licence.headers}")
            print(f"Response Text: {licence.text[:2000]}")
            raise

        cdm.parse_license(session_id, licence.content)
        keys = [
            f"{key.kid.hex}:{key.key.hex()}"
            for key in cdm.get_keys(session_id)
            if key.type == "CONTENT"
        ]
        cdm.close(session_id)
        return keys

    def get_highest_resolution(self, url_mpd):
        response = self.session.get(url_mpd, timeout=30)
        response.raise_for_status()

        root = ET.fromstring(response.content)
        representations = root.findall(".//{urn:mpeg:dash:schema:mpd:2011}Representation")

        max_height = 0
        for rep in representations:
            try:
                height = int(rep.get("height", 0))
                max_height = max(max_height, height)
            except ValueError:
                pass

        if max_height >= 2160:
            return "2160p"
        if max_height >= 1080:
            return "1080p"
        if max_height >= 720:
            return "720p"
        return "SD"

    def get_mpd_streams(self, url_mpd):
        response = self.session.get(url_mpd, timeout=30)
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


def build_filename(video, resolution):
    show_title = first_name(video.get("lostl")) or first_name(video.get("lok")) or "TVNZ"
    episode_name = first_name(video.get("lodn")) or first_name(video.get("lon")) or video.get("nu") or "Video"

    cty = video.get("cty")
    season = video.get("snum")
    episode = video.get("epnum")

    show = safe_name(show_title)
    name = safe_name(episode_name)

    if cty == "tvepisode" and season and episode:
        return f"{show}.S{int(season):02}E{int(episode):02}.{name}.{resolution}.TVNZ.WEB-DL.AAC2.0.H.264"

    if cty == "movie":
        return f"{name}.{resolution}.TVNZ.WEB-DL.AAC2.0.H.264"

    return f"{name}.{resolution}.TVNZ.WEB-DL.AAC2.0.H.264"

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

def build_download_command(mpd_url, formatted_file_name, keys, mode="auto"):
    selectors = "" if mode == "interactive" else '--select-video best --select-audio best -da role="Description" --select-subtitle all '
    download_command = (
        f'N_m3u8DL-RE "{mpd_url}" '
        f'{selectors}'
        f'-mt -M format=mkv '
        f'--save-name "{formatted_file_name}" '
        f'--save-dir "{DOWNLOAD_DIR}" '
    )

    if keys:
        download_command += "--key " + " --key ".join(keys)

    return append_downloader_proxy(download_command)

def get_download_command(video_url, mode="auto"):
    api = TVNZAPI()
    api.authenticate()

    video = api.get_video_from_url(video_url)
    mpd_url, lic_url = api.authorize_playback(video)

    pssh = api.get_pssh(mpd_url)
    if not pssh:
        print(f"{bcolors.FAIL}Failed to extract PSSH data{bcolors.ENDC}")
        return

    keys = api.get_keys(pssh, lic_url)
    resolution = api.get_highest_resolution(mpd_url)
    formatted_file_name = build_filename(video, resolution)

    print(f"{bcolors.LIGHTBLUE}MPD URL: {bcolors.ENDC}{mpd_url}")
    print(f"{bcolors.RED}License URL: {bcolors.ENDC}{lic_url}")
    print(f"{bcolors.LIGHTBLUE}PSSH: {bcolors.ENDC}{pssh}")

    for key in keys:
        print(f"{bcolors.GREEN}KEYS: {bcolors.ENDC}--key {key}")

    if mode == "info":
        print_streams(api.get_mpd_streams(mpd_url))
        print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")
        return

    download_command = build_download_command(mpd_url, formatted_file_name, keys, mode)

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
    print(mask_proxy_command(download_command))

    user_input = input("Do you wish to download? Y or N: ").strip().lower()
    if user_input == "y":
        subprocess.run(download_command, shell=True)


def main(video_url, downloads_path, wvd_device_path, local_storage_path, mode="auto"):
    global DOWNLOAD_DIR, WVD_DEVICE_PATH, LOCAL_STORAGE_PATH
    
    DOWNLOAD_DIR = downloads_path
    WVD_DEVICE_PATH = wvd_device_path
    LOCAL_STORAGE_PATH = local_storage_path

    get_download_command(video_url, mode)

