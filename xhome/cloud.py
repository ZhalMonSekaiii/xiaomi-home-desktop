"""Cloud transport: the private API that the Xiaomi Home / Mi Home app talks to.

Login  : account.xiaomi.com (QR long-polling or password) -> userId + ssecurity + serviceToken
API    : https://{server}.api.io.mi.com/app/<path>   (server "cn" -> https://api.io.mi.com/app)
Body   : every form field is RC4 encrypted with a per-request signed nonce and signed with SHA1,
         the response body is RC4 encrypted the same way (header MIOT-ENCRYPT-ALGORITHM).
"""
from __future__ import annotations

import hashlib
import json
import random
import string
import time

import requests

from .crypto import build_encrypted_params, rc4_decrypt_b64

SERVERS = ["sg", "cn", "de", "us", "ru", "tw", "in", "i2"]
SERVER_NAMES = {
    "cn": "China", "de": "Europe", "us": "United States", "ru": "Russia",
    "tw": "Taiwan", "sg": "Singapore (Indonesia/SEA)", "in": "India", "i2": "Europe 2",
}
ACCOUNT = "https://account.xiaomi.com"
STS_CALLBACK = "https://sts.api.io.mi.com/sts"


class CloudError(Exception):
    def __init__(self, message: str, code=None):
        super().__init__(message if code is None else f"{message} (code {code})")
        self.code = code


class SessionExpired(CloudError):
    pass


def _json(text: str) -> dict:
    return json.loads(text.replace("&&&START&&&", "", 1))


def _random_agent() -> tuple[str, str]:
    agent_id = "".join(random.choice("ABCDE") for _ in range(13))
    prefix = "".join(random.choice(string.ascii_lowercase) for _ in range(18))
    return f"{prefix}-{agent_id} APP/com.xiaomi.mihome APPV/10.5.201", agent_id


def _tz_cookie() -> str:
    offset = -time.timezone if not time.localtime().tm_isdst else -time.altzone
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return f"GMT{sign}{offset // 3600:02d}:{offset % 3600 // 60:02d}"


class MiCloud:
    def __init__(self, session: dict | None = None):
        session = session or {}
        self.user_id: str | None = session.get("user_id")
        self.c_user_id: str | None = session.get("c_user_id")
        self.ssecurity: str | None = session.get("ssecurity")
        self.service_token: str | None = session.get("service_token")
        self.pass_token: str | None = session.get("pass_token")
        self.server: str = session.get("server") or "sg"
        self.agent: str = session.get("agent") or _random_agent()[0]
        self.device_id: str = session.get("device_id") or "".join(random.choices(string.ascii_lowercase, k=6))
        self.http = requests.Session()

    # ---------------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id, "c_user_id": self.c_user_id, "ssecurity": self.ssecurity,
            "service_token": self.service_token, "pass_token": self.pass_token,
            "server": self.server, "agent": self.agent, "device_id": self.device_id,
        }

    @property
    def logged_in(self) -> bool:
        return bool(self.user_id and self.ssecurity and self.service_token)

    # ---------------------------------------------------------------------- login
    def _account_cookies(self, **extra) -> dict:
        return {"sdkVersion": "accountsdk-18.8.15", "deviceId": self.device_id, **extra}

    def _account_headers(self) -> dict:
        return {"User-Agent": self.agent, "Content-Type": "application/x-www-form-urlencoded"}

    def qr_login_start(self) -> dict:
        """Step 1 of QR login. Returns {qr, login_url, lp, timeout}; scan `qr` with the Xiaomi Home app."""
        params = {
            "_qrsize": "480", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue", "callback": STS_CALLBACK,
            "_hasLogo": "false", "sid": "xiaomiio", "serviceParam": "", "_locale": "en_US",
            "_dc": str(int(time.time() * 1000)),
        }
        r = self.http.get(f"{ACCOUNT}/longPolling/loginUrl", params=params,
                          headers=self._account_headers(), cookies=self._account_cookies(), timeout=15)
        r.raise_for_status()
        data = _json(r.text)
        if "qr" not in data or "lp" not in data:
            raise CloudError(f"unexpected QR login response: {data}")
        return {"qr": data["qr"], "login_url": data.get("loginUrl"), "lp": data["lp"],
                "timeout": int(data.get("timeout", 300))}

    def qr_image(self, qr_url: str) -> bytes:
        r = self.http.get(qr_url, headers=self._account_headers(), timeout=15)
        r.raise_for_status()
        return r.content

    def qr_login_wait(self, lp_url: str, timeout: int = 300, cancel=None) -> None:
        """Step 2 of QR login: long-poll until the user confirms on the phone."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel is not None and cancel():
                raise CloudError("login cancelled")
            try:
                r = self.http.get(lp_url, headers=self._account_headers(), timeout=10)
            except requests.exceptions.Timeout:
                continue
            if r.status_code != 200:
                time.sleep(1)
                continue
            data = _json(r.text)
            if "ssecurity" not in data:
                if data.get("code") not in (None, 0):
                    time.sleep(1)
                continue
            self.user_id = str(data["userId"])
            self.c_user_id = data.get("cUserId")
            self.pass_token = data.get("passToken")
            self.ssecurity = data["ssecurity"]
            self._fetch_service_token(data["location"])
            return
        raise CloudError("QR login timed out")

    def password_login(self, username: str, password: str) -> None:
        """Classic login. Accounts with 2FA / captcha should use QR login instead."""
        self.http.cookies.clear()
        r = self.http.get(f"{ACCOUNT}/pass/serviceLogin", params={"sid": "xiaomiio", "_json": "true"},
                          headers=self._account_headers(), cookies=self._account_cookies(userId=username),
                          timeout=15)
        step1 = _json(r.text)
        data = {
            "sid": "xiaomiio", "hash": hashlib.md5(password.encode()).hexdigest().upper(),
            "callback": STS_CALLBACK, "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue", "user": username,
            "_sign": step1.get("_sign", ""), "_json": "true",
        }
        r = self.http.post(f"{ACCOUNT}/pass/serviceLoginAuth2", data=data, headers=self._account_headers(),
                           cookies=self._account_cookies(), timeout=15)
        step2 = _json(r.text)
        if step2.get("notificationUrl"):
            raise CloudError("this account needs 2FA verification - open this link in a browser and retry, "
                             f"or use QR/browser login: {step2['notificationUrl']}")
        if step2.get("captchaUrl"):
            raise CloudError("Xiaomi asked for a captcha - use QR/browser login instead")
        if "ssecurity" not in step2 or not step2.get("location"):
            raise CloudError(f"login failed: {step2.get('desc') or step2.get('description') or step2}",
                             step2.get("code"))
        self.user_id = str(step2["userId"])
        self.c_user_id = step2.get("cUserId")
        self.pass_token = step2.get("passToken")
        self.ssecurity = step2["ssecurity"]
        self._fetch_service_token(step2["location"])

    def refresh(self) -> bool:
        """Get a fresh serviceToken from the long-lived passToken (no user interaction)."""
        if not (self.user_id and self.pass_token):
            return False
        r = self.http.get(f"{ACCOUNT}/pass/serviceLogin", params={"sid": "xiaomiio", "_json": "true"},
                          headers=self._account_headers(),
                          cookies=self._account_cookies(userId=self.user_id, passToken=self.pass_token),
                          timeout=15)
        data = _json(r.text)
        if data.get("code") != 0 or not data.get("ssecurity") or not data.get("location"):
            return False
        self.ssecurity = data["ssecurity"]
        self._fetch_service_token(data["location"])
        return True

    def _fetch_service_token(self, location: str) -> None:
        r = self.http.get(location, headers=self._account_headers(), timeout=15)
        token = r.cookies.get("serviceToken") or self.http.cookies.get("serviceToken")
        if not token:
            raise CloudError("no serviceToken received after login")
        self.service_token = token

    def logout(self) -> None:
        self.user_id = self.c_user_id = self.ssecurity = self.service_token = self.pass_token = None

    # ------------------------------------------------------------------ API core
    @staticmethod
    def api_base(server: str) -> str:
        return "https://api.io.mi.com/app" if server == "cn" else f"https://{server}.api.io.mi.com/app"

    def request(self, path: str, data: dict, server: str | None = None, _retry: bool = True):
        """POST an encrypted request to /app<path> and return the decrypted `result`."""
        if not self.logged_in:
            raise SessionExpired("not logged in to the cloud - run `python -m xhome login`")
        server = server or self.server
        fields, snonce = build_encrypted_params(
            "POST", path, {"data": json.dumps(data, separators=(",", ":"))}, self.ssecurity)
        headers = {
            "User-Agent": self.agent, "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2", "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }
        cookies = {
            "userId": self.user_id, "yetAnotherServiceToken": self.service_token,
            "serviceToken": self.service_token, "locale": "en_US", "timezone": _tz_cookie(),
            "is_daylight": "0", "dst_offset": "0", "channel": "MI_APP_STORE",
        }
        r = self.http.post(self.api_base(server) + path, params=fields, headers=headers,
                           cookies=cookies, timeout=20)
        if r.status_code in (401, 403):
            if _retry and self.refresh():
                return self.request(path, data, server, _retry=False)
            raise SessionExpired("cloud session expired - log in again", r.status_code)
        if r.status_code != 200:
            raise CloudError(f"HTTP {r.status_code} from {path}: {r.text[:200]}")
        try:
            body = json.loads(rc4_decrypt_b64(snonce, r.text))
        except Exception:
            try:
                body = r.json()  # some errors come back unencrypted
            except ValueError as exc:
                raise CloudError(f"could not decrypt the response from {path}") from exc
        if body.get("code") not in (0, None):
            if body.get("code") in (2, 3) and _retry and self.refresh():
                return self.request(path, data, server, _retry=False)
            raise CloudError(body.get("message") or str(body), body.get("code"))
        return body.get("result")

    # -------------------------------------------------------------- inventory
    def homes(self, server: str | None = None) -> list[dict]:
        res = self.request("/v2/homeroom/gethome",
                           {"fg": True, "fetch_share": True, "fetch_share_dev": True,
                            "limit": 300, "app_ver": 7}, server) or {}
        homes = [dict(h, _owner=int(self.user_id)) for h in res.get("homelist") or []]
        try:
            cnt = self.request("/v2/user/get_device_cnt", {"fetch_own": True, "fetch_share": True}, server) or {}
            for h in (cnt.get("share") or {}).get("share_family") or []:
                homes.append({"id": h["home_id"], "name": h.get("home_name", "Shared"),
                              "_owner": h["home_owner"], "roomlist": [], "_shared": True})
        except CloudError:
            pass
        return homes

    def devices(self, server: str | None = None) -> list[dict]:
        """Every device on the account (incl. shared homes) with token, local IP, room name."""
        server = server or self.server
        by_did: dict[str, dict] = {}
        for home in self.homes(server):
            rooms = {did: room.get("name") for room in home.get("roomlist") or [] for did in room.get("dids") or []}
            res = self.request("/v2/home/home_device_list",
                               {"home_owner": home["_owner"], "home_id": int(home["id"]), "limit": 200,
                                "get_split_device": True, "support_smart_home": True}, server) or {}
            for dev in res.get("device_info") or []:
                dev["home_name"] = home.get("name")
                dev["room_name"] = rooms.get(dev.get("did"))
                by_did[dev["did"]] = dev
        if not by_did:  # older accounts / fallback endpoint
            res = self.request("/home/device_list",
                               {"getVirtualModel": True, "getHuamiDevices": 1, "get_split_device": False,
                                "support_smart_home": True}, server) or {}
            for dev in res.get("list") or []:
                by_did[dev["did"]] = dev
        for dev in by_did.values():
            dev["server"] = server
        return list(by_did.values())

    def find_server(self) -> dict[str, int]:
        """Count devices on every regional server (an account can have devices on several)."""
        counts = {}
        for server in SERVERS:
            try:
                counts[server] = len(self.devices(server))
            except CloudError:
                counts[server] = -1
        return counts

    # ------------------------------------------------------------ device control
    def miot_get(self, did: str, props: list[tuple[int, int]], server: str | None = None) -> list[dict]:
        params = [{"did": did, "siid": s, "piid": p} for s, p in props]
        return self.request("/miotspec/prop/get", {"params": params}, server) or []

    def miot_set(self, did: str, siid: int, piid: int, value, server: str | None = None) -> list[dict]:
        params = [{"did": did, "siid": siid, "piid": piid, "value": value}]
        return self.request("/miotspec/prop/set", {"params": params}, server) or []

    def miot_action(self, did: str, siid: int, aiid: int, ins: list | None = None, server: str | None = None):
        params = {"did": did, "siid": siid, "aiid": aiid, "in": ins or []}
        return self.request("/miotspec/action", {"params": params}, server)

    def rpc(self, did: str, method: str, params=None, server: str | None = None):
        """Legacy miIO command relayed by the cloud (same as sending it over LAN)."""
        return self.request(f"/home/rpc/{did}", {"method": method, "params": [] if params is None else params},
                            server)
