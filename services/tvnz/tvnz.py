import base64
import binascii
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
import jwt
import requests
import urllib3
import yaml
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from colors import bcolors
import icons
from filename_utils import safe_windows_filename
from download_confirm import confirm_download
from quality_utils import apply_quality_to_filename, video_selector

from services.proxy import append_downloader_proxy, mask_proxy_command
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DOWNLOAD_DIR = None
WVD_DEVICE_PATH = None
TVNZ_CREDENTIAL = None
CONFIG_PATH = None
TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "temp"))
EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "export"))
console = Console()

USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 11; Android TV Build/RTMA.250416.082)"
)

TVNZ_HEADERS = {
    "x-device-type": "androidtv",
    "x-app-store-type": "androidtv",
    "x-client-id": "tvnz-tvnz-androidtv",
    "user-agent": USER_AGENT,
}

ENDPOINTS = {
    "authorize": "https://watch-cdn.edge-api.tvnz.co.nz/media/content/authorize",
    "register": "https://watch-cdn.edge-api.tvnz.co.nz/device/app/register",
    "refresh": "https://rest-prod-tvnz.evergentpd.com/tvnz/refreshToken",
    "catalog": "https://data-store-cdn.cms-api.tvnz.co.nz/content/urn/resource/catalog",
    "seasons": "https://data-store-cdn.cms-api.tvnz.co.nz/content/series/{series_id}/seasons",
    "episodes": "https://data-store-cdn.cms-api.tvnz.co.nz/content/series/{series_id}/episodes",
    "entitlements": "https://rest-prod-tvnz.evergentpd.com/tvnz/getEntitlements",
    "contact": "https://rest-prod-tvnz.evergentpd.com/tvnz/getContact",
    "oauth": "https://watch-cdn.edge-api.tvnz.co.nz/oauth2/token",
}

ANDROIDTV_CLIENT = {
    "client_id": "android-ui-app",
    "client_secret": "dcb2b779-1cf8-4672-81ed-8ee1a8345389",
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
            **TVNZ_HEADERS,
            "Origin": "https://tvnz.co.nz",
            "Referer": "https://tvnz.co.nz/",
        })

        self.access_token = None
        self.refresh_token = None
        self.device_ref = None
        self.email = None
        self.contact_id = None
        self.xauthorization = None
        self.oauth_token = None
        self.secret = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_config_email(self):
        credential = TVNZ_CREDENTIAL
        if isinstance(credential, dict):
            credential = credential.get("default")
        if isinstance(credential, list):
            credential = credential[0] if credential else None

        email = str(credential or "").split(":", 1)[0].strip()
        if not email:
            raise ValueError("Missing config value: credentials.tvnz")
        if "@" not in email:
            raise ValueError("TVNZ credential must contain an email address")
        return email

    def read_config(self):
        if not CONFIG_PATH:
            return {}
        if not os.path.exists(CONFIG_PATH):
            return {}
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def write_config(self, config):
        if not CONFIG_PATH:
            return
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)

    def load_cached_tokens(self):
        config = self.read_config()
        cache = config.get("tvnz", {}).get("cache")
        if not isinstance(cache, dict):
            return None
        if cache.get("email") != self.email:
            return None
        required = ("accessToken", "refreshToken", "deviceID")
        if any(not cache.get(key) for key in required):
            return None
        return cache

    def save_cached_tokens(self, tokens):
        config = self.read_config()
        tvnz_config = config.setdefault("tvnz", {})
        tvnz_config["cache"] = tokens
        self.write_config(config)

    def access_token_is_current(self, access_token):
        try:
            decoded = jwt.decode(access_token, options={"verify_signature": False})
            exp = int(decoded.get("exp", 0))
        except Exception:
            return False
        return exp > int(time.time()) + 120

    def create_otp(self):
        payload = {
            "CreateOTPRequestMessage": {
                **CONTACT,
                "email": self.email,
            }
        }
        r = self.session.post("https://rest-prod-tvnz.evergentpd.com/tvnz/createOTP", json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        msg = data.get("CreateOTPResponseMessage", {})
        if not msg.get("isUserExist", False):
            raise ValueError(f"TVNZ user not found for email: {self.email}")
        if msg.get("status", "").lower() != "success":
            raise ConnectionError(f"Failed to create TVNZ OTP: {data}")

    def confirm_otp(self, otp, device_id):
        payload = {
            "ConfirmOTPRequestMessage": {
                **CONTACT,
                "email": self.email,
                "canCreateAccount": True,
                "checkDeviceLimit": True,
                "dmaId": "001",
                "otp": otp,
                "isGenerateJWT": True,
                "isPrivacyPoliciesAccepted": True,
                "isTAndCAccepted": True,
                "deviceDetails": {
                    "deviceType": "Android TV",
                    "deviceName": "androidtv",
                    "modelNo": "Android TV",
                    "appType": "Android",
                    "serialNo": device_id,
                },
            }
        }
        r = self.session.post("https://rest-prod-tvnz.evergentpd.com/tvnz/confirmOTP", json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        msg = data.get("ConfirmOTPResponseMessage", {})
        if msg.get("status", "").lower() != "success":
            raise ConnectionError(f"Failed to confirm TVNZ OTP: {data}")
        return msg

    def refresh_user_tokens(self, tokens):
        payload = {
            "RefreshTokenRequestMessage": {
                **CONTACT,
                "refreshToken": tokens.get("refreshToken"),
            }
        }
        r = self.session.post(ENDPOINTS["refresh"], json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        msg = data.get("RefreshTokenResponseMessage", {})
        if msg.get("message", "").lower() != "success":
            raise ConnectionError(f"Failed to refresh TVNZ user tokens: {data}")

        refreshed = tokens.copy()
        refreshed.update(msg)
        refreshed["email"] = self.email
        self.save_cached_tokens(refreshed)
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Refreshed TVNZ cached user tokens{bcolors.ENDC}")
        return refreshed

    def create_user_tokens(self):
        print(f"{bcolors.YELLOW}{icons.ICON_INFO} No TVNZ cached user tokens found. Sending OTP...{bcolors.ENDC}")
        self.create_otp()
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} OTP code sent to {self.email}{bcolors.ENDC}")
        otp = input("Enter TVNZ OTP code: ").strip().replace(" ", "")
        if not otp:
            raise ValueError("OTP code not provided")

        device_id = str(uuid.uuid4())
        confirmation = self.confirm_otp(otp, device_id)
        params = confirmation.get("params") or []
        tokens = {p.get("paramName"): p.get("paramValue") for p in params if p.get("paramName")}
        tokens["contactID"] = confirmation.get("contactID")
        tokens["deviceID"] = device_id
        tokens["email"] = self.email
        self.save_cached_tokens(tokens)
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} TVNZ user tokens cached{bcolors.ENDC}")
        return tokens

    def load_or_create_user_tokens(self):
        self.email = self.get_config_email()
        tokens = self.load_cached_tokens()
        if not tokens:
            return self.create_user_tokens()
        if self.access_token_is_current(tokens.get("accessToken")):
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Loaded TVNZ cached user tokens{bcolors.ENDC}")
            return tokens
        print(f"{bcolors.YELLOW}{icons.ICON_INFO} TVNZ cached token expired or close to expiry. Refreshing...{bcolors.ENDC}")
        try:
            return self.refresh_user_tokens(tokens)
        except Exception as error:
            print(f"{bcolors.YELLOW}{icons.ICON_WARNING} TVNZ token refresh failed: {error}{bcolors.ENDC}")
            return self.create_user_tokens()

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
        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Contact ID obtained{bcolors.ENDC}")

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

        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Entitlement token obtained{bcolors.ENDC}")

    def get_oauth_token(self):
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
        }

        payload = {
            **ANDROIDTV_CLIENT,
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

        print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} OAuth token obtained{bcolors.ENDC}")

    def register_app(self):
        headers = {
            "accept": "*/*",
            "authorization": f"Bearer {self.oauth_token}",
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://tvnz.co.nz",
            "user-agent": USER_AGENT,
            "x-authorization": self.xauthorization,
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

        #print(f"{bcolors.OKGREEN}{ICON_SUCCESS} App registered{bcolors.ENDC}")

    def authenticate(self):
        tokens = self.load_or_create_user_tokens()
        self.access_token = tokens.get("accessToken")
        self.refresh_token = tokens.get("refreshToken")
        self.device_ref = tokens.get("deviceID")
        self.contact_id = tokens.get("contactID")

        if not all((self.access_token, self.refresh_token, self.device_ref)):
            raise ValueError("TVNZ cached token data is incomplete")
        if not self.contact_id:
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
            "x-device-id": device_token,
        }

        payload = {
            "deviceName": "androidtv",
            "deviceId": self.device_ref,
            "deviceManufacturer": "Android TV",
            "deviceModelName": "Android TV",
            "deviceOs": "Android",
            "deviceOsVersion": "10",
            "contentId": video.get("nu"),
            "mediaFormat": "dash",
            "contentTypeId": "vod",
            "catalogType": video.get("cty"),
            "drm": "widevine",
            "delivery": "streaming",
            "quality": "high",
            "disableSsai": "true",
            "supportedResolution": "UHD",
            "supportedAudioCodecs": "mp4a",
            "supportedVideoCodecs": "avc,hevc,av01",
            "supportedMaxWVSecurityLevel": "L1",
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

def clean_info_value(value):
    value = clean_text(first_name(value) if isinstance(value, (list, dict)) else value)
    return re.sub(r"\s+", " ", value).strip()

def format_info_date(value):
    value = clean_info_value(value)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"{parsed.day} {parsed.strftime('%B %Y')}"
    except Exception:
        return value

def print_info_metadata(video):
    if not video:
        return

    show_title = clean_info_value(video.get("lostl") or video.get("lok"))
    episode_title = clean_info_value(video.get("lodn") or video.get("lon") or video.get("nu"))
    date_aired = format_info_date(video.get("oadt") or video.get("adte") or video.get("broadcastDateTime") or video.get("r"))
    description = clean_info_value(video.get("losd") or video.get("lold") or video.get("sd") or video.get("synopsis"))

    rows = [
        ("Show", show_title),
        ("Title", episode_title),
        ("Date Aired", date_aired),
        ("Description", description),
    ]
    rows = [(label, value) for label, value in rows if value]
    if not rows:
        return

    print(f"\n{bcolors.YELLOW}Episode metadata:{bcolors.ENDC}")
    for label, value in rows:
        print(f"{bcolors.LIGHTBLUE}{label}: {bcolors.ENDC}{value}")

def build_download_command(mpd_url, formatted_file_name, keys, mode="auto", quality=None, save_subs=False):
    subtitle_selector = "--select-subtitle all" if save_subs else "--drop-subtitle all"
    selectors = "" if mode == "interactive" else f'{video_selector(quality)} --select-audio best -da role="Description" {subtitle_selector} '
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

def get_download_command(video_url, mode="auto", auto_download=False, quality=None, auto_confirm=False, save_subs=False):
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
        print_info_metadata(video)
        print(f"\n{bcolors.YELLOW}Suggested filename: {bcolors.ENDC}{formatted_file_name}.mkv")
        return

    formatted_file_name = apply_quality_to_filename(formatted_file_name, quality)
    download_command = build_download_command(mpd_url, formatted_file_name, keys, mode, quality, save_subs)

    print(f"{bcolors.YELLOW}DOWNLOAD COMMAND:{bcolors.ENDC}")
    print(mask_proxy_command(download_command))

    if confirm_download("Do you wish to download? Y or N: ", auto_confirm=auto_confirm, auto_download=auto_download):
        print(f"{bcolors.LIGHTBLUE}{icons.ICON_INFO} Download starting{bcolors.ENDC}")
        result = subprocess.run(download_command, shell=True)
        if result.returncode == 0:
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Download complete{bcolors.ENDC}")
    else:
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")


def clean_text(text):
    if not text:
        return ""
    return (
        str(text)
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .strip()
    )

def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def extract_series_title_path(series_url):
    series_url = series_url.strip()
    path = series_url if series_url.startswith("/") else urlparse(series_url).path
    path = path.replace("/player/", "/").rstrip("/")
    match = re.match(r"^/tvseries/([\w-]+)$", path)
    if not match:
        raise ValueError("Invalid TVNZ series URL. Expected format like: https://www.tvnz.co.nz/tvseries/grand-designs-new-zealand")
    return path, match.group(1)

def looks_like_tvnz_series_url(video_url):
    path = video_url if video_url.startswith("/") else urlparse(video_url).path
    path = path.replace("/player/", "/").rstrip("/")
    return bool(re.match(r"^/tvseries/[\w-]+$", path))

def catalogue_fetch_json(session, url, params=None):
    response = session.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()

def fetch_series(session, title_path):
    data = catalogue_fetch_json(session, ENDPOINTS["catalog"] + title_path, CATALOG_PARAMS)
    series = data.get("data") or {}
    series_id = series.get("id")
    if not series_id:
        raise ValueError("Could not find TVNZ series ID in catalogue response.")
    show_title = clean_text(first_name(series.get("lon")) or first_name(series.get("lodn")) or title_path.split("/")[-1].replace("-", " ").title())
    return series_id, show_title

def fetch_seasons(session, series_id):
    params = {
        **CATALOG_PARAMS,
        "pageNumber": "1",
        "pageSize": "99",
        "sortBy": "asc",
        "sortOrder": "desc",
    }
    data = catalogue_fetch_json(session, ENDPOINTS["seasons"].format(series_id=series_id), params)
    seasons = data.get("data") or []
    return [season for season in seasons if season.get("id")]

def fetch_episodes_for_season(session, series_id, season_id):
    params = {
        **CATALOG_PARAMS,
        "seasonId": season_id,
        "pageNumber": "1",
        "pageSize": "99",
        "sortBy": "epnum",
        "sortOrder": "asc",
    }
    data = catalogue_fetch_json(session, ENDPOINTS["episodes"].format(series_id=series_id), params)
    return data.get("data") or []

def get_thumbnail(episode):
    image_id = episode.get("id")
    aspect = "0-16x9"
    if isinstance(episode.get("ia"), list) and episode["ia"]:
        aspect = episode["ia"][0]
    if not image_id:
        return ""
    return f"https://image-resizer-cloud-cdn.cms-api.tvnz.co.nz/image/{image_id}/{aspect}.jpg?width=320"

def episode_to_list_item(episode, show_title):
    video_id = episode.get("nu") or episode.get("id") or ""
    season_number = episode.get("snum") or ""
    episode_number = episode.get("epnum") or ""
    ctype = episode.get("cty") or "tvepisode"
    episode_name = clean_text(first_name(episode.get("lodn")) or first_name(episode.get("lon")))
    fallback_title = f"Season {season_number} Episode {episode_number}".strip()
    title = episode_name or fallback_title
    air_date = episode.get("oadt") or episode.get("adte") or episode.get("broadcastDateTime") or episode.get("r") or ""
    description = clean_text(first_name(episode.get("losd")) or first_name(episode.get("lold")) or first_name(episode.get("sd")) or episode.get("synopsis"))

    return {
        "Video URL": f"https://www.tvnz.co.nz/{ctype}/{video_id}" if video_id else "",
        "Video ID": video_id,
        "Show Title": show_title,
        "Season": season_number,
        "Season Label": f"Season {season_number}" if season_number != "" else "Episodes",
        "Episode": episode_number,
        "Episode Label": str(episode_number) if episode_number != "" else "-",
        "Sort Season": parse_int(season_number) or 0,
        "Sort Episode": parse_int(episode_number) or 0,
        "Title": title,
        "Description": description,
        "Date Aired": air_date,
        "Thumbnail": get_thumbnail(episode),
    }

def collect_episode_details(series_url):
    title_path, show_slug = extract_series_title_path(series_url)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    series_id, show_title = fetch_series(session, title_path)
    seasons = fetch_seasons(session, series_id)

    episodes = []
    for season in seasons:
        for episode in fetch_episodes_for_season(session, series_id, season["id"]):
            item = episode_to_list_item(episode, show_title)
            if item["Video ID"]:
                episodes.append(item)

    episodes.sort(key=lambda item: (item.get("Sort Season") or 0, item.get("Sort Episode") or 0, item.get("Title") or ""))
    episode_data = {
        "Episode Summary": [
            f"{episode['Season Label']} Episode {episode['Episode Label']} - {episode['Title']}"
            for episode in episodes
        ],
        "Episode Details": episodes,
    }
    return show_slug, episode_data

def save_episode_list_json(show_slug, episode_data):
    os.makedirs(TEMP_DIR, exist_ok=True)
    output_path = os.path.join(TEMP_DIR, f"tvnz_{safe_windows_filename(show_slug)}_episodes.json")
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(episode_data, file, ensure_ascii=False, indent=4)
    return output_path

def export_episode_list_text(show_slug, episodes):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    safe_slug = safe_windows_filename(show_slug)
    output_path = os.path.join(EXPORT_DIR, f"tvnz_{safe_slug}_export_{timestamp}.txt")

    with open(output_path, "w", encoding="utf-8") as file:
        for episode in episodes:
            label = episode.get("Season Label") or "Episodes"
            episode_number = episode.get("Episode Label") or "-"
            title = episode.get("Title") or "-"
            url = episode.get("Video URL") or "-"
            file.write(f"{label} Episode {episode_number} - {title}\n")
            file.write(f"{url}\n")

    return output_path

def print_episode_list(series_title, episodes):
    if not episodes:
        print(f"{bcolors.WARNING}No playable TVNZ episodes found.{bcolors.ENDC}")
        return

    tree_style = "grey70"
    label_style = "bold grey70"
    header_style = "bright_blue"
    groups = {}
    for episode in episodes:
        label = episode.get("Season Label") or "Episodes"
        groups.setdefault(label, []).append(episode)

    group_labels = sorted(groups, key=lambda label: parse_int(re.search(r"\d+", label).group(0)) if re.search(r"\d+", label) else 0)
    for group_episodes in groups.values():
        group_episodes.sort(key=lambda item: item.get("Sort Episode") or item.get("Episode") or 0)

    season_summary = ",  ".join(f"{label}({len(groups[label])})" for label in group_labels)
    console.print(Rule(Text.assemble(("TVNZ Series: ", f"bold {header_style}"), (series_title, "bold white")), style=header_style))
    console.print()
    console.print(
        Text.assemble(
            (f"{len(group_labels)} Seasons", label_style),
            (f",  {season_summary}" if season_summary else "", "white"),
        )
    )

    for group_index, label in enumerate(group_labels):
        if group_index > 0:
            console.print(Text("│", style=tree_style))

        group_is_last = group_index == len(group_labels) - 1
        group_branch = "└─" if group_is_last else "├─"
        group_child_prefix = "   " if group_is_last else "│  "
        group_episodes = groups[label]
        console.print(Text.assemble((f"{group_branch} ", tree_style), (f"{label}: ", label_style), (f"{len(group_episodes)} episodes", "white")))

        for index, episode in enumerate(group_episodes):
            is_last = index == len(group_episodes) - 1
            branch = "└─" if is_last else "├─"
            url_branch = "  " if is_last else "│ "
            console.print(
                Text.assemble(
                    (group_child_prefix, tree_style),
                    (f"{branch} ", tree_style),
                    (f"{episode.get('Episode Label') or '-'}. ", label_style),
                    (episode.get("Title") or "-", "white"),
                )
            )
            console.print(
                Text.assemble(
                    (group_child_prefix, tree_style),
                    (f"{url_branch} ", tree_style),
                    (episode.get("Video URL") or "-", "bright_blue"),
                )
            )

def list_show_episodes(series_url, export_list=False):
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    show_slug, episode_data = collect_episode_details(series_url)
    episodes = episode_data["Episode Details"]
    series_title = episodes[0].get("Show Title") if episodes else show_slug.replace("-", " ").title()
    output_path = save_episode_list_json(show_slug, episode_data)

    try:
        console.print()
        print_episode_list(series_title, episodes)
        print(f"\n{bcolors.OKGREEN}{icons.ICON_SUCCESS} Found {len(episodes)} episode(s){bcolors.ENDC}")
        if export_list:
            export_path = export_episode_list_text(show_slug, episodes)
            print(f"{bcolors.OKGREEN}{icons.ICON_SUCCESS} Exported list: {export_path}{bcolors.ENDC}")
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)

def parse_selector_part(selector_part):
    match = re.fullmatch(r"s(?P<season>\d{2}|\d{4})(?:e(?P<episode>\d{2}))?", selector_part)
    if not match:
        raise ValueError(
            "Download selector must be sXXeXX, sXXXXeXX, sXX, sXXXX, or a matching range. "
            "Examples: s01e01, s2026e01, s01, s2026, s01e03-s02e02, s01-s03"
        )

    return {
        "season": int(match.group("season")),
        "episode": int(match.group("episode")) if match.group("episode") else None,
    }

def parse_download_selector(selector):
    selector = str(selector or "").strip().lower()
    if "-" not in selector:
        part = parse_selector_part(selector)
        return {
            "type": "single_episode" if part["episode"] is not None else "single_season",
            "start": part,
            "end": part,
        }

    range_parts = selector.split("-", 1)
    if not range_parts[0] or not range_parts[1]:
        raise ValueError(
            "Download range must include both start and end selectors. "
            "Examples: s01e03-s02e02 or s01-s03"
        )

    start = parse_selector_part(range_parts[0])
    end = parse_selector_part(range_parts[1])
    start_has_episode = start["episode"] is not None
    end_has_episode = end["episode"] is not None

    if start_has_episode != end_has_episode:
        raise ValueError("Download range must use two episode selectors or two season selectors.")

    if start_has_episode:
        if (start["season"], start["episode"]) > (end["season"], end["episode"]):
            raise ValueError("Download episode range start must be before the end selector.")
        return {"type": "episode_range", "start": start, "end": end}

    if start["season"] > end["season"]:
        raise ValueError("Download season range start must be before the end selector.")
    return {"type": "season_range", "start": start, "end": end}

def format_selector_part(part):
    season = part["season"]
    season_label = f"s{season:04d}" if season >= 1000 else f"s{season:02d}"
    if part["episode"] is not None:
        return f"{season_label}e{part['episode']:02d}"
    return season_label

def format_download_selector(parsed_selector):
    if parsed_selector["start"] == parsed_selector["end"]:
        return format_selector_part(parsed_selector["start"])
    return f"{format_selector_part(parsed_selector['start'])}-{format_selector_part(parsed_selector['end'])}"

def format_queue_selector(season, episode=None):
    season_label = f"S{season:04d}" if season >= 1000 else f"S{season:02d}"
    if episode is not None:
        return f"{season_label}E{episode:02d}"
    return season_label

def warn_if_partial_range_match(parsed_selector, selected):
    if parsed_selector["type"] == "episode_range":
        requested_start = (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
        requested_end = (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
        matched_start = (parse_int(selected[0].get("Season")) or 0, parse_int(selected[0].get("Episode")) or 0)
        matched_end = (parse_int(selected[-1].get("Season")) or 0, parse_int(selected[-1].get("Episode")) or 0)
        if matched_start > requested_start or matched_end < requested_end:
            matched_label = f"{format_queue_selector(*matched_start)}-{format_queue_selector(*matched_end)}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched {matched_label}.{bcolors.ENDC}")

    if parsed_selector["type"] == "season_range":
        requested_start = parsed_selector["start"]["season"]
        requested_end = parsed_selector["end"]["season"]
        matched_seasons = sorted({parse_int(item.get("Season")) or 0 for item in selected})
        if matched_seasons[0] > requested_start or matched_seasons[-1] < requested_end:
            matched_label = f"{format_queue_selector(matched_seasons[0])}-{format_queue_selector(matched_seasons[-1])}"
            print(f"{bcolors.WARNING}{icons.ICON_WARNING} Requested range {format_download_selector(parsed_selector)} only matched seasons {matched_label}.{bcolors.ENDC}")

def select_episodes(series_url, selector):
    parsed_selector = parse_download_selector(selector)
    _, episode_data = collect_episode_details(series_url)
    episodes = episode_data["Episode Details"]

    selected = []
    for episode in episodes:
        season = parse_int(episode.get("Season"))
        episode_number = parse_int(episode.get("Episode"))
        if season is None or episode_number is None:
            continue

        if parsed_selector["type"] == "single_episode":
            keep = season == parsed_selector["start"]["season"] and episode_number == parsed_selector["start"]["episode"]
        elif parsed_selector["type"] == "single_season":
            keep = season == parsed_selector["start"]["season"]
        elif parsed_selector["type"] == "episode_range":
            keep = (
                (parsed_selector["start"]["season"], parsed_selector["start"]["episode"])
                <= (season, episode_number)
                <= (parsed_selector["end"]["season"], parsed_selector["end"]["episode"])
            )
        else:
            keep = parsed_selector["start"]["season"] <= season <= parsed_selector["end"]["season"]

        if keep:
            selected.append(episode)

    if not selected:
        normalized = format_download_selector(parsed_selector)
        series_title = episodes[0].get("Show Title") if episodes else extract_series_title_path(series_url)[1].replace("-", " ").title()
        raise LookupError(f"No TVNZ episodes found for selector {normalized} in {series_title}.")

    selected.sort(key=lambda item: (item.get("Sort Season") or 0, item.get("Sort Episode") or 0, item.get("Title") or ""))
    warn_if_partial_range_match(parsed_selector, selected)
    return selected

def format_queue_label(episode):
    season = parse_int(episode.get("Season"))
    episode_number = parse_int(episode.get("Episode"))
    title = episode.get("Title") or episode.get("Video URL") or "-"

    if season is not None and episode_number is not None:
        return f"S{season:02d}E{episode_number:02d} {title}"
    if episode_number is not None:
        return f"E{episode_number:02d} {title}"
    return title

def print_download_queue(episodes):
    print(f"\n{bcolors.YELLOW}Download queue:{bcolors.ENDC}")
    for episode in episodes:
        print(f"{format_queue_label(episode)}")

def download_selected_episodes(series_url, selector, downloads_path, wvd_device_path, tvnz_credential, config_path, quality=None, auto_confirm=False, save_subs=False):
    print(f"{bcolors.LIGHTBLUE}{icons.ICON_WAITING} Retrieving series information.....{bcolors.ENDC}")
    try:
        episodes = select_episodes(series_url, selector)
    except LookupError as error:
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} {error}{bcolors.ENDC}")
        return
    print_download_queue(episodes)

    if not confirm_download(f"\nDownload {len(episodes)} episode(s)? Y or N: ", auto_confirm=auto_confirm):
        print(f"{bcolors.RED}{icons.ICON_FAILURE} Download Cancelled{bcolors.ENDC}")
        return

    for index, episode in enumerate(episodes, start=1):
        print(f"\n{bcolors.LIGHTBLUE}{icons.ICON_INFO} Downloading {index}/{len(episodes)}: {format_queue_label(episode)}{bcolors.ENDC}")
        main(
            episode["Video URL"],
            downloads_path,
            wvd_device_path,
            tvnz_credential,
            config_path,
            mode="auto",
            export_list=False,
            download_selector=None,
            auto_download=True,
            quality=quality,
            auto_confirm=auto_confirm,
            save_subs=save_subs,
        )

def main(video_url, downloads_path, wvd_device_path, tvnz_credential, config_path, mode="auto", export_list=False, download_selector=None, auto_download=False, quality=None, auto_confirm=False, save_subs=False):
    global DOWNLOAD_DIR, WVD_DEVICE_PATH, TVNZ_CREDENTIAL, CONFIG_PATH
    
    DOWNLOAD_DIR = downloads_path
    WVD_DEVICE_PATH = wvd_device_path
    TVNZ_CREDENTIAL = tvnz_credential
    CONFIG_PATH = config_path

    if mode == "list":
        list_show_episodes(video_url, export_list)
        return

    if mode == "download":
        download_selected_episodes(video_url, download_selector, downloads_path, wvd_device_path, tvnz_credential, config_path, quality, auto_confirm, save_subs)
        return

    if looks_like_tvnz_series_url(video_url):
        print(f"{bcolors.WARNING}{icons.ICON_WARNING} TVNZ series URLs need a flag.{bcolors.ENDC}")
        print(f"{bcolors.YELLOW}{icons.ICON_INFO} Use -l to list episodes or -d with a selector to download from a series.{bcolors.ENDC}")
        return

    get_download_command(video_url, mode, auto_download, quality, auto_confirm, save_subs)

