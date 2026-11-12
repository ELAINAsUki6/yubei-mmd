"""二游 MMD 模型导航 —— 带审核的后端。

只用 Python 标准库，不装任何依赖。

流程：
    访客提交  ->  data/pending.json（待审核）
    站长审核  ->  通过 -> data/published.json（所有人可见）
                 拒绝 ->  丢弃（记一份 rejected 备查）

数据文件：
    data/published.json   线上数据（游戏分区 + 角色 + 配布地址）
    data/pending.json     待审核队列
    data/rejected.json    被拒记录（防重复、留痕）
    data/admin.token      管理员口令，第一次运行自动生成

启动：
    python server.py            # 默认 http://127.0.0.1:8000
    python server.py 8080 0.0.0.0   # 端口 / 监听地址（0.0.0.0 = 局域网可访问）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import sys
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

try:  # 控制台按 UTF-8 输出，免得中文变成乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
PUBLISHED = DATA / "published.json"
PENDING = DATA / "pending.json"
REJECTED = DATA / "rejected.json"
TOKEN_FILE = DATA / "admin.token"
TOTP_FILE = DATA / "totp.secret"
USERS = DATA / "users.json"
FAVS = DATA / "favs.json"
SECRET_FILE = DATA / "secret.key"
EVENTS = DATA / "events.json"
INDEXNOW_FILE = DATA / "indexnow.key"
BAIDU_TOKEN_FILE = DATA / "baidu.token"

SITE_URL = "https://ybslmmd.cn/"
SITE_ORIGIN = "https://ybslmmd.cn"  # 百度 site 参数要求无末尾斜杠
SITE_HOST = "ybslmmd.cn"
PING_COOLDOWN = 120          # IndexNow：没配额限制，2 分钟一次
BAIDU_COOLDOWN = 6 * 3600    # 百度每天只有 10 条额度，6 小时一次就够
_last_ping = [0.0, 0.0]      # [IndexNow, 百度]

# 普通用户：注册登录 + 收藏同步
USER_TTL = 30 * 24 * 3600        # 登录票 30 天
USER_RATE: dict[str, list[float]] = {}   # ip -> 注册时间
USER_RATE_LIMIT = 20             # 每 IP 每小时最多注册 20 个
                                 # （校园网 / 公司网 / 手机流量常常很多人共用一个出口 IP）

# 登录会话：口令/动态码只用来"换票"，之后的请求带这张票
SESSIONS: dict[str, float] = {}
SESSION_TTL = 8 * 3600            # 8 小时后要重新验证

LOCK = threading.Lock()
EVENT_LOCK = threading.Lock()   # 单独一把，避免和管理操作的 LOCK 嵌套死锁
RATE: dict[str, list[float]] = {}       # ip -> 最近提交时间戳
RATE_LIMIT = 20                          # 每 IP 每小时最多提交条数
RATE_WINDOW = 3600
LOGIN_FAIL: dict[str, list[float]] = {}  # ip -> 最近失败次数
LOGIN_MAX_FAIL = 8                       # 一小时内错 8 次就锁
PALETTE = ["#e06c9f", "#7fd0c8", "#b79cf0", "#8fb8e0", "#e0d070", "#f0a0c0", "#a8b4c8", "#8fc7f0"]

MAX_BODY = 64 * 1024
MAX_TEXT = 200
URL_RE = re.compile(r"^https?://[^\s<>\"']{6,500}$", re.I)


# ---------------------------------------------------------------- 存储

def _read(path: Path, default):
    try:
        # utf-8-sig：Windows 记事本 / PowerShell 存出来的 JSON 常带 BOM，
        # 用 utf-8 读会直接解析失败然后被当成空数据。
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _write(path: Path, obj) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_published() -> dict:
    data = _read(PUBLISHED, None)
    if not isinstance(data, dict):
        data = {"games": [], "links": {}, "updatedAt": None}
    data.setdefault("games", [])
    data.setdefault("links", {})
    return data


def load_pending() -> list:
    data = _read(PENDING, [])
    return data if isinstance(data, list) else []


def load_rejected() -> list:
    data = _read(REJECTED, [])
    return data if isinstance(data, list) else []


def admin_token() -> str:
    DATA.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.is_file():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(9)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


# ---------------------------------------------------------------- 动态口令（TOTP）
# 标准 RFC 6238，和 Google Authenticator / 微软 Authenticator 通用。
# 6 位数字，30 秒一换；服务器允许前后各一个时间窗（防手机时钟稍偏）。


def totp_secret() -> str:
    DATA.mkdir(parents=True, exist_ok=True)
    if TOTP_FILE.is_file():
        s = TOTP_FILE.read_text(encoding="utf-8").strip()
        if s:
            return s
    raw = secrets.token_bytes(20)                       # 160 bit，标准长度
    s = base64.b32encode(raw).decode().rstrip("=")
    TOTP_FILE.write_text(s, encoding="utf-8")
    return s


def totp_code(secret: str, step: int, digits: int = 6) -> str:
    pad = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + pad)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % (10 ** digits):0{digits}d}"


def totp_verify(secret: str, code: str, window: int = 1) -> bool:
    code = (code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return False
    step = int(time.time() // 30)
    for delta in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret, step + delta), code):
            return True
    return False


def totp_uri(secret: str, issuer: str = "MMDHub", account: str = "admin") -> str:
    return (f"otpauth://totp/{issuer}:{account}?secret={secret}"
            f"&issuer={issuer}&algorithm=SHA1&digits=6&period=30")


# ---------------------------------------------------------------- 会话

def new_session() -> tuple[str, float]:
    sid = secrets.token_urlsafe(18)
    expires = time.time() + SESSION_TTL
    with LOCK:
        # 顺手清掉过期的
        for key, exp in list(SESSIONS.items()):
            if exp <= time.time():
                SESSIONS.pop(key, None)
        SESSIONS[sid] = expires
    return sid, expires


def session_ok(sid: str) -> bool:
    if not sid:
        return False
    with LOCK:
        exp = SESSIONS.get(sid)
        if exp and exp > time.time():
            return True
        SESSIONS.pop(sid, None)
    return False


def login_locked(ip: str) -> bool:
    now = time.time()
    fails = [t for t in LOGIN_FAIL.get(ip, []) if now - t < RATE_WINDOW]
    LOGIN_FAIL[ip] = fails
    return len(fails) >= LOGIN_MAX_FAIL


def login_fail(ip: str) -> None:
    LOGIN_FAIL.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------- 普通用户账号

def server_secret() -> bytes:
    DATA.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.is_file():
        raw = SECRET_FILE.read_text(encoding="utf-8").strip()
        if raw:
            return bytes.fromhex(raw)
    raw = secrets.token_bytes(32)
    SECRET_FILE.write_text(raw.hex(), encoding="utf-8")
    return raw


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex(), digest.hex()


def check_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return secrets.compare_digest(digest.hex(), hash_hex)


USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5·\-]{2,20}$")

# 不许抢的名字：防止冒充站长 / 官方
RESERVED_NAMES = {
    "admin", "administrator", "root", "moderator", "mod", "system", "support",
    "official", "staff", "owner", "webmaster",
    "管理员", "站长", "官方", "版主", "客服", "系统", "超管",
    "ybsl", "ybslmmd", "ybsl520",
}


def normalize_name(name: str) -> str:
    """全角转半角、去首尾空格。这样「ＡＢＣ」「ABC」「abc 」会当成同一个名字。"""
    return unicodedata.normalize("NFKC", name or "").strip()


def find_user(users: dict, name: str) -> str:
    """按"不区分大小写 + 规范化"找已存在的用户名，返回存储时用的原始写法。"""
    target = normalize_name(name).casefold()
    for existing in users:
        if normalize_name(existing).casefold() == target:
            return existing
    return ""


def load_users() -> dict:
    data = _read(USERS, {})
    return data if isinstance(data, dict) else {}


def load_favs() -> dict:
    data = _read(FAVS, {})
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 搜索引擎推送
# IndexNow：必应 / Yandex 等共用，只要把 <key>.txt 放到站点根目录即可，不用注册。
# 百度：需要在「百度搜索资源平台」拿 token，放进 data/baidu.token（可选）。

def indexnow_key() -> str:
    DATA.mkdir(parents=True, exist_ok=True)
    if INDEXNOW_FILE.is_file():
        k = INDEXNOW_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    k = secrets.token_hex(16)
    INDEXNOW_FILE.write_text(k, encoding="utf-8")
    return k


def _ping_indexnow() -> str:
    key = indexnow_key()
    body = json.dumps({
        "host": SITE_HOST,
        "key": key,
        "keyLocation": f"{SITE_URL}{key}.txt",
        "urlList": [SITE_URL],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.indexnow.org/indexnow", data=body,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return f"IndexNow {r.status}"


def _ping_baidu() -> str:
    if not BAIDU_TOKEN_FILE.is_file():
        return "百度 未配置（data/baidu.token）"
    token = BAIDU_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        return "百度 未配置"
    req = urllib.request.Request(
        f"http://data.zz.baidu.com/urls?site={SITE_ORIGIN}&token={token}",
        data=SITE_URL.encode("utf-8"),
        headers={"Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        detail = r.read().decode("utf-8", "replace").strip()[:160]
        return f"百度 {r.status} {detail}"


def ping_search_engines(force: bool = False) -> list[str]:
    now = time.time()
    out = []

    # 必应 / Yandex：没配额限制，随便推
    if force or now - _last_ping[0] >= PING_COOLDOWN:
        _last_ping[0] = now
        try:
            out.append(_ping_indexnow())
        except Exception as exc:  # noqa: BLE001
            out.append(f"必应 IndexNow 失败: {exc}")
    else:
        out.append("必应 IndexNow 跳过（冷却中）")

    # 百度：每天 10 条，站点又只有一个 URL，6 小时推一次足够
    if not BAIDU_TOKEN_FILE.is_file():
        out.append("百度 未配置（data/baidu.token）")
    elif force or now - _last_ping[1] >= BAIDU_COOLDOWN:
        _last_ping[1] = now
        try:
            out.append(_ping_baidu())
        except Exception as exc:  # noqa: BLE001
            out.append(f"百度 失败: {exc}")
    else:
        left = int((BAIDU_COOLDOWN - (now - _last_ping[1])) / 60) + 1
        out.append(f"百度 跳过（约 {left} 分钟后再推，每天只有 10 条额度）")

    for msg in out:
        print(f"  [搜索引擎推送] {msg}", flush=True)
    return out


def ping_async() -> None:
    """内容变了就在后台推一次，别拖慢请求。"""
    threading.Thread(target=ping_search_engines, kwargs={"force": False}, daemon=True).start()


# ---------------------------------------------------------------- 更新日志

def load_events() -> list:
    data = _read(EVENTS, [])
    return data if isinstance(data, list) else []


def log_event(text: str, kind: str = "info") -> None:
    """记一条更新日志。写失败不能影响主流程。
    注意：用独立的 EVENT_LOCK —— 调用点常常已经持有 LOCK，
    threading.Lock 不可重入，共用会直接死锁。"""
    try:
        with EVENT_LOCK:
            events = load_events()
            events.insert(0, {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "kind": kind,
                "text": text,
            })
            _write(EVENTS, events[:400])
    except Exception:  # noqa: BLE001
        pass


DEFAULT_FOLDER = {"id": "default", "name": "默认收藏"}


def normalize_favs(value) -> dict:
    """收藏统一成 {folders:[{id,name}], items:[{key,type,folder,...}]}。
    老版本存的是 ["nte/薄荷", ...] 这种字符串数组，这里自动升级。"""
    if isinstance(value, list):
        items = []
        for k in value[:500]:
            if isinstance(k, str) and "/" in k:
                gid, name = k.split("/", 1)
                items.append({"key": "c:" + k, "type": "char", "folder": "default",
                              "gameId": gid[:60], "charName": name[:60]})
        return {"folders": [dict(DEFAULT_FOLDER)], "items": items}

    if not isinstance(value, dict):
        return {"folders": [dict(DEFAULT_FOLDER)], "items": []}

    folders = []
    for f in (value.get("folders") or [])[:30]:
        if isinstance(f, dict):
            fid = f.get("id")
            fname = f.get("name")
            if isinstance(fid, str) and isinstance(fname, str) and fid.strip() and fname.strip():
                folders.append({"id": fid.strip()[:24], "name": fname.strip()[:20]})
    ids = [f["id"] for f in folders]
    if not folders:
        folders = [dict(DEFAULT_FOLDER)]
        ids = ["default"]

    items = []
    for it in (value.get("items") or [])[:500]:
        if not isinstance(it, dict):
            continue
        key = it.get("key")
        if not isinstance(key, str) or not (2 <= len(key) <= 400):
            continue
        folder = it.get("folder")
        item = {
            "key": key,
            "type": "link" if it.get("type") == "link" else "char",
            "folder": folder if folder in ids else ids[0],
        }
        for field, limit in (("gameId", 60), ("charName", 60), ("url", 500),
                             ("platform", 40), ("kind", 10), ("note", 120)):
            v = it.get(field)
            if isinstance(v, str) and v.strip():
                item[field] = v.strip()[:limit]
        items.append(item)

    return {"folders": folders, "items": items}


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")


def make_user_token(username: str) -> str:
    # 用户名可能是中文，HTTP 头只能放 ASCII，所以先 base64
    exp = int(time.time()) + USER_TTL
    payload = f"{_b64(username)}.{exp}"
    sig = hmac.new(server_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    return f"{payload}.{sig}"


def user_from_token(token: str) -> str:
    """从登录票里解出用户名；伪造或过期返回空串。"""
    if not token or token.count(".") < 2:
        return ""
    try:
        name_b64, exp, sig = token.rsplit(".", 2)
        if int(exp) < time.time():
            return ""
        username = _unb64(name_b64)
    except (ValueError, UnicodeDecodeError):
        return ""
    payload = f"{name_b64}.{exp}"
    expect = hmac.new(server_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    if not secrets.compare_digest(sig, expect):
        return ""
    return username if username in load_users() else ""


# ---------------------------------------------------------------- 校验

def _text(value, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _find_game(games: list, game_id: str):
    for g in games:
        if g.get("id") == game_id:
            return g
    return None


def validate_game(payload: dict, published: dict) -> tuple[dict | None, str]:
    """用户提交一个「新游戏分区」。角色可以一起带上。"""
    name = normalize_name(_text(payload.get("gameName"), 40))
    note = _text(payload.get("note"), 120)
    submitter = _text(payload.get("submitter"), 40)

    if not (2 <= len(name) <= 30):
        return None, "游戏名 2–30 字"
    if any(not c.isprintable() for c in name):
        return None, "游戏名里有奇怪的字符"

    # 已有同名分区（不分大小写）就不让提交
    for g in published["games"]:
        if normalize_name(g.get("name", "")).casefold() == name.casefold():
            return None, "这个分区已经有了"

    # 角色：一行一个 / 逗号 / 顿号都行
    raw = payload.get("chars")
    chars = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = re.split(r"[,，、\n\r]+", raw)
    else:
        items = []
    for item in items:
        c = normalize_name(_text(item, 30))
        if 1 <= len(c) <= 20 and c not in chars:
            chars.append(c)
        if len(chars) >= 60:
            break

    # 同一个分区名已经在队列里等着审，别再塞
    return {
        "type": "game",
        "gameName": name,
        "chars": chars,
        "note": note,
        "submitter": submitter,
    }, ""


def validate_char(payload: dict, published: dict) -> tuple[dict | None, str]:
    """用户申请「在某个分区下加一个新角色」。"""
    game_id = _text(payload.get("gameId"), 60)
    name = normalize_name(_text(payload.get("charName"), 30))
    note = _text(payload.get("note"), 120)
    submitter = _text(payload.get("submitter"), 40)

    game = _find_game(published["games"], game_id)
    if not game:
        return None, "游戏分区不存在"
    if not (1 <= len(name) <= 20):
        return None, "角色名 1–20 字"
    if any(not c.isprintable() for c in name):
        return None, "角色名里有奇怪的字符"
    if any(normalize_name(c).casefold() == name.casefold() for c in game.get("chars", [])):
        return None, "这个分区里已经有这个角色了"

    return {
        "type": "char",
        "gameId": game_id,
        "gameName": game.get("name", ""),
        "charName": name,
        "note": note,
        "submitter": submitter,
    }, ""


def validate_link(payload: dict, published: dict) -> tuple[dict | None, str]:
    """把一条提交洗干净。返回 (干净数据, 错误信息)。"""
    game_id = _text(payload.get("gameId"), 60)
    char = _text(payload.get("charName"), 60)
    url = _text(payload.get("url"), 500)
    platform = _text(payload.get("platform"), 40) or "其他"
    link_kind = payload.get("linkKind") if payload.get("linkKind") in ("official", "fan") else "fan"
    note = _text(payload.get("note"), 120)
    submitter = _text(payload.get("submitter"), 40)

    if not URL_RE.match(url):
        return None, "地址不合法，要 http:// 或 https:// 开头"
    game = _find_game(published["games"], game_id)
    if not game:
        return None, "游戏分区不存在"
    if char not in game.get("chars", []):
        return None, "这个分区里没有这个角色"

    for existing in published["links"].get(game_id, {}).get(char, []):
        if existing.get("url") == url:
            return None, "这个地址已经收录过了"

    return {
        "type": "link",
        "gameId": game_id,
        "charName": char,
        "url": url,
        "platform": platform,
        "linkKind": link_kind,
        "note": note,
        "submitter": submitter,
    }, ""


# ---------------------------------------------------------------- 应用

def apply_submission(item: dict, published: dict) -> tuple[bool, str]:
    p = item.get("payload", {})
    kind = p.get("type")

    if kind == "link":
        gid, char = p["gameId"], p["charName"]
        published["links"].setdefault(gid, {}).setdefault(char, [])
        for existing in published["links"][gid][char]:
            if existing.get("url") == p["url"]:
                return False, "已存在"
        published["links"][gid][char].append({
            "url": p["url"],
            "platform": p["platform"],
            "note": p["note"],
            "kind": p["linkKind"],
        })
        published["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        return True, ""

    if kind == "char":
        gid, name = p.get("gameId"), p.get("charName")
        game = _find_game(published["games"], gid)
        if not game:
            return False, "分区不存在"
        if any(normalize_name(c).casefold() == normalize_name(name).casefold()
               for c in game.get("chars", [])):
            return False, "这个角色已经有了"
        game["chars"].append(name)
        published["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        return True, ""

    if kind == "game":
        name = p.get("gameName", "")
        if not name:
            return False, "分区名是空的"
        # 审核期间可能已经有人建了同名分区
        for g in published["games"]:
            if normalize_name(g.get("name", "")).casefold() == normalize_name(name).casefold():
                return False, "这个分区已经有了"
        gid = "g" + secrets.token_hex(3)
        published["games"].append({
            "id": gid,
            "name": name,
            "color": PALETTE[len(published["games"]) % len(PALETTE)],
            "chars": list(p.get("chars", [])),
        })
        published["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        return True, ""

    return False, f"未知类型 {kind}"


# ---------------------------------------------------------------- 速率

def rate_ok(ip: str) -> bool:
    now = time.time()
    stamps = [t for t in RATE.get(ip, []) if now - t < RATE_WINDOW]
    if len(stamps) >= RATE_LIMIT:
        RATE[ip] = stamps
        return False
    stamps.append(now)
    RATE[ip] = stamps
    return True


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "MMDHub/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write(f"[{datetime.now():%H:%M:%S}] {self.address_string()} {fmt % args}\n")

    # -- 工具 --
    def _json(self, obj, status=HTTPStatus.OK, head_only: bool = False):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(raw)

    def _file(self, path: Path, head_only: bool = False):
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        raw = path.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".xml": "application/xml; charset=utf-8",
            ".md": "text/plain; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(raw)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _is_really_local(self) -> bool:
        """真的是本机直连（不是走隧道/反代进来的）。"""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        return not any(self.headers.get(h) for h in
                       ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"))

    def _is_admin(self, query: dict, body: dict | None = None) -> bool:
        """认「登录票」，其次认本机直连时的静态口令（手机丢了能救急）。"""
        body = body or {}
        cred = (self.headers.get("X-Admin-Session")
                or (query.get("s") or [""])[0]
                or body.get("session", "")
                or "")
        if cred and session_ok(cred):
            return True

        static = (body.get("token", "")
                  or (query.get("token") or [""])[0]
                  or self.headers.get("X-Admin-Token", ""))
        if static and self._is_really_local() and secrets.compare_digest(static, admin_token()):
            return True
        return False

    def _client_ip(self) -> str:
        """真实访客 IP。放在 nginx / Cloudflare 后面时，直连地址是 127.0.0.1，
        得读转发头，否则限流会按"所有人都是同一个 IP"来算。"""
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1", "localhost"):
            for header in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
                value = self.headers.get(header)
                if value:
                    return value.split(",")[0].strip()[:64]
        return peer

    # -- 路由 --
    def do_HEAD(self):
        """有些监控、链接预览、爬虫用 HEAD。不实现会返回 501，看起来像网站挂了。"""
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._file(ROOT / "index.html", head_only=True)
            return
        if path == "/api/data":
            self._json(load_published(), head_only=True)
            return
        if path.startswith("/api/"):
            self._json({"ok": True}, head_only=True)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._file(ROOT / "index.html")
            return
        if path == "/api/data":
            published = load_published()
            self._json(published)
            return
        if path == "/api/pending":
            if not self._is_admin(query):
                self._json({"error": "口令不对"}, HTTPStatus.UNAUTHORIZED)
                return
            self._json({
                "pending": list(reversed(load_pending())),
                "published_at": load_published().get("updatedAt"),
            })
            return
        if path == "/api/events":
            limit = 60
            try:
                if query.get("limit"):
                    limit = max(1, min(200, int(query["limit"][0])))
            except (ValueError, IndexError):
                pass
            self._json({"events": load_events()[:limit]})
            return

        if path == "/api/stats":
            pending = load_pending()
            self._json({"pending": len(pending), "published": load_published().get("updatedAt")})
            return

        # IndexNow 的验证文件：必须是 <key>.txt，内容是 key 本身
        key = indexnow_key()
        if path == f"/{key}.txt":
            raw = key.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/account/me":
            token = self.headers.get("X-User-Token", "")
            username = user_from_token(token)
            if not username:
                self._json({"username": "", "favs": {"folders": [dict(DEFAULT_FOLDER)], "items": []}}, HTTPStatus.OK)
                return
            self._json({
                "username": username,
                "favs": normalize_favs(load_favs().get(username)),
            })
            return

        # 静态文件（截图、README 之类）
        safe = (ROOT / path.lstrip("/")).resolve()
        if str(safe).startswith(str(ROOT)) and safe.is_file():
            self._file(safe)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ---------------- 普通用户：注册 / 登录 / 收藏 ----------------
        if path in ("/api/account/register", "/api/account/login"):
            ip = self._client_ip()
            body = self._body()
            username = normalize_name(_text(body.get("username"), 20))
            password = body.get("password") if isinstance(body.get("password"), str) else ""

            if not USERNAME_RE.match(username):
                self._json({"error": "用户名 2–20 个字，只能是中文、字母、数字、_ - ·"},
                           HTTPStatus.BAD_REQUEST)
                return
            if username.casefold() in RESERVED_NAMES:
                self._json({"error": "这个用户名留给官方用，换一个吧"}, HTTPStatus.BAD_REQUEST)
                return
            if len(password) < 6:
                self._json({"error": "密码至少 6 位"}, HTTPStatus.BAD_REQUEST)
                return

            if path.endswith("register"):
                if login_locked(ip):
                    self._json({"error": "操作太频繁，过一小时再来"}, HTTPStatus.TOO_MANY_REQUESTS)
                    return
                now = time.time()
                recent = [t for t in USER_RATE.get(ip, []) if now - t < 3600]
                if len(recent) >= USER_RATE_LIMIT:
                    self._json({"error": "这个网络注册太频繁了（每小时最多 20 个），过一会儿再试"},
                               HTTPStatus.TOO_MANY_REQUESTS)
                    return
                USER_RATE[ip] = recent + [now]
                with LOCK:
                    users = load_users()
                    # 不区分大小写、不看全角半角，都算重名
                    if find_user(users, username):
                        self._json({"error": f"「{username}」已经有人用了（不分大小写），换一个吧"},
                                   HTTPStatus.CONFLICT)
                        return
                    salt, digest = hash_password(password)
                    users[username] = {
                        "salt": salt, "hash": digest,
                        "created": datetime.now().isoformat(timespec="seconds"),
                    }
                    _write(USERS, users)
                self._json({
                    "ok": True,
                    "username": username,
                    "token": make_user_token(username),
                    "favs": {"folders": [dict(DEFAULT_FOLDER)], "items": []},
                })
                return

            # 登录
            if login_locked(ip):
                self._json({"error": "错太多次了，过一小时再来"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            users = load_users()
            stored = find_user(users, username)
            rec = users.get(stored) if stored else None
            if not rec or not check_password(password, rec.get("salt", ""), rec.get("hash", "")):
                login_fail(ip)
                self._json({"error": "用户名或密码不对"}, HTTPStatus.UNAUTHORIZED)
                return
            self._json({
                "ok": True,
                "username": stored,
                "token": make_user_token(stored),
                "favs": normalize_favs(load_favs().get(stored)),
            })
            return

        if path == "/api/account/favs":
            username = user_from_token(self.headers.get("X-User-Token", ""))
            if not username:
                self._json({"error": "请先登录"}, HTTPStatus.UNAUTHORIZED)
                return
            body = self._body()
            raw = body.get("favs")
            if not isinstance(raw, (list, dict)):
                self._json({"error": "格式不对"}, HTTPStatus.BAD_REQUEST)
                return
            clean = normalize_favs(raw)
            with LOCK:
                favs = load_favs()
                favs[username] = clean
                _write(FAVS, favs)
            self._json({"ok": True,
                        "count": len(clean["items"]),
                        "folders": len(clean["folders"])})
            return

        if path == "/api/submit":
            ip = self._client_ip()
            if not rate_ok(ip):
                self._json({"error": "提交太频繁，歇一会儿再来"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            body = self._body()
            with LOCK:
                published = load_published()
                # type=game 提交新分区 / type=char 提交新角色 / 其余按配布地址处理
                sub_type = body.get("type")
                if sub_type == "game":
                    clean, err = validate_game(body, published)
                elif sub_type == "char":
                    clean, err = validate_char(body, published)
                else:
                    clean, err = validate_link(body, published)
                if err:
                    self._json({"error": err}, HTTPStatus.BAD_REQUEST)
                    return
                pending = load_pending()
                # 同样的东西已经在队列里等着审的，别再进来一条
                for old in pending:
                    op = old.get("payload", {})
                    if (clean["type"] == "game"
                            and op.get("type") == "game"
                            and normalize_name(op.get("gameName", "")).casefold()
                            == normalize_name(clean["gameName"]).casefold()):
                        self._json({"error": "这个分区已经在审核队列里了"}, HTTPStatus.CONFLICT)
                        return
                    if (clean["type"] == "char"
                            and op.get("type") == "char"
                            and op.get("gameId") == clean["gameId"]
                            and normalize_name(op.get("charName", "")).casefold()
                            == normalize_name(clean["charName"]).casefold()):
                        self._json({"error": "这个角色已经在审核队列里了"}, HTTPStatus.CONFLICT)
                        return
                    if (clean["type"] == "link"
                            and op.get("type") == "link"
                            and op.get("gameId") == clean["gameId"]
                            and op.get("charName") == clean["charName"]
                            and op.get("url") == clean["url"]):
                        self._json({"error": "这条已经在审核队列里了"}, HTTPStatus.CONFLICT)
                        return
                item = {
                    "id": secrets.token_hex(6),
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "ip": ip,
                    "status": "pending",
                    "payload": clean,
                }
                pending.append(item)
                _write(PENDING, pending)
            self._json({"ok": True, "id": item["id"], "message": "已提交，等站长审核"})
            return

        if path == "/api/login":
            ip = self._client_ip()
            if login_locked(ip):
                self._json({"error": "错太多次了，过一小时再来"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            body = self._body()
            if totp_verify(totp_secret(), _text(body.get("code"), 12)):
                sid, expires = new_session()
                self._json({"session": sid, "expires": expires})
                return
            static = _text(body.get("token"), 90)
            if static and self._is_really_local() and secrets.compare_digest(static, admin_token()):
                sid, expires = new_session()
                self._json({"session": sid, "expires": expires})
                return
            login_fail(ip)
            self._json({"error": "动态码不对（或已过期）"}, HTTPStatus.UNAUTHORIZED)
            return

        if path == "/api/admin/mutate":
            body = self._body()
            if not self._is_admin({}, body):
                self._json({"error": "登录失效，请重新输入动态码"}, HTTPStatus.UNAUTHORIZED)
                return
            op = body.get("op")
            with LOCK:
                published = load_published()
                games = published["games"]
                links = published["links"]
                err = ""
                desc = ""

                if op == "add_game":
                    name = _text(body.get("name"), 40)
                    if not name:
                        err = "名字不能为空"
                    elif any(g.get("name") == name for g in games):
                        err = "已经有同名分区了"
                    else:
                        games.append({
                            "id": "g" + secrets.token_hex(3),
                            "name": name,
                            "color": PALETTE[len(games) % len(PALETTE)],
                            "chars": [],
                        })
                        desc = f"新增分区「{name}」"

                elif op == "del_game":
                    gid = _text(body.get("gameId"), 60)
                    gone = _find_game(games, gid)
                    before = len(games)
                    published["games"] = [g for g in games if g.get("id") != gid]
                    if gone:
                        desc = f"删除分区「{gone.get('name')}」"
                    if len(published["games"]) == before:
                        err = "分区不存在"
                    else:
                        links.pop(gid, None)

                elif op == "add_char":
                    gid = _text(body.get("gameId"), 60)
                    name = _text(body.get("name"), 60)
                    game = _find_game(games, gid)
                    if not game:
                        err = "分区不存在"
                    elif not name:
                        err = "名字不能为空"
                    elif name in game["chars"]:
                        err = "这个角色已经有了"
                    else:
                        game["chars"].append(name)
                        desc = f"「{game.get('name')}」新增角色「{name}」"

                elif op == "del_char":
                    gid = _text(body.get("gameId"), 60)
                    name = _text(body.get("name"), 60)
                    game = _find_game(games, gid)
                    if not game:
                        err = "分区不存在"
                    elif name not in game["chars"]:
                        err = "角色不存在"
                    else:
                        game["chars"].remove(name)
                        if gid in links:
                            links[gid].pop(name, None)
                        desc = f"「{game.get('name')}」删除角色「{name}」"

                elif op == "rename_game":
                    gid = _text(body.get("gameId"), 60)
                    new_name = normalize_name(_text(body.get("name"), 40))
                    game = _find_game(games, gid)
                    if not game:
                        err = "分区不存在"
                    elif not (2 <= len(new_name) <= 30):
                        err = "分区名 2–30 字"
                    elif any(g.get("id") != gid
                             and normalize_name(g.get("name", "")).casefold() == new_name.casefold()
                             for g in games):
                        err = "已经有同名分区了"
                    else:
                        old_display = game.get("name")
                        game["name"] = new_name
                        desc = f"分区改名「{old_display}」→「{new_name}」"

                elif op == "rename_char":
                    gid = _text(body.get("gameId"), 60)
                    old_name = _text(body.get("oldName"), 60)
                    new_name = normalize_name(_text(body.get("newName"), 30))
                    game = _find_game(games, gid)
                    if not game:
                        err = "分区不存在"
                    elif old_name not in game["chars"]:
                        err = "角色不存在"
                    elif not (1 <= len(new_name) <= 20):
                        err = "角色名 1–20 字"
                    elif any(normalize_name(c).casefold() == new_name.casefold() and c != old_name
                             for c in game["chars"]):
                        err = "这个分区里已经有同名角色了"
                    else:
                        game["chars"] = [new_name if c == old_name else c for c in game["chars"]]
                        # 配布地址跟着改名走，不然就成孤儿了
                        if gid in links and old_name in links[gid]:
                            links[gid][new_name] = links[gid].pop(old_name)
                        # 用户的收藏也一起迁过去（新旧两种结构都兼容）
                        favs = load_favs()
                        touched = False
                        old_key, new_key = f"{gid}/{old_name}", f"{gid}/{new_name}"
                        for user, value in favs.items():
                            if isinstance(value, list):
                                if old_key in value:
                                    favs[user] = [new_key if x == old_key else x for x in value]
                                    touched = True
                                continue
                            data = normalize_favs(value)
                            hit = False
                            for it in data["items"]:
                                if it.get("type") == "char" and it.get("key") == "c:" + old_key:
                                    it["key"] = "c:" + new_key
                                    it["charName"] = new_name
                                    hit = True
                            if hit:
                                favs[user] = data
                                touched = True
                        if touched:
                            _write(FAVS, favs)
                        desc = f"「{game.get('name')}」角色改名「{old_name}」→「{new_name}」"

                elif op == "import":
                    new_games = body.get("games")
                    new_links = body.get("links")
                    if not isinstance(new_games, list) or not new_games:
                        err = "分区数据不合法"
                    else:
                        published["games"] = new_games
                        published["links"] = new_links if isinstance(new_links, dict) else {}
                        desc = f"导入数据（{len(new_games)} 个分区）"

                elif op == "add_link":
                    gid = _text(body.get("gameId"), 60)
                    char = _text(body.get("charName"), 60)
                    url = _text(body.get("url"), 500)
                    game = _find_game(games, gid)
                    if not game:
                        err = "分区不存在"
                    elif char not in game.get("chars", []):
                        err = "这个分区里没有这个角色"
                    elif not URL_RE.match(url):
                        err = "地址不合法"
                    else:
                        arr = links.setdefault(gid, {}).setdefault(char, [])
                        if any(x.get("url") == url for x in arr):
                            err = "这条地址已经收录过了"
                        else:
                            arr.append({
                                "url": url,
                                "platform": _text(body.get("platform"), 40) or "其他",
                                "note": _text(body.get("note"), 120),
                                "kind": "official" if body.get("kind") == "official" else "fan",
                            })
                            desc = f"「{game.get('name')}·{char}」新增地址"

                elif op in ("del_link", "set_link_kind"):
                    gid = _text(body.get("gameId"), 60)
                    char = _text(body.get("charName"), 60)
                    url = _text(body.get("url"), 500)
                    arr = (links.get(gid) or {}).get(char) or []
                    hit = next((x for x in arr if x.get("url") == url), None)
                    if hit is None:
                        err = "没找到这条地址"
                    elif op == "del_link":
                        arr.remove(hit)
                        desc = f"「{char}」删除地址"
                    else:
                        hit["kind"] = "official" if body.get("kind") == "official" else "fan"

                else:
                    err = f"未知操作 {op}"

                if err:
                    self._json({"error": err}, HTTPStatus.BAD_REQUEST)
                    return
                published["updatedAt"] = datetime.now().isoformat(timespec="seconds")
                _write(PUBLISHED, published)
                if desc:
                    log_event(desc, "edit")
            ping_async()
            self._json({"ok": True, "games": published["games"], "links": published["links"]})
            return

        if path == "/api/decide":
            body = self._body()
            if not self._is_admin({}, body):
                self._json({"error": "登录失效，请重新输入动态码"}, HTTPStatus.UNAUTHORIZED)
                return
            item_id = _text(body.get("id"), 40)
            action = body.get("action")
            if action not in ("approve", "reject"):
                self._json({"error": "action 只能是 approve / reject"}, HTTPStatus.BAD_REQUEST)
                return
            with LOCK:
                pending = load_pending()
                item = next((x for x in pending if x.get("id") == item_id), None)
                if not item:
                    self._json({"error": "这条不在队列里（可能已经处理过）"}, HTTPStatus.NOT_FOUND)
                    return
                published = load_published()
                if action == "approve":
                    ok, msg = apply_submission(item, published)
                    if not ok:
                        pending = [x for x in pending if x.get("id") != item_id]
                        _write(PENDING, pending)
                        self._json({"error": msg or "无法应用"}, HTTPStatus.BAD_REQUEST)
                        return
                    _write(PUBLISHED, published)
                    pending = [x for x in pending if x.get("id") != item_id]
                    _write(PENDING, pending)
                    pl = item.get("payload", {})
                    if pl.get("type") == "game":
                        log_event(f"通过申请 · 新分区「{pl.get('gameName')}」", "approve")
                    elif pl.get("type") == "char":
                        log_event(f"通过申请 · 新角色「{pl.get('charName')}」", "approve")
                    else:
                        log_event(f"通过申请 · 「{pl.get('charName')}」新增一条地址", "approve")
                    ping_async()
                    self._json({"ok": True, "applied": item["payload"]})
                else:
                    pending = [x for x in pending if x.get("id") != item_id]
                    _write(PENDING, pending)
                    rejected = load_rejected()
                    rejected.append({**item, "status": "rejected"})
                    _write(REJECTED, rejected[-500:])
                    self._json({"ok": True})
            return

        self.send_error(HTTPStatus.NOT_FOUND)


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 8000
    host = argv[2] if len(argv) > 2 else "127.0.0.1"

    DATA.mkdir(parents=True, exist_ok=True)
    if not PUBLISHED.is_file():
        _write(PUBLISHED, {"games": [], "links": {}, "updatedAt": None})

    token = admin_token()
    secret = totp_secret()
    in_key = indexnow_key()          # 启动时就生成，方便备案/验证
    if not EVENTS.is_file():
        _pub = load_published()
        _chars = sum(len(g.get("chars", [])) for g in _pub.get("games", []))
        log_event(f"御北的小站上线 · {len(_pub.get('games', []))} 个分区 / {_chars} 个角色", "info")
    pretty = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))

    server = ThreadingHTTPServer((host, port), Handler)
    print("=" * 62)
    print("  二游 MMD 模型导航 · 带审核的后端")
    print(f"  地址    http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    print(f"  监听    {host}:{port}")
    print("-" * 62)
    print("  管理员登录：动态口令（30 秒变一次）")
    print("  第一次用，请在手机的 Authenticator 里「手动输入密钥」：")
    print()
    print(f"      {pretty}")
    print()
    print("  或者把下面这行整段粘进验证器：")
    print(f"      {totp_uri(secret)}")
    print()
    print(f"  （密钥保存在 {TOTP_FILE}，别外发）")
    print("-" * 62)
    print(f"  本机直连的静态口令兜底：{token}")
    print("  —— 只在「本机 + 不是走隧道」时有效，手机丢了用它救急")
    print("=" * 62)
    print(f"  IndexNow 验证文件：{SITE_URL}{in_key}.txt")
    print(f"  百度推送：{'已配置' if BAIDU_TOKEN_FILE.is_file() else '未配置（可选，放 data/baidu.token）'}")
    print("  停止    Ctrl+C")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
