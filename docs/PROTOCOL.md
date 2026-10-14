# Reverse-engineering notes — Xiaomi Home protocols

The Xiaomi Home (Mi Home) app talks to devices over **two paths**. `xhome` implements both.

```
                    ┌──────────────── Cloud (internet) ─────────────────┐
 Laptop (xhome) ──► │ account.xiaomi.com  → login, ssecurity, serviceToken │
        │           │ {sg}.api.io.mi.com  → RC4-encrypted API              │ ──► gateway / device
        │           └─────────────────────────────────────────────────────┘
        │
        └──── LAN (UDP 54321, miIO protocol, AES-128-CBC with the device token) ────► Wi-Fi device
```

| | Local (LAN) | Cloud (API server) |
|---|---|---|
| Transport | UDP 54321, binary miIO | HTTPS POST, RC4-encrypted form |
| Secret | 16-byte token per device | `ssecurity` + `serviceToken` per account |
| Needs internet | no | yes |
| Devices | Wi-Fi only | all (Wi-Fi, BLE, Zigbee through a gateway) |
| Latency | ~2–50 ms | ~150–800 ms |

---

## 1. Account login (`xhome/cloud.py`)

### 1a. QR / browser (default — no password handled by xhome, works with 2FA/captcha)
1. `GET https://account.xiaomi.com/longPolling/loginUrl`
   `?_qrsize=480&qs=%3Fsid%3Dxiaomiio%26_json%3Dtrue&callback=https://sts.api.io.mi.com/sts&sid=xiaomiio&_dc=<ms>`
   The response starts with `&&&START&&&`, followed by JSON: `qr` (PNG URL), `loginUrl`, `lp` (long-polling
   URL), `timeout`.
2. The user approves the ticket, either by scanning `qr` with a phone camera or by opening `loginUrl` in any
   browser. `loginUrl` redirects to Xiaomi's regular login page (`/fe/service/login`); signing in there
   approves the ticket. The Xiaomi Home app's "Add device" scanner does **not** handle these QR codes.
3. `GET lp` (repeat every 10 s until HTTP 200) → `userId`, `ssecurity`, `cUserId`, `passToken`, `location`.
4. `GET location` → **`serviceToken`** cookie.

### 1b. Password
1. `GET /pass/serviceLogin?sid=xiaomiio&_json=true` → `_sign`
2. `POST /pass/serviceLoginAuth2` with `user`, `hash = MD5(password).upper()`, `_sign`, `sid`, `callback`, `qs`
   → `ssecurity`, `userId`, `location` (or `notificationUrl` = 2FA required, `captchaUrl` = captcha)
3. `GET location` → `serviceToken`

### 1c. Silent refresh
`GET /pass/serviceLogin?sid=xiaomiio&_json=true` with the `userId` + `passToken` cookies → new `ssecurity` and
`location` → new `serviceToken`. `xhome` does this automatically when the API answers 401.

## 2. Encrypted cloud API requests

Base URL: `https://api.io.mi.com/app` (cn) or `https://{server}.api.io.mi.com/app`
(`sg` = Singapore, used by Southeast Asian accounts; others: `de us ru tw in i2`).

For every request (`xhome/crypto.py → build_encrypted_params`):

```
nonce        = base64( random(8) + uint32_be(now_ms / 60000) )
signed_nonce = base64( SHA256( b64dec(ssecurity) + b64dec(nonce) ) )
params       = { "data": "<json>" }
params.rc4_hash__ = base64(SHA1("POST&/path&data=<json>&" + signed_nonce))     # path without /app
every param value = base64( RC4(key=b64dec(signed_nonce), first 1024 keystream bytes dropped)(value) )
signature    = base64(SHA1("POST&/path&data=<enc>&rc4_hash__=<enc>&" + signed_nonce))
send: data, rc4_hash__, signature, ssecurity, _nonce   (as the query string of a POST)
```

Headers: `MIOT-ENCRYPT-ALGORITHM: ENCRYPT-RC4`, `x-xiaomi-protocal-flag-cli: PROTOCAL-HTTP2`,
User-Agent `… APP/com.xiaomi.mihome APPV/10.5.201`.
Cookies: `userId`, `serviceToken`, `yetAnotherServiceToken`, `locale`, `timezone`, `channel`.
Response = base64 RC4 with the same `signed_nonce` → JSON `{"code":0,"message":"ok","result":…}`.

### Endpoints used

| Path | `data` | Purpose |
|---|---|---|
| `/v2/homeroom/gethome` | `{fg, fetch_share, fetch_share_dev, limit:300, app_ver:7}` | homes + rooms |
| `/v2/user/get_device_cnt` | `{fetch_own, fetch_share}` | homes shared with the account |
| `/v2/home/home_device_list` | `{home_owner, home_id, limit:200, get_split_device, support_smart_home}` | devices + **token** + **localip** |
| `/home/device_list` | `{getVirtualModel, getHuamiDevices, …}` | fallback device list |
| `/miotspec/prop/get` | `{params:[{did,siid,piid}]}` | read properties |
| `/miotspec/prop/set` | `{params:[{did,siid,piid,value}]}` | write properties |
| `/miotspec/action` | `{params:{did,siid,aiid,in:[…]}}` | run an action |
| `/home/rpc/{did}` | `{method, params}` | legacy miIO command relayed by the cloud |

## 3. Local miIO protocol (`xhome/local.py`)

```
offset 0   2       4          8          12       16                 32
       2131 length  00000000   device-id  stamp    MD5 checksum (16)  AES payload
```

* **Hello / discovery:** `21 31 00 20` + 28×`FF`, broadcast to UDP 54321. Every device answers with its
  `device-id` + `stamp` (seconds since boot). Devices that are not set up yet leak their token in the
  checksum field.
* **Keys:** `key = MD5(token)`, `iv = MD5(key + token)`, AES-128-CBC + PKCS#7.
* **Checksum:** `MD5(header[0:16] + token + encrypted_payload)`.
* **Stamp:** `stamp_from_hello + seconds_since_hello + 1` (packets with an older stamp are ignored).
* **Payload:** JSON `{"id":N,"method":"…","params":…}` + `\x00`. `id` must increase; the reply carries the
  same `id`.
* Wrong token → the device stays **silent** (no error reply), which looks like a timeout.

Common methods: `miIO.info`, `get_properties`, `set_properties`, `action` (MIoT), plus model-specific legacy
methods (`get_prop`, `set_power`, `app_start`, …).

**Firmware quirk:** `xiaomi.airc.ra4r00` (Mijia AC) wraps some string values in double quotes, e.g.
`"value":""12345/ABCDEFG""`, which is invalid JSON. `local.py` repairs this before parsing.

## 4. MIoT-Spec (`xhome/spec.py`)

Device features are described publicly on `miot-spec.org`:

* `GET https://miot-spec.org/miot-spec-v2/instances?status=all` → model → URN (~48k models, ~7 MB, cached 7 days)
* `GET https://miot-spec.org/miot-spec-v2/instance?type=<urn>` → services → properties / actions / events

Addressing: properties are `(siid, piid)`, actions `(siid, aiid)`. `xhome` derives names from the URN, e.g.
`urn:miot-spec-v2:property:brightness:…` in the `light` service → `light.brightness` (= `2.2`).

## 5. Common error codes

MIoT codes returned by devices (LAN path; the cloud usually wraps them as `-7040xxxxx`):

| Code | Meaning |
|---|---|
| `-4001` | property not readable |
| `-4002` | property not writable |
| `-4003` | property/action/event does not exist |
| `-4004` | internal device error |
| `-4005` | invalid property value |
| `-4006` | invalid action `in` parameters |
| `-704042011` | device offline (cloud) |
| `-32601` / `-9999` | miIO method not supported / device did not respond |
| HTTP 401 | `serviceToken` expired → refresh / log in again |
