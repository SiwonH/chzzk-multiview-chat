import gzip
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP_VERSION = "v29"
GITHUB_REPO = "SiwonH/chzzk-multiview-chat"
GITHUB_ASSET_NAME = "chzzk_chat.py"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=15"
GITHUB_LATEST_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
GITHUB_VERSIONS_JSON = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/versions.json"
GITHUB_HEADERS = {
    "User-Agent": "chzzk-multiview-chat-updater",
    "Accept": "application/vnd.github+json",
}

SEARCH_URL = "https://api.chzzk.naver.com/service/v1/search/channels"
CHANNEL_INFO_URL = "https://api.chzzk.naver.com/service/v1/channels/{}"
LIVE_DETAIL_URL = "https://api.chzzk.naver.com/service/v3/channels/{}/live-detail"
STATUS_URL = "https://api.chzzk.naver.com/polling/v2/channels/{}/live-status"
TOKEN_URL = "https://comm-api.game.naver.com/nng_main/v1/chats/access-token?channelId={}&chatType=STREAMING"

CHANNEL_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
CHZZK_URL_RE = re.compile(
    r"(?:https?://)?(?:m\.)?chzzk\.naver\.com/(?:live/)?([0-9a-fA-F]{32})(?:[/?#]|$)",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chzzk_history.json")
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chzzk_cookies.json")
MAX_HISTORY = 9
history_lock = threading.Lock()
cookie_lock = threading.Lock()
INITIAL_CHANNELS = []

def load_cookies():
    """저장된 네이버 로그인 쿠키(NID_AUT, NID_SES)를 불러옵니다."""
    with cookie_lock:
        if not os.path.exists(COOKIES_FILE):
            return {"NID_AUT": "", "NID_SES": ""}
        try:
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"NID_AUT": "", "NID_SES": ""}
            return {
                "NID_AUT": str(data.get("NID_AUT") or "").strip(),
                "NID_SES": str(data.get("NID_SES") or "").strip(),
            }
        except Exception:
            return {"NID_AUT": "", "NID_SES": ""}

def save_cookies(nid_aut, nid_ses):
    """네이버 로그인 쿠키를 로컬 JSON 파일에 저장합니다."""
    cleaned = {
        "NID_AUT": str(nid_aut or "").strip(),
        "NID_SES": str(nid_ses or "").strip(),
    }
    with cookie_lock:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)

def clear_cookies():
    """저장된 쿠키 파일을 삭제합니다."""
    with cookie_lock:
        if os.path.exists(COOKIES_FILE):
            os.remove(COOKIES_FILE)

def get_cookie_status():
    """프론트/CLI 표시용 쿠키 설정 여부 요약을 반환합니다."""
    cookies = load_cookies()
    aut = cookies.get("NID_AUT", "")
    ses = cookies.get("NID_SES", "")
    has_cookie = bool(aut and ses)
    preview = (aut[:4] + "****" + aut[-4:]) if len(aut) > 8 else ("설정됨" if aut else "")
    return {"hasCookie": has_cookie, "nidAutPreview": preview}

def sanitize_channels(channels):
    """채널 목록에서 중복과 잘못된 항목을 정리해 최대 6개까지 반환합니다."""
    if not isinstance(channels, list):
        return []
    cleaned = []
    seen = set()
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        cname = str(ch.get("name") or "").strip()
        if cid and cname and cid not in seen:
            seen.add(cid)
            cleaned.append({"id": cid, "name": cname})
        if len(cleaned) >= 6:
            break
    return cleaned

def load_history():
    """저장된 최근 스트리머 조합 목록을 불러옵니다."""
    with history_lock:
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            result = []
            for entry in data:
                cleaned = sanitize_channels(entry)
                if cleaned:
                    result.append(cleaned)
                if len(result) >= MAX_HISTORY:
                    break
            return result
        except Exception:
            return []

def save_history(history):
    """기록 목록을 JSON 파일에 저장합니다."""
    with history_lock:
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history[:MAX_HISTORY], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def record_history_entry(channels):
    """선택/편성한 채널 조합을 기록 맨 앞(1번)에 저장합니다."""
    cleaned = sanitize_channels(channels)
    if not cleaned:
        return
    key = tuple(ch["id"] for ch in cleaned)
    history = load_history()
    filtered = [entry for entry in history if tuple(ch["id"] for ch in entry) != key]
    save_history([cleaned] + filtered)

def request_json(url, use_cookies=False):
    headers = dict(HEADERS)
    if use_cookies:
        cookies = load_cookies()
        parts = []
        if cookies.get("NID_AUT"):
            parts.append(f"NID_AUT={cookies['NID_AUT']}")
        if cookies.get("NID_SES"):
            parts.append(f"NID_SES={cookies['NID_SES']}")
        if parts:
            headers["Cookie"] = "; ".join(parts)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)

def extract_channel_id(text):
    """입력이 32자리 채널 ID 또는 치지직 채널/방송 URL이면 channelId를 반환합니다."""
    raw = (text or "").strip()
    if CHANNEL_ID_RE.match(raw):
        return raw.lower()
    m = CHZZK_URL_RE.search(raw)
    if m:
        return m.group(1).lower()
    return None

def parse_channel_summary(ch):
    """치지직 채널 응답 객체에서 공통 요약 딕셔너리를 생성합니다."""
    if not isinstance(ch, dict):
        return None
    c_id = ch.get("channelId")
    c_name = ch.get("channelName")
    if not c_id or not c_name:
        return None
    return {
        "id": c_id,
        "name": c_name,
        "openLive": bool(ch.get("openLive", False)),
        "followerCount": int(ch.get("followerCount") or 0)
    }

def get_channel_by_id(channel_id):
    """채널 ID로 단일 채널 정보를 직접 조회합니다."""
    try:
        data = request_json(CHANNEL_INFO_URL.format(channel_id), use_cookies=True)
        return parse_channel_summary(data.get("content"))
    except Exception:
        return None

def search_channels(keyword, size=6):
    """치지직 API에서 이름, 채널 ID 또는 치지직 URL로 채널 목록을 조회합니다."""
    if not keyword:
        return []
    direct_id = extract_channel_id(keyword)
    if direct_id:
        ch = get_channel_by_id(direct_id)
        return [ch] if ch else []
    try:
        q = urllib.parse.urlencode({"keyword": keyword, "offset": 0, "size": size})
        data = request_json(f"{SEARCH_URL}?{q}", use_cookies=True)
        rows = data.get("content", {}).get("data", [])
        results = []
        for row in rows:
            item = parse_channel_summary(row.get("channel"))
            if item:
                results.append(item)
        return results
    except Exception:
        return []

def find_channel_id(keyword):
    """이름, 채널 ID 또는 치지직 URL과 가장 잘 맞는 단일 채널 ID를 반환합니다."""
    results = search_channels(keyword, size=3)
    if not results:
        return None, None
    for ch in results:
        if "".join(keyword.split()).lower() == "".join(ch["name"].split()).lower():
            return ch["id"], ch["name"]
    return results[0]["id"], results[0]["name"]

def get_live_status(channel_id):
    """방종 상태에서 방송 시작 여부만 1회 호출로 가볍게 확인합니다."""
    try:
        status_data = request_json(STATUS_URL.format(channel_id), use_cookies=True)
        content = status_data.get("content") or {}
        return {
            "chatChannelId": content.get("chatChannelId"),
            "status": content.get("status", "UNKNOWN")
        }
    except Exception as e:
        return {"error": str(e)}

def get_chat_credentials(channel_id):
    """채널 ID로 chatChannelId와 읽기 전용 accessToken을 발급받습니다."""
    try:
        status_data = request_json(STATUS_URL.format(channel_id), use_cookies=True)
        content = status_data.get("content") or {}
        chat_channel_id = content.get("chatChannelId")
        live_status = content.get("status", "UNKNOWN")

        if not chat_channel_id:
            return {"error": "채팅 채널 ID를 찾을 수 없습니다."}

        token_data = request_json(TOKEN_URL.format(chat_channel_id), use_cookies=True)
        access_token = (token_data.get("content") or {}).get("accessToken")

        if not access_token:
            return {"error": "채팅 토큰 발급에 실패했습니다."}

        return {
            "chatChannelId": chat_channel_id,
            "accessToken": access_token,
            "status": live_status
        }
    except Exception as e:
        return {"error": str(e)}

def get_stream_info(channel_id):
    """채널 ID로 방송 상세 정보와 저지연 LLHLS(또는 HLS) 스트림 URL을 조회합니다."""
    try:
        detail_data = request_json(LIVE_DETAIL_URL.format(channel_id), use_cookies=True)
        content = detail_data.get("content") or {}
        status = content.get("status", "UNKNOWN")
        title = content.get("liveTitle") or ""
        category = content.get("liveCategoryValue") or ""
        user_count = int(content.get("concurrentUserCount") or 0)
        adult = bool(content.get("adult", False))

        if status != "OPEN":
            return {"status": status, "error": "현재 방송 중이 아닙니다."}

        playback_raw = content.get("livePlaybackJson")
        if not playback_raw:
            msg = "성인 인증이 필요한 방송입니다. (🔑 쿠키 설정 필요)" if adult else "재생 가능한 스트림 주소를 찾지 못했습니다."
            return {"status": status, "error": msg}

        playback = json.loads(playback_raw) if isinstance(playback_raw, str) else playback_raw
        media_list = (playback or {}).get("media") or []

        llhls_url = None
        hls_url = None
        for m in media_list:
            mid = m.get("mediaId")
            path = m.get("path")
            if not path:
                continue
            if mid == "LLHLS" and not llhls_url:
                llhls_url = path
            elif mid == "HLS" and not hls_url:
                hls_url = path
            elif m.get("protocol") == "HLS" and not hls_url:
                hls_url = path

        primary_url = llhls_url or hls_url
        if not primary_url:
            return {"status": status, "error": "HLS 스트림 주소를 찾지 못했습니다."}

        return {
            "status": status,
            "title": title,
            "category": category,
            "userCount": user_count,
            "hlsUrl": primary_url,
            "hlsFallbackUrl": hls_url if (llhls_url and hls_url and hls_url != llhls_url) else None,
            "isLowLatency": bool(llhls_url),
        }
    except Exception as e:
        return {"error": str(e)}

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>치지직 단일 창 멀티뷰 채팅</title>
    <style>
        :root {
            --chat-font-size: 20px;
        }
        * { box-sizing: border-box; }
        html, body {
            margin: 0; padding: 0; width: 100%; height: 100%;
            background-color: #0b0b0e; color: #f4f4f6;
            font-family: 'Malgun Gothic', -apple-system, sans-serif;
            overflow: hidden;
        }

        /* 전체 화면 멀티뷰 그리드 */
        .wrapper {
            display: grid;
            grid-template-columns: repeat(1, 1fr);
            width: 100vw;
            height: 100vh;
            overflow: hidden;
        }
        .empty-screen {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            gap: 12px;
            color: #a1a1aa;
            font-size: 14px;
        }
        .chat-box {
            display: flex;
            flex-direction: column;
            height: 100vh;
            border-right: 1px solid #222228;
            min-width: 0;
            min-height: 0;
            overflow: hidden;
            position: relative;
            contain: content;
        }
        .chat-box:last-child { border-right: none; }

        /* 스트리머 개별 헤더 */
        .header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 7px 8px; background-color: #141418;
            border-bottom: 2px solid #00ffa3; flex-shrink: 0;
            gap: 4px;
        }
        .streamer-name {
            margin: 0; font-size: 14px; font-weight: bold; color: #fff;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            cursor: pointer; padding: 1px 4px; border-radius: 4px;
            transition: background-color 0.15s, color 0.15s;
        }
        .streamer-name:hover {
            color: #00ffa3;
            background-color: rgba(0, 255, 163, 0.1);
        }
        .header-controls { display: flex; align-items: center; gap: 3px; flex-shrink: 0; }
        .global-header-tools {
            position: fixed;
            top: 58px;
            right: 0;
            z-index: 90;
            display: inline-flex;
            flex-direction: row;
            align-items: stretch;
            height: 85px;
            box-sizing: border-box;
            padding: 0;
            border-radius: 10px 0 0 10px;
            background: rgba(20, 20, 28, 0.95);
            border: 1px solid rgba(0, 255, 163, 0.45);
            border-right: none;
            box-shadow: -4px 6px 18px rgba(0, 0, 0, 0.68);
            transition: background-color 0.2s, border-color 0.2s, box-shadow 0.2s;
        }
        .global-header-tools.is-collapsed {
            background: transparent;
            border: none;
            box-shadow: none;
        }
        .global-header-tools:not(.is-collapsed):hover,
        .global-header-tools:not(.is-collapsed):focus-within,
        .global-header-tools.intro-spotlight {
            background: rgba(22, 22, 30, 0.98);
            border-color: #00ffa3;
            box-shadow: -4px 8px 24px rgba(0, 0, 0, 0.78);
        }
        .global-header-tools.intro-spotlight {
            animation: toolsIntroPulse 1.4s ease-out 3;
        }
        @keyframes toolsIntroPulse {
            0% { box-shadow: 0 0 0 0 rgba(0, 255, 163, 0.6), -4px 8px 24px rgba(0, 0, 0, 0.78); }
            70% { box-shadow: 0 0 0 10px rgba(0, 255, 163, 0), -4px 8px 24px rgba(0, 0, 0, 0.78); }
            100% { box-shadow: 0 0 0 0 rgba(0, 255, 163, 0), -4px 8px 24px rgba(0, 0, 0, 0.78); }
        }
        .global-tools-expanded {
            display: inline-flex;
            align-items: stretch;
            height: 100%;
        }
        .global-tools-inner {
            display: flex;
            flex-direction: column;
            justify-content: center;
            gap: 5px;
            padding: 5px 7px;
            height: 100%;
            box-sizing: border-box;
        }
        .global-tools-section {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 4px;
            padding: 3px 4px;
            border-radius: 7px;
            background: rgba(255, 255, 255, 0.035);
            border: 1px solid rgba(255, 255, 255, 0.09);
            box-sizing: border-box;
        }
        .global-header-tools.is-collapsed .global-tools-expanded {
            display: none;
        }
        .btn-tools-handle {
            padding: 0;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            line-height: 1;
            cursor: pointer;
            transition: width 0.16s ease-out, min-width 0.16s ease-out, opacity 0.16s, background-color 0.16s, border-color 0.16s, color 0.16s, box-shadow 0.16s;
        }
        .btn-tools-expand-tab {
            display: none;
            width: 10px;
            min-width: 10px;
            height: 85px;
            box-sizing: border-box;
            border-radius: 8px 0 0 8px;
            background: rgba(22, 22, 30, 0.72);
            border: 1px solid rgba(255, 255, 255, 0.16);
            border-right: none;
            color: rgba(212, 212, 216, 0.75);
            font-size: 9px;
            opacity: 0.45;
            box-shadow: none;
        }
        .btn-tools-expand-tab:hover,
        .btn-tools-expand-tab.near-hover {
            width: 24px;
            min-width: 24px;
            opacity: 1;
            background: rgba(24, 26, 36, 0.98);
            border-color: #00ffa3;
            color: #00ffa3;
            font-size: 11px;
            box-shadow: -3px 4px 12px rgba(0, 0, 0, 0.6);
        }
        .global-header-tools.is-collapsed .btn-tools-expand-tab {
            display: inline-flex;
        }
        .btn-tools-collapse-tab {
            width: 14px;
            min-width: 14px;
            height: 100%;
            font-size: 10px;
            border-radius: 9px 0 0 9px;
            background: rgba(255, 255, 255, 0.04);
            border: none;
            border-right: 1px solid rgba(255, 255, 255, 0.14);
            color: #a1a1aa;
        }
        .btn-tools-collapse-tab:hover {
            width: 18px;
            min-width: 18px;
            background: rgba(0, 255, 163, 0.18);
            color: #00ffa3;
        }
        .global-header-tools .btn-mini {
            height: 26px;
            min-width: 26px;
            padding: 0 6px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 12.5px;
            border-radius: 5px;
            background: #2a2a34;
            border-color: #3f3f4e;
            color: #f4f4f6;
            transition: background-color 0.15s, border-color 0.15s, color 0.15s;
        }
        .global-header-tools .btn-mini.btn-tools-primary {
            color: #00ffa3;
            border-color: rgba(0, 255, 163, 0.52);
            padding: 0 8px;
            gap: 4px;
            font-weight: 600;
        }
        .global-header-tools .btn-mini:hover {
            background: #353542;
            border-color: #00ffa3;
            color: #00ffa3;
        }
        .global-header-tools .font-indicator {
            font-size: 13px;
            min-width: 24px;
            text-align: center;
            color: #f4f4f6;
            user-select: none;
        }
        .global-tools-hint {
            position: absolute;
            top: calc(100% + 12px);
            right: 12px;
            width: 286px;
            background: rgba(16, 18, 24, 0.98);
            color: #e4e4e7;
            border: 2px solid #00ffa3;
            border-radius: 12px;
            padding: 13px 15px;
            box-shadow: 0 12px 30px rgba(0, 0, 0, 0.85), 0 0 18px rgba(0, 255, 163, 0.25);
            pointer-events: none;
            opacity: 0;
            transform: translateY(-6px);
            transition: opacity 0.28s, transform 0.28s;
            cursor: pointer;
        }
        .global-tools-hint::before {
            content: "";
            position: absolute;
            top: -8px;
            right: 22px;
            width: 12px;
            height: 12px;
            background: rgba(16, 18, 24, 0.98);
            border-left: 2px solid #00ffa3;
            border-top: 2px solid #00ffa3;
            transform: rotate(45deg);
        }
        .global-tools-hint-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            font-size: 14px;
            font-weight: 800;
            color: #00ffa3;
        }
        .global-tools-hint-close {
            background: rgba(255, 255, 255, 0.08);
            color: #d4d4d8;
            border: 1px solid #3f3f4e;
            border-radius: 5px;
            padding: 1px 6px;
            font-size: 12px;
            line-height: 1.3;
            cursor: pointer;
        }
        .global-tools-hint-close:hover {
            border-color: #00ffa3;
            color: #00ffa3;
        }
        .global-tools-hint-body {
            margin-top: 6px;
            font-size: 12.5px;
            line-height: 1.5;
            color: #f4f4f6;
        }
        .global-tools-hint-sub {
            margin-top: 5px;
            font-size: 11.5px;
            line-height: 1.4;
            color: #9ca3af;
        }
        .global-header-tools.intro-spotlight .global-tools-hint {
            opacity: 1;
            transform: translateY(0);
            pointer-events: auto;
        }
        .status-dot {
            width: 7px; height: 7px; border-radius: 50%; background-color: #666;
            margin: 0 2px; flex-shrink: 0;
        }
        .status-dot.connected { background-color: #00ffa3; }
        .status-dot.reconnecting {
            background-color: #facc15;
            box-shadow: 0 0 0 2px rgba(250, 204, 21, 0.25);
        }
        .status-dot.error { background-color: #ff4d4d; }

        .btn-mini {
            background: #23232a; color: #d4d4d8; border: 1px solid #34343f;
            border-radius: 4px; padding: 2px 5px; font-size: 11px; cursor: pointer;
            line-height: 1.3;
        }
        .btn-mini:hover { background: #32323c; color: #00ffa3; border-color: #00ffa3; }
        .btn-close-col:hover { color: #ff6b6b; border-color: #ff6b6b; }

        /* 스트리머/매니저 칸 & 미니 영상 토글 버튼 상태 */
        .btn-pin-toggle { position: relative; }
        .btn-pin-toggle.active,
        .btn-video-toggle.active {
            border-color: rgba(0, 255, 163, 0.5);
            color: #00ffa3;
            background: rgba(0, 255, 163, 0.08);
        }
        .btn-pin-toggle.muted { opacity: 0.5; }

        /* 상단 미니 스트리밍 플레이어 */
        .video-panel {
            position: relative;
            width: 100%;
            aspect-ratio: 16 / 9;
            max-height: 42vh;
            background-color: #000;
            flex-shrink: 0;
            overflow: hidden;
            border-bottom: 1px solid #222228;
        }
        .video-panel.hidden { display: none !important; }
        .video-player {
            width: 100%;
            height: 100%;
            display: block;
            object-fit: contain;
            background-color: #000;
        }
        .video-msg {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 10px;
            text-align: center;
            font-size: 12px;
            color: #a1a1aa;
            background: rgba(11, 11, 14, 0.85);
            pointer-events: none;
        }
        .video-msg.hidden { display: none; }
        .video-overlay {
            position: absolute;
            left: 0;
            right: 0;
            bottom: 0;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 6px;
            padding: 14px 8px 6px;
            background: linear-gradient(transparent, rgba(0, 0, 0, 0.84));
            opacity: 0;
            transition: opacity 0.15s;
        }
        .video-panel:hover .video-overlay { opacity: 1; }
        .video-info-badge {
            font-size: 10px;
            color: #e4e4e7;
            background: rgba(20, 20, 26, 0.8);
            border: 1px solid #34343f;
            border-radius: 4px;
            padding: 2px 6px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            max-width: 55%;
        }
        .video-overlay-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
        .btn-video-ctrl {
            background: rgba(24, 24, 30, 0.9);
            color: #e4e4e7;
            border: 1px solid #3f3f4a;
            border-radius: 4px;
            padding: 2px 6px;
            font-size: 11px;
            cursor: pointer;
            line-height: 1.3;
        }
        .btn-video-ctrl:hover { border-color: #00ffa3; color: #00ffa3; }

        .col-body {
            display: flex;
            flex-direction: column;
            flex: 1 1 auto;
            min-width: 0;
            min-height: 0;
            overflow: hidden;
        }
        .chat-pane {
            display: flex;
            flex-direction: column;
            flex: 1 1 auto;
            min-width: 0;
            min-height: 0;
            overflow: hidden;
        }

        /* 더블클릭 전체화면 집중 모드 (큰 영상 + 해당 방송 채팅) */
        body.focus-mode .wrapper {
            display: flex;
        }
        body.focus-mode .chat-box:not(.is-focused) {
            display: none !important;
        }
        body.focus-mode .chat-box.is-focused {
            flex: 1 1 auto;
            width: 100vw;
            height: 100vh;
            border-right: none;
        }
        body.focus-mode .chat-box.is-focused .col-body {
            flex-direction: row;
        }
        body.focus-mode .chat-box.is-focused .video-panel {
            flex: 1 1 auto;
            width: auto;
            height: 100%;
            max-height: none;
            aspect-ratio: auto;
            border-bottom: none;
            border-right: none;
        }
        .focus-resizer {
            display: none;
            width: 5px;
            background-color: #1f222e;
            border-left: 1px solid #2b2f40;
            border-right: 1px solid #2b2f40;
            cursor: ew-resize;
            flex-shrink: 0;
            transition: background-color 0.15s;
        }
        body.focus-mode .chat-box.is-focused .focus-resizer {
            display: block;
        }
        .focus-resizer:hover, .focus-resizer.dragging {
            background-color: #00ffa3;
        }
        body.focus-mode .chat-box.is-focused .chat-pane {
            flex: 0 0 var(--focus-chat-width, clamp(250px, 18vw, 330px));
            height: 100%;
        }
        .btn-pin-toggle.has-new {
            opacity: 1;
            background-color: rgba(0, 255, 163, 0.24);
            border-color: #00ffa3;
            color: #00ffa3;
        }
        .btn-pin-toggle.has-new::after {
            content: "";
            position: absolute;
            top: -3px;
            right: -3px;
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background-color: #00ffa3;
            border: 1.5px solid #141418;
        }

        .font-indicator {
            font-size: 11px; color: #a1a1aa; min-width: 16px; text-align: center; font-weight: bold;
        }

        /* 스트리머 & 매니저 전용 상단 칸 */
        .pinned-panel {
            display: flex; flex-direction: column;
            height: 20%; min-height: 44px; max-height: 75%;
            background-color: #13151d; flex-shrink: 0; overflow: hidden;
        }
        .pinned-panel.hidden { display: none !important; }
        .pinned-header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 2px 8px; background-color: #191c26;
            font-size: 11px; color: #93c5fd; font-weight: bold;
            flex-shrink: 0; border-bottom: 1px solid #222634;
        }
        .pinned-actions { display: flex; align-items: center; gap: 6px; }
        .btn-pin-sub {
            background: transparent; border: none; color: #71717a;
            font-size: 11px; cursor: pointer; padding: 0 3px;
        }
        .btn-pin-sub:hover { color: #f4f4f6; }
        .pinned-list {
            flex: 1 1 auto; min-height: 0; overflow-y: auto;
            padding: 5px 8px; font-size: var(--chat-font-size);
            line-height: 1.42; word-break: break-word;
            contain: content;
            overflow-anchor: none;
        }
        .pinned-list::-webkit-scrollbar { width: 6px; }
        .pinned-list::-webkit-scrollbar-track { background: #13151d; }
        .pinned-list::-webkit-scrollbar-thumb { background: #33394b; border-radius: 3px; }
        .pinned-list::-webkit-scrollbar-thumb:hover { background: #00ffa3; }

        .pinned-empty { color: #52525b; font-size: 11px; font-style: italic; }
        .pinned-item { margin-bottom: 4px; padding: 3px 6px; border-radius: 4px; }
        .pinned-item.role-streamer {
            background: rgba(0, 255, 163, 0.13); border-left: 3px solid #00ffa3;
        }
        .pinned-item.role-manager {
            background: rgba(96, 165, 250, 0.13); border-left: 3px solid #60a5fa;
        }
        .pin-time { color: #9ca3af; font-size: 10px; margin-right: 4px; }

        /* 높이 조절 드래그 바 */
        .resizer {
            height: 5px; background-color: #1f222e;
            border-top: 1px solid #2b2f40; border-bottom: 1px solid #2b2f40;
            cursor: ns-resize; flex-shrink: 0; transition: background-color 0.15s;
        }
        .resizer.hidden { display: none !important; }
        .resizer:hover, .resizer.dragging { background-color: #00ffa3; }

        /* 일반 채팅 영역 */
        .messages-wrap {
            position: relative; flex: 1 1 auto; min-height: 0;
            display: flex; flex-direction: column; overflow: hidden;
        }
        .messages {
            flex: 1 1 auto; min-height: 0; overflow-y: scroll;
            padding: 8px 10px; font-size: var(--chat-font-size);
            line-height: 1.48; word-break: break-word;
            contain: content;
            overflow-anchor: none;
        }
        .messages::-webkit-scrollbar { width: 8px; }
        .messages::-webkit-scrollbar-track { background: #111115; }
        .messages::-webkit-scrollbar-thumb { background: #3f3f4a; border-radius: 4px; }
        .messages::-webkit-scrollbar-thumb:hover { background: #00ffa3; }

        .btn-scroll-bottom {
            display: none; position: absolute; bottom: 10px; left: 50%;
            transform: translateX(-50%); background: rgba(20, 20, 26, 0.94);
            color: #00ffa3; border: 1px solid #00ffa3; border-radius: 14px;
            padding: 4px 12px; font-size: 11px; font-weight: bold;
            cursor: pointer; z-index: 10; white-space: nowrap;
        }
        .btn-scroll-bottom.visible { display: block; }
        .btn-scroll-bottom:hover { background: #00ffa3; color: #0b0b0e; }

        .msg-row { margin-bottom: 5px; padding: 1px 3px; border-radius: 4px; }
        .msg-row.highlight-streamer {
            background: rgba(0, 255, 163, 0.13); border-left: 3px solid #00ffa3; padding: 4px 6px;
        }
        .msg-row.highlight-manager {
            background: rgba(96, 165, 250, 0.13); border-left: 3px solid #60a5fa; padding: 4px 6px;
        }
        .role-badge {
            display: inline-block; font-size: 10px; font-weight: bold;
            padding: 1px 4px; border-radius: 3px; margin-right: 4px; vertical-align: middle;
        }
        .badge-streamer { background: #00ffa3; color: #0b0b0e; }
        .badge-manager { background: #3b82f6; color: #ffffff; }

        .msg-system { color: #71717a; font-size: 12px; font-style: italic; }
        .msg-donation {
            background: rgba(250, 204, 21, 0.12); border-left: 3px solid #facc15;
            padding: 6px 8px; border-radius: 4px; margin: 6px 0;
        }
        .donation-header { color: #fde047; font-weight: bold; font-size: 12px; margin-bottom: 2px; }
        .nickname { font-weight: 700; margin-right: 6px; }
        .msg-text { color: #f4f4f6; }
        .emote {
            height: calc(var(--chat-font-size) * 1.65);
            vertical-align: middle; margin: -2px 1px;
        }

        /* 우클릭 컨텍스트 메뉴 */
        .context-menu {
            display: none;
            position: fixed;
            z-index: 250;
            min-width: 185px;
            background: #181820;
            border: 1px solid #323240;
            border-radius: 8px;
            padding: 5px;
            box-shadow: 0 10px 28px rgba(0, 0, 0, 0.78);
            font-size: 12px;
        }
        .context-menu.open { display: block; }
        .context-menu-header {
            padding: 4px 10px 6px;
            font-size: 11px;
            font-weight: bold;
            color: #00ffa3;
            border-bottom: 1px solid #262632;
            margin-bottom: 4px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 210px;
        }
        .context-menu-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            width: 100%;
            background: transparent;
            border: none;
            color: #e4e4e7;
            padding: 7px 10px;
            border-radius: 5px;
            font-size: 12px;
            text-align: left;
            cursor: pointer;
            gap: 12px;
        }
        .context-menu-item:hover {
            background: #272734;
            color: #00ffa3;
        }
        .context-menu-shortcut {
            color: #71717a;
            font-size: 10px;
        }
        .context-menu-sep {
            height: 1px;
            background: #262632;
            margin: 4px 2px;
        }

        /* 스트리머 편집/검색 모달 */
        .modal-backdrop {
            display: none; position: fixed; inset: 0;
            background: rgba(0, 0, 0, 0.72); z-index: 100;
            align-items: center; justify-content: center;
        }
        .modal-backdrop.open { display: flex; }
        .modal-card {
            background: #16161d; border: 1px solid #2e2e3a;
            border-radius: 10px; width: 520px; max-width: 94vw; max-height: 88vh;
            overflow-y: auto; padding: 18px; box-shadow: 0 12px 32px rgba(0, 0, 0, 0.75);
        }
        .modal-head {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid #262630;
        }
        .modal-title { font-size: 15px; font-weight: bold; color: #00ffa3; }
        .modal-close {
            background: transparent; border: none; color: #a1a1aa;
            font-size: 18px; cursor: pointer; line-height: 1;
        }
        .modal-close:hover { color: #fff; }

        .search-row { display: flex; gap: 6px; margin-bottom: 10px; }
        .search-input {
            flex: 1; background: #0e0e13; border: 1px solid #343442;
            color: #fff; padding: 8px 10px; border-radius: 6px; font-size: 13px;
            outline: none;
        }
        .search-input:focus { border-color: #00ffa3; }
        .btn-primary {
            background: #00ffa3; color: #0b0b0e; border: none;
            border-radius: 6px; padding: 8px 14px; font-weight: bold;
            font-size: 13px; cursor: pointer;
        }
        .btn-primary:hover { background: #00d98b; }

        .search-status { font-size: 12px; color: #a1a1aa; margin-bottom: 8px; min-height: 16px; }
        .search-results {
            display: flex; flex-direction: column; gap: 5px;
            max-height: 210px; overflow-y: auto; margin-bottom: 14px;
        }
        .result-item {
            display: flex; justify-content: space-between; align-items: center;
            background: #1e1e27; border: 1px solid #2b2b36;
            padding: 8px 10px; border-radius: 6px; cursor: pointer;
        }
        .result-item:hover { border-color: #00ffa3; background: #242430; }
        .result-info { display: flex; align-items: center; gap: 8px; }
        .live-badge {
            font-size: 10px; font-weight: bold; padding: 1px 5px; border-radius: 3px;
        }
        .live-badge.on { background: #ef4444; color: #fff; }
        .live-badge.off { background: #27272a; color: #a1a1aa; }
        .follower-txt { font-size: 11px; color: #888; }

        .history-section { margin-bottom: 14px; }
        .history-list {
            display: flex; flex-direction: column; gap: 5px;
            max-height: 190px; overflow-y: auto;
        }
        .history-empty {
            font-size: 12px; color: #71717a; padding: 8px 10px;
            background: #121218; border: 1px dashed #262630; border-radius: 6px;
        }
        .history-row {
            display: flex; align-items: center; justify-content: space-between; gap: 8px;
            background: #121218; border: 1px solid #262630;
            padding: 6px 8px; border-radius: 6px;
        }
        .history-row:hover { border-color: #343444; }
        .history-idx {
            font-size: 11px; font-weight: bold; color: #00ffa3;
            flex-shrink: 0; min-width: 22px;
        }
        .history-chips {
            display: flex; flex-wrap: wrap; gap: 4px; flex: 1; min-width: 0;
        }
        .history-chip {
            background: #1e1e27; color: #e4e4e7; border: 1px solid #2e2e3c;
            border-radius: 4px; padding: 2px 7px; font-size: 12px; cursor: pointer;
            max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .history-chip:hover {
            background: rgba(0, 255, 163, 0.14);
            border-color: #00ffa3;
            color: #00ffa3;
        }
        .history-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }

        .active-section-title {
            font-size: 12px; font-weight: bold; color: #a1a1aa;
            margin-bottom: 8px; display: flex; justify-content: space-between;
        }
        .active-list {
            display: flex; flex-direction: column; gap: 5px;
            max-height: 210px; overflow-y: auto;
        }
        .active-item {
            display: flex; justify-content: space-between; align-items: center;
            background: #121218; border: 1px solid #262630;
            padding: 6px 10px; border-radius: 6px; font-size: 13px;
        }
        .active-item-actions { display: flex; gap: 4px; }

        .global-header-tools .btn-mini.btn-cookie.active,
        .btn-cookie.active {
            border-color: rgba(0, 255, 163, 0.55);
            color: #00ffa3;
            background: rgba(0, 255, 163, 0.14);
        }
        .global-header-tools:hover .btn-mini.btn-cookie.active,
        .global-header-tools.intro-spotlight .btn-mini.btn-cookie.active {
            border-color: rgba(0, 255, 163, 0.65);
            color: #00ffa3;
            background: rgba(0, 255, 163, 0.14);
        }
        .cookie-guide {
            font-size: 12px;
            color: #a1a1aa;
            background: #121218;
            border: 1px solid #262630;
            border-radius: 6px;
            padding: 10px 12px;
            line-height: 1.55;
            margin-bottom: 12px;
        }
        .cookie-field { margin-bottom: 10px; }
        .cookie-label {
            display: block;
            font-size: 12px;
            font-weight: bold;
            color: #d4d4d8;
            margin-bottom: 4px;
        }
        .cookie-actions {
            display: flex;
            justify-content: flex-end;
            gap: 8px;
            margin-top: 12px;
        }
        .btn-secondary {
            background: #23232a;
            color: #d4d4d8;
            border: 1px solid #34343f;
            border-radius: 6px;
            padding: 8px 12px;
            font-size: 13px;
            cursor: pointer;
        }
        .btn-secondary:hover { border-color: #ff6b6b; color: #ff6b6b; }
    </style>
</head>
<body>
    <div class="wrapper" id="wrapper"></div>

    <!-- 전역 컨트롤 묶음 -->
    <div class="global-header-tools" id="global-tools">
        <button type="button" class="btn-tools-handle btn-tools-expand-tab" id="btn-tools-expand" title="설정 · 글자크기 툴바 펼치기">◀</button>
        <div class="global-tools-expanded" id="global-tools-expanded">
            <button type="button" class="btn-tools-handle btn-tools-collapse-tab" id="btn-tools-collapse" title="툴바 접기">▶</button>
            <div class="global-tools-inner">
                <div class="global-tools-section" aria-label="관리 및 인증">
                    <button class="btn-mini btn-tools-primary" id="btn-manage-streamers" title="스트리머 추가 및 편성 관리 (E)">⚙ 설정</button>
                    <button class="btn-mini btn-cookie" id="btn-cookie-settings" title="네이버 로그인 쿠키 설정 (성인 인증 방송용)">🔑</button>
                </div>
                <div class="global-tools-section" aria-label="글자 크기 조절">
                    <button class="btn-mini" id="btn-font-down" title="글자 작게 (-)">-</button>
                    <span class="font-indicator" id="font-size-val">20</span>
                    <button class="btn-mini" id="btn-font-up" title="글자 크게 (+)">+</button>
                </div>
            </div>
        </div>
        <div class="global-tools-hint" id="global-tools-hint" title="클릭하면 닫히고 툴바가 접힙니다">
            <div class="global-tools-hint-head">
                <span>⚙ 설정 · 쿠키 · 글자크기 (E)</span>
                <button type="button" class="global-tools-hint-close" id="btn-tools-hint-close" title="닫기">✕</button>
            </div>
            <div class="global-tools-hint-body">
                스트리머 편성, 성인 인증 쿠키, 채팅 글자 크기를 여기서 바로 바꿀 수 있어요.
            </div>
            <div class="global-tools-hint-sub">
                잠시 후 오른쪽 끝 얇은 <b>[◀]</b> 버튼으로 접히며, 눌러서 언제든 다시 펼칠 수 있습니다.
            </div>
        </div>
    </div>

    <!-- 📌 우클릭 컨텍스트 메뉴 -->
    <div class="context-menu" id="pin-context-menu">
        <div class="context-menu-header" id="ctx-menu-title">스트리머 · 매니저 칸</div>
        <button class="context-menu-item" id="ctx-toggle-one">
            <span id="ctx-toggle-one-label">현재 칸 펴기</span>
            <span class="context-menu-shortcut">좌클릭</span>
        </button>
        <div class="context-menu-sep"></div>
        <button class="context-menu-item" id="ctx-expand-all">
            <span>모든 칸 일괄 펴기</span>
            <span class="context-menu-shortcut">H</span>
        </button>
        <button class="context-menu-item" id="ctx-collapse-all">
            <span>모든 칸 일괄 접기</span>
            <span class="context-menu-shortcut">H</span>
        </button>
        <div class="context-menu-sep"></div>
        <button class="context-menu-item" id="ctx-reset-height-all">
            <span>모든 칸 높이 1/5 초기화</span>
        </button>
        <button class="context-menu-item" id="ctx-clear-all">
            <span>모든 칸 알림 비우기</span>
        </button>
    </div>

    <!-- 모달 최근 기록 우클릭 컨텍스트 메뉴 -->
    <div class="context-menu" id="history-context-menu" style="z-index: 300;">
        <div class="context-menu-header" id="hist-ctx-title">최근 기록</div>
        <button class="context-menu-item" id="hist-ctx-pick-one">
            <span id="hist-ctx-pick-one-label">이 스트리머 추가</span>
            <span class="context-menu-shortcut">좌클릭</span>
        </button>
        <button class="context-menu-item" id="hist-ctx-load-combo">
            <span>이 조합 전체 불러오기</span>
        </button>
        <div class="context-menu-sep"></div>
        <button class="context-menu-item" id="hist-ctx-delete">
            <span>이 기록 삭제</span>
        </button>
    </div>

    <!-- 네이버 쿠키 설정 모달 -->
    <div class="modal-backdrop" id="cookie-modal-backdrop">
        <div class="modal-card">
            <div class="modal-head">
                <span class="modal-title">🔑 네이버 로그인 쿠키 설정</span>
                <button class="modal-close" id="cookie-modal-close">×</button>
            </div>
            <div class="cookie-guide">
                성인(19+) 방송 시청/채팅에 사용됩니다. (이 PC의 로컬 파일에만 저장)<br>
                <b>chzzk.naver.com</b> 로그인 → <b>F12</b> → <b>Application</b> → <b>Cookies</b>에서<br>
                <b>NID_AUT</b>와 <b>NID_SES</b> 값을 복사해 입력하세요.
            </div>
            <div class="search-status" id="cookie-status-txt">확인 중...</div>
            <div class="cookie-field">
                <label class="cookie-label" for="cookie-nid-aut">NID_AUT</label>
                <input type="password" class="search-input" id="cookie-nid-aut" placeholder="NID_AUT 값 붙여넣기" autocomplete="off" style="width:100%">
            </div>
            <div class="cookie-field">
                <label class="cookie-label" for="cookie-nid-ses">NID_SES</label>
                <input type="password" class="search-input" id="cookie-nid-ses" placeholder="NID_SES 값 붙여넣기" autocomplete="off" style="width:100%">
            </div>
            <div class="cookie-actions">
                <button type="button" class="btn-secondary" id="btn-cookie-clear">저장된 쿠키 삭제</button>
                <button type="button" class="btn-primary" id="btn-cookie-save">저장</button>
            </div>
        </div>
    </div>

    <!-- 스트리머 검색/편집 모달 -->
    <div class="modal-backdrop" id="modal-backdrop">
        <div class="modal-card">
            <div class="modal-head">
                <span class="modal-title" id="modal-title">스트리머 추가 · 편성 관리</span>
                <button class="modal-close" id="modal-close">×</button>
            </div>
            <div class="search-row">
                <input type="text" class="search-input" id="search-input" placeholder="스트리머 이름, 채널 ID 또는 치지직 URL (엔터로 즉시 선택)">
                <button class="btn-primary" id="btn-search">검색</button>
            </div>
            <div class="search-status" id="search-status">이름, 채널 ID 또는 치지직 URL을 입력하고 엔터를 누르세요.</div>
            <div class="search-results" id="search-results"></div>

            <div class="history-section" id="history-section">
                <div class="active-section-title">
                    <span id="history-section-title">📜 최근 기록 (칩 클릭: 1명 추가 / 우클릭: 메뉴)</span>
                    <span id="history-count-label">0 / 9</span>
                </div>
                <div class="history-list" id="history-list"></div>
            </div>

            <div id="active-section">
                <div class="active-section-title">
                    <span>현재 표시 중인 채널 (최대 6개)</span>
                    <span id="active-count-label">0 / 6</span>
                </div>
                <div class="active-list" id="active-list"></div>
            </div>
        </div>
    </div>

    <script>
        const INITIAL_FROM_SERVER = __CHANNELS_JSON__;
        const STORAGE_CHANNELS_KEY = "chzzk_multiview_saved_channels";
        const PALETTE = [
            "#ff7b7b", "#ffa94d", "#ffd43b", "#69db7c", "#38d9a9",
            "#4dabf7", "#9775fa", "#f783ac", "#66d9e8", "#a9e34b"
        ];

        function loadInitialChannels() {
            if (Array.isArray(INITIAL_FROM_SERVER) && INITIAL_FROM_SERVER.length > 0) {
                localStorage.setItem(STORAGE_CHANNELS_KEY, JSON.stringify(INITIAL_FROM_SERVER));
                return INITIAL_FROM_SERVER;
            }
            try {
                const saved = JSON.parse(localStorage.getItem(STORAGE_CHANNELS_KEY) || "[]");
                if (Array.isArray(saved) && saved.length > 0) return saved;
            } catch (e) {}
            return [];
        }

        let activeChannels = loadInitialChannels();
        const clientMap = new Map();

        let currentFontSize = parseInt(localStorage.getItem("chzzk_chat_font_size") || "20", 10);

        function applyFontSize(size) {
            currentFontSize = Math.min(26, Math.max(10, size));
            document.documentElement.style.setProperty("--chat-font-size", currentFontSize + "px");
            localStorage.setItem("chzzk_chat_font_size", currentFontSize);
            const valEl = document.getElementById("font-size-val");
            if (valEl) valEl.textContent = currentFontSize;

            requestAnimationFrame(() => {
                clientMap.forEach(client => client.keepBottomAnchor());
            });
        }

        document.getElementById("btn-font-down").onclick = () => applyFontSize(currentFontSize - 1);
        document.getElementById("btn-font-up").onclick = () => applyFontSize(currentFontSize + 1);

        // PC 네트워크 복구 시 끊어져 있던 방 즉시 재연결
        window.addEventListener("online", () => {
            clientMap.forEach(client => {
                if (!client.ws || client.ws.readyState === WebSocket.CLOSED || client.ws.readyState === WebSocket.CLOSING) {
                    client.retryCount = 0;
                    client.connect(true);
                }
            });
        });

        // 우클릭 컨텍스트 메뉴 제어
        const contextMenuEl = document.getElementById("pin-context-menu");
        let contextTargetClient = null;

        function positionContextMenu(menuEl, e) {
            menuEl.style.visibility = "hidden";
            menuEl.classList.add("open");

            const rect = menuEl.getBoundingClientRect();
            const safeX = Math.min(e.clientX, window.innerWidth - rect.width - 8);
            const safeY = Math.min(e.clientY, window.innerHeight - rect.height - 8);

            menuEl.style.left = Math.max(8, safeX) + "px";
            menuEl.style.top = Math.max(8, safeY) + "px";
            menuEl.style.visibility = "visible";
        }

        function openPinContextMenu(e, client) {
            e.preventDefault();
            e.stopPropagation();
            contextTargetClient = client;

            document.getElementById("ctx-menu-title").textContent = `${client.channel.name} · 상단 칸`;
            document.getElementById("ctx-toggle-one-label").textContent =
                client.isPinHidden ? "현재 칸 펴기" : "현재 칸 접기";

            positionContextMenu(contextMenuEl, e);
        }

        function closePinContextMenu() {
            contextMenuEl.classList.remove("open");
            contextTargetClient = null;
        }

        const historyCtxMenuEl = document.getElementById("history-context-menu");
        let histCtxTargetChannel = null;
        let histCtxTargetComboIdx = -1;

        function openHistoryContextMenu(e, comboIdx, channel) {
            e.preventDefault();
            e.stopPropagation();
            histCtxTargetComboIdx = comboIdx;
            histCtxTargetChannel = channel || null;

            const pickOneBtn = document.getElementById("hist-ctx-pick-one");
            const pickOneLabel = document.getElementById("hist-ctx-pick-one-label");
            const titleEl = document.getElementById("hist-ctx-title");
            const loadComboBtn = document.getElementById("hist-ctx-load-combo");

            if (histCtxTargetChannel) {
                titleEl.textContent = `${histCtxTargetChannel.name} · 기록 #${comboIdx + 1}`;
                pickOneLabel.textContent = "이 스트리머 추가";
                pickOneBtn.style.display = "flex";
            } else {
                titleEl.textContent = `최근 조합 #${comboIdx + 1}`;
                pickOneBtn.style.display = "none";
            }
            loadComboBtn.style.display = "flex";

            positionContextMenu(historyCtxMenuEl, e);
        }

        function closeHistoryContextMenu() {
            historyCtxMenuEl.classList.remove("open");
            histCtxTargetChannel = null;
            histCtxTargetComboIdx = -1;
        }

        document.getElementById("hist-ctx-pick-one").onclick = () => {
            if (histCtxTargetChannel) {
                applySelectedStreamer(histCtxTargetChannel);
            }
            closeHistoryContextMenu();
        };

        document.getElementById("hist-ctx-load-combo").onclick = () => {
            if (histCtxTargetComboIdx >= 0) {
                applyComboFromHistory(histCtxTargetComboIdx);
            }
            closeHistoryContextMenu();
        };

        document.getElementById("hist-ctx-delete").onclick = () => {
            if (histCtxTargetComboIdx >= 0) {
                deleteHistoryEntry(histCtxTargetComboIdx);
            }
            closeHistoryContextMenu();
        };

        document.getElementById("ctx-toggle-one").onclick = () => {
            if (contextTargetClient) {
                contextTargetClient.setPinHidden(!contextTargetClient.isPinHidden);
            }
            closePinContextMenu();
        };

        document.getElementById("ctx-expand-all").onclick = () => {
            clientMap.forEach(c => c.setPinHidden(false));
            closePinContextMenu();
        };

        document.getElementById("ctx-collapse-all").onclick = () => {
            clientMap.forEach(c => c.setPinHidden(true));
            closePinContextMenu();
        };

        document.getElementById("ctx-reset-height-all").onclick = () => {
            clientMap.forEach(c => c.setPinHeightPercent(20));
            closePinContextMenu();
        };

        document.getElementById("ctx-clear-all").onclick = () => {
            clientMap.forEach(c => c.clearPinned());
            closePinContextMenu();
        };

        window.addEventListener("click", (e) => {
            if (!contextMenuEl.contains(e.target)) {
                closePinContextMenu();
            }
            if (!historyCtxMenuEl.contains(e.target)) {
                closeHistoryContextMenu();
            }
        });
        window.addEventListener("contextmenu", (e) => {
            if (!e.target.closest(".btn-pin-toggle")) {
                closePinContextMenu();
            }
            if (!e.target.closest(".history-row")) {
                closeHistoryContextMenu();
            }
        });
        window.addEventListener("blur", () => {
            closePinContextMenu();
            closeHistoryContextMenu();
        });
        window.addEventListener("resize", () => {
            closePinContextMenu();
            closeHistoryContextMenu();
        });

        let focusedClient = null;
        let focusPrevFullscreen = false;
        let focusSuspendedClients = [];
        let focusPrevQuality = 360;
        let focusPrevMuted = true;
        let focusExiting = false;

        function enterFocusMode(client) {
            if (!client || client.isDestroyed) return;
            if (focusedClient === client) return;
            if (focusedClient) exitFocusMode(true);

            focusedClient = client;
            focusPrevFullscreen = !!document.fullscreenElement;
            focusPrevQuality = client.preferredQuality;
            focusPrevMuted = client.videoEl.muted;
            focusSuspendedClients = [];

            clientMap.forEach((other) => {
                if (other !== client && other.isVideoOpen) {
                    focusSuspendedClients.push(other);
                    other.stopVideo();
                }
            });

            if (!client.isVideoOpen) {
                client.setVideoOpen(true);
            }

            document.body.classList.add("focus-mode");
            client.container.classList.add("is-focused");
            client.videoFocusBtn.textContent = "↩";
            client.videoFocusBtn.title = "멀티뷰로 복귀 (더블클릭 / Esc)";

            client.setVideoMuted(false);
            client.preferredQuality = 0;
            client.videoQualBtn.textContent = "원본";
            client.applyPreferredVideoLevel();
            client.snapToLiveEdge();
            client.runWithAnchorPreserved(() => {});

            if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
                document.documentElement.requestFullscreen().catch(() => {});
            }
        }

        function exitFocusMode(skipFullscreenExit = false) {
            if (!focusedClient || focusExiting) return;
            focusExiting = true;

            const target = focusedClient;
            focusedClient = null;

            document.body.classList.remove("focus-mode");
            target.container.classList.remove("is-focused");
            target.videoFocusBtn.textContent = "⤢";
            target.videoFocusBtn.title = "전체화면 집중 모드 (더블클릭)";

            target.setVideoMuted(focusPrevMuted);
            target.preferredQuality = focusPrevQuality;
            target.videoQualBtn.textContent = target.preferredQuality === 0 ? "원본" : `${target.preferredQuality}p`;
            target.applyPreferredVideoLevel();

            const toRestore = focusSuspendedClients;
            focusSuspendedClients = [];
            for (let i = 0; i < toRestore.length; i++) {
                const other = toRestore[i];
                if (!other.isDestroyed && other.isVideoOpen) {
                    other.startVideo();
                }
            }

            clientMap.forEach(c => c.runWithAnchorPreserved(() => {}));

            if (!skipFullscreenExit && !focusPrevFullscreen && document.fullscreenElement && document.exitFullscreen) {
                document.exitFullscreen().catch(() => {});
            }
            endFocusResize();
            focusExiting = false;
        }

        const FOCUS_CHAT_WIDTH_KEY = "chzzk_focus_chat_width";
        let activeFocusResizer = null;

        function applyFocusChatWidth(px, persist = true) {
            if (px == null) {
                document.documentElement.style.removeProperty("--focus-chat-width");
                if (persist) localStorage.removeItem(FOCUS_CHAT_WIDTH_KEY);
                return;
            }
            const maxPx = Math.max(260, Math.min(560, Math.floor(window.innerWidth * 0.48)));
            const clamped = Math.max(220, Math.min(maxPx, Math.round(px)));
            document.documentElement.style.setProperty("--focus-chat-width", `${clamped}px`);
            if (persist) localStorage.setItem(FOCUS_CHAT_WIDTH_KEY, String(clamped));
        }

        function endFocusResize() {
            if (!activeFocusResizer) return;
            activeFocusResizer.classList.remove("dragging");
            activeFocusResizer = null;
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
        }

        (function setupFocusResizer() {
            const savedWidth = Number(localStorage.getItem(FOCUS_CHAT_WIDTH_KEY));
            if (Number.isFinite(savedWidth) && savedWidth >= 220) {
                applyFocusChatWidth(savedWidth, false);
            }

            document.addEventListener("mousedown", (e) => {
                const resizer = e.target.closest(".focus-resizer");
                if (!resizer || !focusedClient) return;
                e.preventDefault();
                activeFocusResizer = resizer;
                resizer.classList.add("dragging");
                document.body.style.cursor = "ew-resize";
                document.body.style.userSelect = "none";
            });

            window.addEventListener("mousemove", (e) => {
                if (!activeFocusResizer || !focusedClient) return;
                const colBody = activeFocusResizer.parentElement;
                if (!colBody) return;
                const rect = colBody.getBoundingClientRect();
                applyFocusChatWidth(rect.right - e.clientX, true);
            });

            window.addEventListener("mouseup", endFocusResize);
            window.addEventListener("blur", endFocusResize);

            document.addEventListener("dblclick", (e) => {
                if (e.target.closest(".focus-resizer")) {
                    applyFocusChatWidth(null, true);
                }
            });
        })();

        function toggleFocusMode(client) {
            if (focusedClient === client) {
                exitFocusMode();
            } else {
                enterFocusMode(client);
            }
        }

        document.addEventListener("fullscreenchange", () => {
            if (focusedClient && !document.fullscreenElement) {
                exitFocusMode(true);
            }
        });

        window.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                if (historyCtxMenuEl.classList.contains("open")) {
                    closeHistoryContextMenu();
                    return;
                }
                if (contextMenuEl.classList.contains("open")) {
                    closePinContextMenu();
                    return;
                }
                if (cookieModalBackdrop.classList.contains("open")) {
                    closeCookieModal();
                    return;
                }
                if (modalBackdrop.classList.contains("open")) {
                    closeModal();
                    return;
                }
                if (focusedClient) {
                    exitFocusMode();
                    return;
                }
                return;
            }
            if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
            if (e.key === "-" || e.key === "_") applyFontSize(currentFontSize - 1);
            if (e.key === "+" || e.key === "=") applyFontSize(currentFontSize + 1);
            if (e.key === "e" || e.key === "E") openManageModal();
            if (e.key === "h" || e.key === "H") {
                const clients = Array.from(clientMap.values());
                const anyVisible = clients.some(c => !c.isPinHidden);
                clients.forEach(c => c.setPinHidden(anyVisible));
            }
        });

        function escapeHtml(str) {
            return String(str)
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;");
        }

        function isReadableColor(hex) {
            const clean = hex.replace("#", "");
            if (clean.length !== 6) return false;
            const r = parseInt(clean.substring(0, 2), 16);
            const g = parseInt(clean.substring(2, 4), 16);
            const b = parseInt(clean.substring(4, 6), 16);
            const brightness = (r * 299 + g * 587 + b * 114) / 1000;
            return brightness > 95;
        }

        function getNicknameColor(profile, role) {
            if (role === "streamer") return "#00ffa3";
            if (role === "streaming_chat_manager" || role === "streaming_channel_manager") return "#60a5fa";

            try {
                const code = profile?.streamingProperty?.nicknameColor?.colorCode;
                if (code) {
                    const hex = code.startsWith("#") ? code : "#" + code;
                    if (isReadableColor(hex)) return hex;
                }
            } catch (e) {}

            const name = profile?.nickname || "user";
            let hash = 0;
            for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
            return PALETTE[Math.abs(hash) % PALETTE.length];
        }

        const MAX_MAIN_NODES_AUTO = 120;
        const MAX_MAIN_NODES_PAUSED = 800;
        const MAX_PIN_NODES = 50;
        const FLUSH_INTERVAL_MS = 100;

        let globalFlushTimer = null;
        let globalFlushRaf = null;
        const dirtyClients = new Set();

        function scheduleGlobalFlush(client) {
            dirtyClients.add(client);
            if (document.hidden) return;
            if (globalFlushTimer !== null || globalFlushRaf !== null) return;
            globalFlushTimer = setTimeout(() => {
                globalFlushTimer = null;
                if (document.hidden) return;
                globalFlushRaf = requestAnimationFrame(runGlobalFlush);
            }, FLUSH_INTERVAL_MS);
        }

        function runGlobalFlush() {
            globalFlushRaf = null;
            if (document.hidden || dirtyClients.size === 0) return;

            const clients = Array.from(dirtyClients);
            dirtyClients.clear();

            // Phase 1: 모든 채널의 DOM 쓰기만 먼저 일괄 수행
            const scrollTargets = [];
            for (let i = 0; i < clients.length; i++) {
                const c = clients[i];
                if (!c.isDestroyed && c.flushDomOnly()) {
                    scrollTargets.push(c);
                }
            }

            // Phase 2: 모든 채널의 스크롤 갱신을 한 번에 처리 (Forced Reflow 최소화)
            for (let i = 0; i < scrollTargets.length; i++) {
                scrollTargets[i].flushScrollOnly();
            }
        }

        document.addEventListener("visibilitychange", () => {
            if (!document.hidden && dirtyClients.size > 0) {
                if (globalFlushTimer !== null) {
                    clearTimeout(globalFlushTimer);
                    globalFlushTimer = null;
                }
                if (globalFlushRaf === null) {
                    globalFlushRaf = requestAnimationFrame(runGlobalFlush);
                }
            }
        });

        function appendFormattedText(targetEl, rawMsg, extras) {
            const text = rawMsg || "";
            const emojis = extras?.emojis;
            if (!emojis || text.indexOf("{:") === -1) {
                targetEl.textContent = text;
                return;
            }

            const re = /\{:([a-zA-Z0-9_]+):\}/g;
            let lastIdx = 0;
            let match;
            let hasEmote = false;

            while ((match = re.exec(text)) !== null) {
                const url = emojis[match[1]];
                if (typeof url === "string" && /^https?:\/\//i.test(url)) {
                    if (match.index > lastIdx) {
                        targetEl.appendChild(document.createTextNode(text.slice(lastIdx, match.index)));
                    }
                    const img = document.createElement("img");
                    img.className = "emote";
                    img.src = url;
                    img.alt = match[1];
                    img.decoding = "async";
                    targetEl.appendChild(img);
                    lastIdx = re.lastIndex;
                    hasEmote = true;
                }
            }

            if (!hasEmote) {
                targetEl.textContent = text;
                return;
            }
            if (lastIdx < text.length) {
                targetEl.appendChild(document.createTextNode(text.slice(lastIdx)));
            }
        }

        function getTimeString() {
            const now = new Date();
            const hh = String(now.getHours()).padStart(2, "0");
            const mm = String(now.getMinutes()).padStart(2, "0");
            return `${hh}:${mm}`;
        }

        let hlsLibPromise = null;
        function ensureHlsLib() {
            if (window.Hls) return Promise.resolve(window.Hls);
            if (hlsLibPromise) return hlsLibPromise;
            hlsLibPromise = new Promise((resolve, reject) => {
                const s = document.createElement("script");
                s.src = "https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js";
                s.onload = () => resolve(window.Hls);
                s.onerror = () => {
                    hlsLibPromise = null;
                    reject(new Error("hls.js 로드 실패"));
                };
                document.head.appendChild(s);
            });
            return hlsLibPromise;
        }

        class ChatClient {
            constructor(channel, container) {
                this.channel = channel;
                this.container = container;
                this.ws = null;
                this.pingTimer = null;
                this.reconnectTimer = null;
                this.livePollTimer = null;

                this.hls = null;
                this.isVideoOpen = false;
                this.videoLoadSeq = 0;
                this.preferredQuality = 360;

                this.isDestroyed = false;
                this.isConnecting = false;
                this.connectSeq = 0;
                this.retryCount = 0;
                this.currentChatChannelId = null;
                this.liveStatus = "UNKNOWN";
                this.statusLineEl = null;

                this.pendingMainQueue = [];
                this.pendingPinQueue = [];
                this._needScrollMain = false;
                this._needScrollPin = false;

                this.autoScrollMain = true;
                this.autoScrollPin = true;
                this.ignoreScroll = false;

                const savedHidden = localStorage.getItem(`chzzk_pin_hidden_${channel.id}`);
                this.isPinHidden = savedHidden === null ? true : savedHidden === "1";

                container.innerHTML = `
                    <div class="header">
                        <h1 class="streamer-name" title="클릭하여 치지직 방송 페이지 열기">${escapeHtml(channel.name)}</h1>
                        <div class="header-controls">
                            <button class="btn-mini btn-video-toggle" title="상단 미니 영상 켜기/끄기 (Shift+클릭: 전체 동시 토글)">📺</button>
                            <button class="btn-mini btn-pin-toggle" title="스트리머·매니저 칸 접기/펴기 (우클릭: 일괄 메뉴)">📌</button>
                            <span class="status-dot" title="연결 상태"></span>
                            <button class="btn-mini btn-reconnect">재연결</button>
                            <button class="btn-mini btn-close-col" title="이 칸 닫기">×</button>
                        </div>
                    </div>
                    <div class="col-body">
                        <div class="video-panel hidden" title="더블클릭: 전체화면 집중 모드 (최고 화질)">
                            <video class="video-player" muted playsinline autoplay></video>
                            <div class="video-msg hidden"></div>
                            <div class="video-overlay">
                                <span class="video-info-badge">LIVE</span>
                                <div class="video-overlay-actions">
                                    <button type="button" class="btn-video-ctrl btn-video-mute" title="음소거 / 해제">🔇</button>
                                    <button type="button" class="btn-video-ctrl btn-video-qual" title="화질 전환 (360p / 480p / 720p / 원본)">360p</button>
                                    <button type="button" class="btn-video-ctrl btn-video-focus" title="전체화면 집중 모드 (더블클릭)">⤢</button>
                                    <button type="button" class="btn-video-ctrl btn-video-reload" title="영상 새로고침">↻</button>
                                    <button type="button" class="btn-video-ctrl btn-video-close" title="영상 닫기">×</button>
                                </div>
                            </div>
                        </div>
                        <div class="focus-resizer" title="드래그: 채팅창 너비 조절 | 더블클릭: 기본 너비 초기화"></div>
                        <div class="chat-pane">
                            <div class="pinned-panel">
                                <div class="pinned-header">
                                    <span>📌 스트리머 · 매니저</span>
                                    <div class="pinned-actions">
                                        <button class="btn-pin-sub btn-clear-pin">비우기</button>
                                        <button class="btn-pin-sub btn-hide-pin">접기</button>
                                    </div>
                                </div>
                                <div class="pinned-list">
                                    <div class="pinned-empty">스트리머·매니저 채팅이 여기에 표시됩니다.</div>
                                </div>
                            </div>
                            <div class="resizer" title="드래그: 칸 높이 조절 | Shift+드래그: 전체 동시 조절 | 더블클릭: 1/5 초기화"></div>
                            <div class="messages-wrap">
                                <div class="messages"></div>
                                <button class="btn-scroll-bottom">↓ 최신 채팅</button>
                            </div>
                        </div>
                    </div>
                `;

                this.headerEl = container.querySelector(".header");
                this.nameEl = container.querySelector(".streamer-name");
                this.controlsEl = container.querySelector(".header-controls");
                this.dotEl = container.querySelector(".status-dot");
                this.chatPaneEl = container.querySelector(".chat-pane");
                this.msgsEl = container.querySelector(".messages");
                this.scrollBtnEl = container.querySelector(".btn-scroll-bottom");
                this.videoPanelEl = container.querySelector(".video-panel");
                this.videoEl = container.querySelector(".video-player");
                this.videoMsgEl = container.querySelector(".video-msg");
                this.videoBadgeEl = container.querySelector(".video-info-badge");
                this.toggleVideoBtn = container.querySelector(".btn-video-toggle");
                this.videoMuteBtn = container.querySelector(".btn-video-mute");
                this.videoQualBtn = container.querySelector(".btn-video-qual");
                this.videoFocusBtn = container.querySelector(".btn-video-focus");
                this.videoReloadBtn = container.querySelector(".btn-video-reload");
                this.videoCloseBtn = container.querySelector(".btn-video-close");
                this.pinPanelEl = container.querySelector(".pinned-panel");
                this.pinListEl = container.querySelector(".pinned-list");
                this.resizerEl = container.querySelector(".resizer");
                this.togglePinBtn = container.querySelector(".btn-pin-toggle");
                this.reconnectBtn = container.querySelector(".btn-reconnect");
                this.closeColBtn = container.querySelector(".btn-close-col");
                this.clearPinBtn = container.querySelector(".btn-clear-pin");
                this.hidePinBtn = container.querySelector(".btn-hide-pin");

                this.nameEl.onclick = () => {
                    window.open(`https://chzzk.naver.com/live/${this.channel.id}`, "_blank", "noopener");
                };
                this.closeColBtn.onclick = () => removeChannel(this.channel.id);

                this.toggleVideoBtn.onclick = (e) => {
                    const nextOpen = !this.isVideoOpen;
                    if (e.shiftKey) {
                        clientMap.forEach(c => c.setVideoOpen(nextOpen));
                    } else {
                        this.setVideoOpen(nextOpen);
                    }
                };
                this.videoPanelEl.ondblclick = (e) => {
                    if (e.target.closest(".video-overlay")) return;
                    toggleFocusMode(this);
                };
                this.videoMuteBtn.onclick = () => this.toggleVideoMute();
                this.videoQualBtn.onclick = () => this.cycleVideoQuality();
                this.videoFocusBtn.onclick = () => toggleFocusMode(this);
                this.videoReloadBtn.onclick = () => this.startVideo();
                this.videoCloseBtn.onclick = () => this.setVideoOpen(false);

                const savedRatio = localStorage.getItem(`chzzk_pin_ratio_${channel.id}`);
                if (savedRatio) {
                    this.pinPanelEl.style.height = savedRatio;
                }
                this.setPinHidden(this.isPinHidden);

                this.togglePinBtn.addEventListener("mouseenter", () => {
                    if (this.togglePinBtn.classList.contains("has-new")) {
                        this.togglePinBtn.classList.remove("has-new");
                    }
                });

                this.togglePinBtn.onclick = (e) => {
                    closePinContextMenu();
                    const nextHidden = !this.isPinHidden;
                    if (e.shiftKey) {
                        clientMap.forEach(c => c.setPinHidden(nextHidden));
                    } else {
                        this.setPinHidden(nextHidden);
                    }
                };

                this.togglePinBtn.addEventListener("contextmenu", (e) => {
                    openPinContextMenu(e, this);
                });

                this.hidePinBtn.onclick = () => this.setPinHidden(true);

                this.msgsEl.addEventListener("scroll", () => {
                    if (this.ignoreScroll) return;
                    const dist = this.msgsEl.scrollHeight - this.msgsEl.scrollTop - this.msgsEl.clientHeight;
                    this.autoScrollMain = dist < 35;
                    if (this.autoScrollMain) {
                        this.scrollBtnEl.classList.remove("visible");
                    } else {
                        this.scrollBtnEl.classList.add("visible");
                    }
                }, { passive: true });

                this.pinListEl.addEventListener("scroll", () => {
                    const dist = this.pinListEl.scrollHeight - this.pinListEl.scrollTop - this.pinListEl.clientHeight;
                    this.autoScrollPin = dist < 25;
                }, { passive: true });

                this.scrollBtnEl.onclick = () => {
                    this.autoScrollMain = true;
                    while (this.msgsEl.children.length > MAX_MAIN_NODES_AUTO) {
                        this.msgsEl.removeChild(this.msgsEl.firstChild);
                    }
                    this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
                    this.scrollBtnEl.classList.remove("visible");
                };

                this.ro = new ResizeObserver(() => this.keepBottomAnchor());
                this.ro.observe(this.msgsEl);
                this.ro.observe(this.pinListEl);

                this.initResizer();

                this.reconnectBtn.onclick = () => {
                    this.retryCount = 0;
                    this.statusLineEl = null;
                    this.connect(false);
                };
                this.clearPinBtn.onclick = () => this.clearPinned();

                this.startLivePoller();
                this.connect(false);
            }

            flushDomOnly() {
                if (this.isDestroyed) return false;

                if (this.pendingMainQueue.length > 0) {
                    const maxDom = this.autoScrollMain ? MAX_MAIN_NODES_AUTO : MAX_MAIN_NODES_PAUSED;
                    const startIdx = Math.max(0, this.pendingMainQueue.length - maxDom);
                    const frag = document.createDocumentFragment();
                    for (let i = startIdx; i < this.pendingMainQueue.length; i++) {
                        const entry = this.pendingMainQueue[i];
                        frag.appendChild(
                            entry.type === "node"
                                ? entry.el
                                : this.buildChatNode(entry.item, entry.isDonation, entry.parsedProfile, entry.parsedExtras)
                        );
                    }
                    this.pendingMainQueue.length = 0;
                    this.msgsEl.appendChild(frag);

                    while (this.msgsEl.children.length > maxDom) {
                        this.msgsEl.removeChild(this.msgsEl.firstChild);
                    }
                    if (this.autoScrollMain) {
                        this._needScrollMain = true;
                    }
                }

                if (this.pendingPinQueue.length > 0) {
                    const emptyPlaceholder = this.pinListEl.querySelector(".pinned-empty");
                    if (emptyPlaceholder) emptyPlaceholder.remove();

                    const startIdx = Math.max(0, this.pendingPinQueue.length - MAX_PIN_NODES);
                    const frag = document.createDocumentFragment();
                    for (let i = startIdx; i < this.pendingPinQueue.length; i++) {
                        frag.appendChild(this.buildPinnedNode(this.pendingPinQueue[i]));
                    }
                    this.pendingPinQueue.length = 0;
                    this.pinListEl.appendChild(frag);

                    while (this.pinListEl.children.length > MAX_PIN_NODES) {
                        this.pinListEl.removeChild(this.pinListEl.firstChild);
                    }

                    if (this.isPinHidden) {
                        this.togglePinBtn.classList.add("has-new");
                    } else if (this.autoScrollPin) {
                        this._needScrollPin = true;
                    }
                }

                return this._needScrollMain || this._needScrollPin;
            }

            flushScrollOnly() {
                if (this.isDestroyed) return;
                if (this._needScrollMain && this.autoScrollMain) {
                    this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
                }
                if (this._needScrollPin && this.autoScrollPin && !this.isPinHidden) {
                    this.pinListEl.scrollTop = this.pinListEl.scrollHeight;
                }
                this._needScrollMain = false;
                this._needScrollPin = false;
            }

            clearPinned() {
                this.pendingPinQueue.length = 0;
                this.pinListEl.innerHTML = `<div class="pinned-empty">스트리머·매니저 채팅이 여기에 표시됩니다.</div>`;
                this.togglePinBtn.classList.remove("has-new");
            }

            runWithAnchorPreserved(fn) {
                const keepMain = this.autoScrollMain;
                const keepPin = this.autoScrollPin;
                this.ignoreScroll = true;
                fn();
                if (keepMain) this.autoScrollMain = true;
                if (keepPin) this.autoScrollPin = true;
                this.keepBottomAnchor();
                requestAnimationFrame(() => {
                    if (this.isDestroyed) return;
                    if (keepMain) {
                        this.autoScrollMain = true;
                        this.scrollBtnEl.classList.remove("visible");
                    }
                    if (keepPin) this.autoScrollPin = true;
                    this.keepBottomAnchor();
                    this.ignoreScroll = false;
                });
            }

            setPinHidden(hidden) {
                this.isPinHidden = hidden;
                localStorage.setItem(`chzzk_pin_hidden_${this.channel.id}`, hidden ? "1" : "0");

                this.runWithAnchorPreserved(() => {
                    if (hidden) {
                        this.pinPanelEl.classList.add("hidden");
                        this.resizerEl.classList.add("hidden");
                        this.togglePinBtn.classList.remove("active");
                        this.togglePinBtn.classList.add("muted");
                    } else {
                        this.pinPanelEl.classList.remove("hidden");
                        this.resizerEl.classList.remove("hidden");
                        this.togglePinBtn.classList.add("active");
                        this.togglePinBtn.classList.remove("muted", "has-new");
                    }
                });
            }

            setPinHeightPercent(percent) {
                const clamped = Math.min(75, Math.max(8, percent));
                const val = clamped.toFixed(2) + "%";
                this.runWithAnchorPreserved(() => {
                    this.pinPanelEl.style.height = val;
                    localStorage.setItem(`chzzk_pin_ratio_${this.channel.id}`, val);
                });
            }

            initResizer() {
                let isDragging = false;

                this.onMouseMove = (e) => {
                    if (!isDragging) return;
                    const paneRect = this.chatPaneEl.getBoundingClientRect();
                    const availableH = paneRect.height;
                    if (availableH <= 0) return;

                    const newPinH = e.clientY - paneRect.top;
                    const percent = (newPinH / availableH) * 100;

                    if (e.shiftKey) {
                        clientMap.forEach(c => c.setPinHeightPercent(percent));
                    } else {
                        this.setPinHeightPercent(percent);
                    }
                };

                this.onMouseUp = () => {
                    if (!isDragging) return;
                    isDragging = false;
                    this.resizerEl.classList.remove("dragging");
                    document.body.style.cursor = "";
                    document.body.style.userSelect = "";
                };

                this.resizerEl.addEventListener("mousedown", (e) => {
                    e.preventDefault();
                    isDragging = true;
                    this.resizerEl.classList.add("dragging");
                    document.body.style.cursor = "ns-resize";
                    document.body.style.userSelect = "none";
                });

                this.resizerEl.addEventListener("dblclick", () => {
                    this.setPinHeightPercent(20);
                });

                window.addEventListener("mousemove", this.onMouseMove);
                window.addEventListener("mouseup", this.onMouseUp);
            }

            keepBottomAnchor() {
                if (this.autoScrollMain) {
                    this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
                }
                if (this.autoScrollPin && !this.isPinHidden) {
                    this.pinListEl.scrollTop = this.pinListEl.scrollHeight;
                }
            }

            setSystemStatus(text, isPersistentLine = false) {
                if (
                    isPersistentLine &&
                    this.statusLineEl &&
                    (this.msgsEl.contains(this.statusLineEl) ||
                        this.pendingMainQueue.some(x => x.type === "node" && x.el === this.statusLineEl))
                ) {
                    this.statusLineEl.textContent = text;
                    if (this.autoScrollMain) {
                        this._needScrollMain = true;
                    }
                    scheduleGlobalFlush(this);
                    return;
                }
                const div = document.createElement("div");
                div.className = "msg-row msg-system";
                div.textContent = text;
                this.statusLineEl = isPersistentLine ? div : null;
                this.enqueueMainNode(div);
            }

            enqueueMainNode(el) {
                this.pendingMainQueue.push({ type: "node", el });
                const maxPending = this.autoScrollMain ? MAX_MAIN_NODES_AUTO : MAX_MAIN_NODES_PAUSED;
                if (this.pendingMainQueue.length > maxPending + 40) {
                    this.pendingMainQueue.splice(0, this.pendingMainQueue.length - maxPending);
                }
                scheduleGlobalFlush(this);
            }

            enqueuePinned(role, nickname, color, rawMsg, extras) {
                this.pendingPinQueue.push({
                    role,
                    nickname,
                    color,
                    rawMsg,
                    extras,
                    timeStr: getTimeString()
                });
                if (this.pendingPinQueue.length > MAX_PIN_NODES + 20) {
                    this.pendingPinQueue.splice(0, this.pendingPinQueue.length - MAX_PIN_NODES);
                }
                scheduleGlobalFlush(this);
            }

            enqueueChat(item, isDonation) {
                let parsedProfile = null;
                let parsedExtras = null;
                if (
                    typeof item.profile === "string" &&
                    (item.profile.indexOf("streamer") !== -1 || item.profile.indexOf("manager") !== -1)
                ) {
                    try { parsedProfile = JSON.parse(item.profile); } catch (e) {}
                    const role = parsedProfile?.userRoleCode || "common_user";
                    if (role === "streamer" || role === "streaming_chat_manager" || role === "streaming_channel_manager") {
                        parsedExtras = {};
                        try { if (item.extras) parsedExtras = JSON.parse(item.extras); } catch (e) {}
                        const nick = parsedProfile.nickname || (parsedExtras && parsedExtras.nickname) || "익명";
                        const color = getNicknameColor(parsedProfile, role);
                        this.enqueuePinned(role, nick, color, item.msg || "", parsedExtras);
                    }
                }

                this.pendingMainQueue.push({
                    type: "chat",
                    item,
                    isDonation,
                    parsedProfile,
                    parsedExtras
                });
                const maxPending = this.autoScrollMain ? MAX_MAIN_NODES_AUTO : MAX_MAIN_NODES_PAUSED;
                if (this.pendingMainQueue.length > maxPending + 40) {
                    this.pendingMainQueue.splice(0, this.pendingMainQueue.length - maxPending);
                }
                scheduleGlobalFlush(this);
            }

            // 방종 상태일 때만 30초 간격으로 /api/status(1회 호출)를 통해 방송 시작 여부를 감지
            startLivePoller() {
                if (this.livePollTimer) clearInterval(this.livePollTimer);
                this.livePollTimer = setInterval(async () => {
                    if (this.isDestroyed || this.isConnecting || this.reconnectTimer) return;
                    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
                    if (this.liveStatus === "OPEN") return;

                    try {
                        const res = await fetch(`/api/status?channelId=${this.channel.id}`);
                        const info = await res.json();
                        if (this.isDestroyed || !info.chatChannelId) return;

                        const chatChanged = this.currentChatChannelId && info.chatChannelId !== this.currentChatChannelId;
                        const turnedLive = info.status === "OPEN" && this.liveStatus !== "OPEN";

                        if (chatChanged || turnedLive) {
                            this.setSystemStatus("방송 시작 감지! 새 채팅방으로 전환합니다...", false);
                            this.retryCount = 0;
                            this.connect(true);
                            if (this.isVideoOpen) {
                                this.startVideo();
                            }
                        }
                    } catch (e) {}
                }, 30000);
            }

            scheduleReconnect(reason) {
                if (this.isDestroyed || this.reconnectTimer) return;

                // 지수 백오프: 3초 -> 4.5초 -> 6.8초 -> ... 최대 30초
                const delayMs = Math.min(30000, Math.round(3000 * Math.pow(1.5, this.retryCount)));
                const delaySec = (delayMs / 1000).toFixed(1).replace(/\.0$/, "");
                this.retryCount += 1;

                this.dotEl.className = "status-dot reconnecting";
                this.dotEl.title = `${delaySec}초 후 자동 재연결 시도 (${this.retryCount}회차)`;
                this.setSystemStatus(`${reason} · ${delaySec}초 후 자동 재연결... (${this.retryCount}회차)`, true);

                this.reconnectTimer = setTimeout(() => {
                    this.reconnectTimer = null;
                    if (!this.isDestroyed) {
                        this.connect(true);
                    }
                }, delayMs);
            }

            async connect(isAutoRetry = false) {
                if (this.isDestroyed) return;
                this.disconnectSocket();
                this.isConnecting = true;
                const seq = ++this.connectSeq;

                this.dotEl.className = "status-dot reconnecting";
                this.dotEl.title = "채팅 서버 연결 중...";
                this.setSystemStatus(
                    isAutoRetry ? `채팅 서버 재연결 시도 중... (${this.retryCount}회차)` : "채팅 서버 연결 중...",
                    true
                );

                try {
                    const res = await fetch(`/api/token?channelId=${this.channel.id}`);
                    const creds = await res.json();

                    if (this.isDestroyed || seq !== this.connectSeq) return;

                    if (creds.error) {
                        this.isConnecting = false;
                        this.scheduleReconnect(`연결 실패(${creds.error})`);
                        return;
                    }

                    const ws = new WebSocket("wss://kr-ss1.chat.naver.com/chat");
                    this.ws = ws;

                    ws.onopen = () => {
                        if (this.isDestroyed || seq !== this.connectSeq) return;
                        ws.send(JSON.stringify({
                            ver: "2",
                            cmd: 100,
                            svcid: "game",
                            cid: creds.chatChannelId,
                            bdy: {
                                uid: null,
                                devType: 2001,
                                accTkn: creds.accessToken,
                                auth: "READ"
                            },
                            tid: 1
                        }));
                    };

                    ws.onmessage = (event) => {
                        if (this.isDestroyed || seq !== this.connectSeq) return;
                        const data = JSON.parse(event.data);

                        if (data.cmd === 10100) {
                            const wasRetry = this.retryCount > 0;
                            this.isConnecting = false;
                            this.retryCount = 0;
                            this.currentChatChannelId = creds.chatChannelId;
                            this.liveStatus = creds.status;

                            this.dotEl.className = "status-dot connected";
                            const stateText = creds.status === "OPEN" ? "방송 중" : "방종 상태";
                            this.dotEl.title = `연결됨 (${stateText})`;
                            this.setSystemStatus(
                                wasRetry ? `재연결 완료 (${stateText})` : `연결 완료 (${stateText})`,
                                true
                            );
                            this.statusLineEl = null;

                            if (this.pingTimer) clearInterval(this.pingTimer);
                            this.pingTimer = setInterval(() => {
                                if (ws.readyState === WebSocket.OPEN) {
                                    ws.send(JSON.stringify({ ver: "2", cmd: 10000 }));
                                }
                            }, 20000);
                        } else if (data.cmd === 0) {
                            ws.send(JSON.stringify({ ver: "2", cmd: 10000 }));
                        } else if (data.cmd === 93101 || data.cmd === 93102) {
                            const list = data.bdy || [];
                            const isDona = data.cmd === 93102;
                            for (let i = 0; i < list.length; i++) {
                                this.enqueueChat(list[i], isDona);
                            }
                        }
                    };

                    ws.onclose = () => {
                        if (this.isDestroyed || seq !== this.connectSeq) return;
                        this.isConnecting = false;
                        if (this.pingTimer) {
                            clearInterval(this.pingTimer);
                            this.pingTimer = null;
                        }
                        this.ws = null;
                        this.scheduleReconnect("채팅 연결 종료");
                    };
                } catch (err) {
                    if (this.isDestroyed || seq !== this.connectSeq) return;
                    this.isConnecting = false;
                    this.scheduleReconnect("네트워크 오류");
                }
            }

            createRoleBadge(isStreamer) {
                const badge = document.createElement("span");
                badge.className = isStreamer ? "role-badge badge-streamer" : "role-badge badge-manager";
                badge.textContent = isStreamer ? "스트리머" : "매니저";
                return badge;
            }

            buildPinnedNode(entry) {
                const isStreamer = entry.role === "streamer";
                const item = document.createElement("div");
                item.className = `pinned-item ${isStreamer ? "role-streamer" : "role-manager"}`;

                const timeSpan = document.createElement("span");
                timeSpan.className = "pin-time";
                timeSpan.textContent = `[${entry.timeStr}]`;

                const nickSpan = document.createElement("span");
                nickSpan.className = "nickname";
                nickSpan.style.color = entry.color;
                nickSpan.textContent = entry.nickname;

                const textSpan = document.createElement("span");
                textSpan.className = "msg-text";
                appendFormattedText(textSpan, entry.rawMsg, entry.extras);

                item.appendChild(timeSpan);
                item.appendChild(this.createRoleBadge(isStreamer));
                item.appendChild(nickSpan);
                item.appendChild(textSpan);
                return item;
            }

            buildChatNode(item, isDonation, parsedProfile, parsedExtras) {
                let profile = parsedProfile || {};
                let extras = parsedExtras || {};
                if (!parsedProfile && item.profile) {
                    try { profile = JSON.parse(item.profile); } catch (e) {}
                }
                if (!parsedExtras && item.extras) {
                    try { extras = JSON.parse(item.extras); } catch (e) {}
                }

                const role = profile?.userRoleCode || "common_user";
                const isStreamer = role === "streamer";
                const isManager = role === "streaming_chat_manager" || role === "streaming_channel_manager";
                const nickname = profile.nickname || extras.nickname || "익명";

                const div = document.createElement("div");
                if (isDonation) {
                    const amount = Number(extras.payAmount || 0).toLocaleString();
                    div.className = "msg-row msg-donation";

                    const head = document.createElement("div");
                    head.className = "donation-header";
                    head.textContent = `🧀 ${nickname} · ${amount}치즈`;

                    const textDiv = document.createElement("div");
                    textDiv.className = "msg-text";
                    appendFormattedText(textDiv, item.msg || "", extras);

                    div.appendChild(head);
                    div.appendChild(textDiv);
                    return div;
                }

                let rowClass = "msg-row";
                if (isStreamer) rowClass += " highlight-streamer";
                else if (isManager) rowClass += " highlight-manager";
                div.className = rowClass;

                if (isStreamer || isManager) {
                    div.appendChild(this.createRoleBadge(isStreamer));
                }

                const nickSpan = document.createElement("span");
                nickSpan.className = "nickname";
                nickSpan.style.color = getNicknameColor(profile, role);
                nickSpan.textContent = nickname;

                const textSpan = document.createElement("span");
                textSpan.className = "msg-text";
                appendFormattedText(textSpan, item.msg || "", extras);

                div.appendChild(nickSpan);
                div.appendChild(textSpan);
                return div;
            }

            setVideoMessage(msg) {
                if (msg) {
                    this.videoMsgEl.textContent = msg;
                    this.videoMsgEl.classList.remove("hidden");
                } else {
                    this.videoMsgEl.textContent = "";
                    this.videoMsgEl.classList.add("hidden");
                }
            }

            setVideoOpen(open) {
                if (!open && focusedClient === this) {
                    exitFocusMode();
                }
                if (this.isVideoOpen === open) return;
                this.isVideoOpen = open;
                this.runWithAnchorPreserved(() => {
                    if (open) {
                        this.videoPanelEl.classList.remove("hidden");
                        this.toggleVideoBtn.classList.add("active");
                    } else {
                        this.videoPanelEl.classList.add("hidden");
                        this.toggleVideoBtn.classList.remove("active");
                    }
                });
                if (open) {
                    this.startVideo();
                } else {
                    this.stopVideo();
                }
            }

            setVideoMuted(muted) {
                this.videoEl.muted = !!muted;
                this.videoMuteBtn.textContent = this.videoEl.muted ? "🔇" : "🔊";
            }

            toggleVideoMute() {
                this.setVideoMuted(!this.videoEl.muted);
            }

            applyPreferredVideoLevel() {
                if (!this.hls || !Array.isArray(this.hls.levels) || this.hls.levels.length === 0) return;
                const levels = this.hls.levels;
                let chosenIdx = -1;

                if (this.preferredQuality === 0) {
                    let maxH = -1;
                    let maxBitrate = -1;
                    for (let i = 0; i < levels.length; i++) {
                        const h = levels[i].height || 0;
                        const b = levels[i].bitrate || 0;
                        if (h > maxH || (h === maxH && b > maxBitrate)) {
                            maxH = h;
                            maxBitrate = b;
                            chosenIdx = i;
                        }
                    }
                } else {
                    let bestHeight = 0;
                    let bestBitrate = -1;
                    for (let i = 0; i < levels.length; i++) {
                        const h = levels[i].height || 0;
                        const b = levels[i].bitrate || 0;
                        if (h > 0 && h <= this.preferredQuality && (h > bestHeight || (h === bestHeight && b > bestBitrate))) {
                            bestHeight = h;
                            bestBitrate = b;
                            chosenIdx = i;
                        }
                    }
                    if (chosenIdx === -1) {
                        let minH = Infinity;
                        for (let i = 0; i < levels.length; i++) {
                            const h = levels[i].height || Infinity;
                            if (h < minH) {
                                minH = h;
                                chosenIdx = i;
                            }
                        }
                    }
                }

                if (chosenIdx >= 0) {
                    this.hls.currentLevel = chosenIdx;
                    const actualH = levels[chosenIdx].height;
                    this.videoQualBtn.textContent = actualH
                        ? `${actualH}p`
                        : (this.preferredQuality === 0 ? "원본" : `${this.preferredQuality}p`);
                }
            }

            cycleVideoQuality() {
                const order = [360, 480, 720, 0];
                const idx = order.indexOf(this.preferredQuality);
                this.preferredQuality = order[(idx + 1) % order.length];
                this.videoQualBtn.textContent = this.preferredQuality === 0 ? "원본" : `${this.preferredQuality}p`;
                this.applyPreferredVideoLevel();
            }

            stopVideo() {
                this.videoLoadSeq += 1;
                if (this.hls) {
                    this.hls.destroy();
                    this.hls = null;
                }
                this.videoEl.pause();
                this.videoEl.removeAttribute("src");
                this.videoEl.load();
                this.setVideoMessage("");
            }

            async startVideo() {
                if (this.isDestroyed || !this.isVideoOpen) return;
                this.stopVideo();
                const seq = this.videoLoadSeq;
                this.setVideoMessage("방송 스트림 불러오는 중...");

                try {
                    const [res, HlsLib] = await Promise.all([
                        fetch(`/api/stream?channelId=${this.channel.id}`),
                        ensureHlsLib()
                    ]);
                    if (this.isDestroyed || !this.isVideoOpen || seq !== this.videoLoadSeq) return;

                    const info = await res.json();
                    if (info.error || !info.hlsUrl) {
                        this.setVideoMessage(info.error || "재생할 수 있는 방송이 없습니다.");
                        return;
                    }

                    const badgeTxt = info.category || "LIVE";
                    this.videoBadgeEl.textContent = info.userCount
                        ? `${badgeTxt} · ${info.userCount.toLocaleString()}명`
                        : badgeTxt;
                    this.videoPanelEl.title = info.title || "";

                    if (HlsLib && HlsLib.isSupported()) {
                        const hls = new HlsLib({
                            lowLatencyMode: true,
                            autoStartLoad: false,
                            liveSyncDuration: 2.0,
                            liveMaxLatencyDuration: 3.5,
                            maxLiveSyncPlaybackRate: 1.25,
                            maxBufferLength: 4,
                            maxMaxBufferLength: 8,
                            backBufferLength: 4,
                            capLevelToPlayerSize: false
                        });
                        this.hls = hls;
                        let retriedMedia = false;
                        let usedFallback = false;

                        hls.on(HlsLib.Events.MANIFEST_PARSED, () => {
                            if (this.isDestroyed || seq !== this.videoLoadSeq) return;
                            this.applyPreferredVideoLevel();
                            hls.startLoad(-1);
                            this.setVideoMessage("");
                            this.videoEl.play().catch(() => {});
                        });

                        hls.on(HlsLib.Events.ERROR, (evt, data) => {
                            if (this.isDestroyed || seq !== this.videoLoadSeq || !data?.fatal) return;
                            if (!retriedMedia && data.type === HlsLib.ErrorTypes.MEDIA_ERROR) {
                                retriedMedia = true;
                                hls.recoverMediaError();
                                return;
                            }
                            if (!usedFallback && info.hlsFallbackUrl) {
                                usedFallback = true;
                                hls.loadSource(info.hlsFallbackUrl);
                                return;
                            }
                            this.setVideoMessage("영상 연결이 끊어졌습니다. (↻ 새로고침)");
                        });

                        hls.loadSource(info.hlsUrl);
                        hls.attachMedia(this.videoEl);
                    } else if (this.videoEl.canPlayType("application/vnd.apple.mpegurl")) {
                        this.videoEl.src = info.hlsUrl;
                        this.setVideoMessage("");
                        this.videoEl.play().catch(() => {});
                    } else {
                        this.setVideoMessage("이 브라우저는 HLS 재생을 지원하지 않습니다.");
                    }
                } catch (err) {
                    if (this.isDestroyed || seq !== this.videoLoadSeq) return;
                    this.setVideoMessage("영상 로드 중 오류가 발생했습니다.");
                }
            }

            snapToLiveEdge() {
                try {
                    if (this.hls && typeof this.hls.liveSyncPosition === "number" && this.hls.liveSyncPosition > 0) {
                        if (this.hls.liveSyncPosition - this.videoEl.currentTime > 3.5) {
                            this.videoEl.currentTime = this.hls.liveSyncPosition;
                        }
                    }
                } catch (e) {}
            }

            disconnectSocket() {
                if (this.reconnectTimer) {
                    clearTimeout(this.reconnectTimer);
                    this.reconnectTimer = null;
                }
                if (this.pingTimer) {
                    clearInterval(this.pingTimer);
                    this.pingTimer = null;
                }
                if (this.ws) {
                    this.ws.onopen = null;
                    this.ws.onmessage = null;
                    this.ws.onclose = null;
                    this.ws.onerror = null;
                    this.ws.close();
                    this.ws = null;
                }
            }

            destroy() {
                if (focusedClient === this) {
                    exitFocusMode(true);
                }
                this.isDestroyed = true;
                this.isConnecting = false;
                dirtyClients.delete(this);
                this.pendingMainQueue.length = 0;
                this.pendingPinQueue.length = 0;
                if (this.livePollTimer) {
                    clearInterval(this.livePollTimer);
                    this.livePollTimer = null;
                }
                this.stopVideo();
                this.disconnectSocket();
                if (this.ro) this.ro.disconnect();
                window.removeEventListener("mousemove", this.onMouseMove);
                window.removeEventListener("mouseup", this.onMouseUp);
                this.container.remove();
            }
        }

        function syncLayout() {
            const wrapper = document.getElementById("wrapper");

            const activeIds = new Set(activeChannels.map(c => c.id));
            for (const [id, client] of clientMap.entries()) {
                if (!activeIds.has(id)) {
                    client.destroy();
                    clientMap.delete(id);
                }
            }

            if (activeChannels.length === 0) {
                wrapper.style.gridTemplateColumns = "1fr";
                wrapper.innerHTML = `
                    <div class="empty-screen">
                        <p>표시 중인 스트리머가 없습니다.</p>
                        <button class="btn-primary" id="btn-empty-add">+ 스트리머 추가하기</button>
                    </div>
                `;
                document.getElementById("btn-empty-add").onclick = openManageModal;
                localStorage.setItem(STORAGE_CHANNELS_KEY, "[]");
                renderModalActiveList();
                return;
            }

            const emptyScreen = wrapper.querySelector(".empty-screen");
            if (emptyScreen) emptyScreen.remove();

            wrapper.style.gridTemplateColumns = `repeat(${activeChannels.length}, 1fr)`;

            activeChannels.forEach((ch) => {
                let client = clientMap.get(ch.id);
                if (!client) {
                    const box = document.createElement("div");
                    box.className = "chat-box";
                    client = new ChatClient(ch, box);
                    clientMap.set(ch.id, client);
                }
                wrapper.appendChild(client.container);

                client.closeColBtn.style.display = activeChannels.length > 1 ? "inline-block" : "none";
            });

            localStorage.setItem(STORAGE_CHANNELS_KEY, JSON.stringify(activeChannels));
            renderModalActiveList();
        }

        let saveHistoryTimer = null;

        function scheduleSaveHistoryToServer() {
            if (saveHistoryTimer !== null) clearTimeout(saveHistoryTimer);
            saveHistoryTimer = setTimeout(() => {
                saveHistoryTimer = null;
                fetch("/api/history", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(activeChannels),
                    keepalive: true
                }).catch(() => {});
            }, 800);
        }

        function addChannel(ch) {
            if (activeChannels.some(c => c.id === ch.id)) {
                setSearchStatus("이미 화면에 추가된 스트리머입니다.");
                return false;
            }
            if (activeChannels.length >= 6) {
                setSearchStatus("최대 6명까지만 동시에 볼 수 있습니다. 기존 칸을 닫고 추가하세요.");
                return false;
            }
            activeChannels.push({ id: ch.id, name: ch.name });
            syncLayout();
            scheduleSaveHistoryToServer();
            return true;
        }

        function removeChannel(targetId) {
            if (activeChannels.length <= 1) return;
            activeChannels = activeChannels.filter(c => c.id !== targetId);
            syncLayout();
            scheduleSaveHistoryToServer();
        }

        function moveChannel(index, direction) {
            const newIndex = index + direction;
            if (newIndex < 0 || newIndex >= activeChannels.length) return;
            const temp = activeChannels[index];
            activeChannels[index] = activeChannels[newIndex];
            activeChannels[newIndex] = temp;
            syncLayout();
            scheduleSaveHistoryToServer();
        }

        const modalBackdrop = document.getElementById("modal-backdrop");
        const modalTitle = document.getElementById("modal-title");
        const searchInput = document.getElementById("search-input");
        const searchStatus = document.getElementById("search-status");
        const searchResultsEl = document.getElementById("search-results");
        const activeSectionEl = document.getElementById("active-section");

        const cookieModalBackdrop = document.getElementById("cookie-modal-backdrop");
        const cookieBtn = document.getElementById("btn-cookie-settings");
        const cookieStatusTxt = document.getElementById("cookie-status-txt");
        const cookieAutInput = document.getElementById("cookie-nid-aut");
        const cookieSesInput = document.getElementById("cookie-nid-ses");

        function applyCookieUiState(info) {
            const has = !!(info && info.hasCookie);
            if (has) {
                cookieBtn.classList.add("active");
                cookieBtn.title = `네이버 쿠키 설정됨 (${info.nidAutPreview || ""})`;
                cookieStatusTxt.textContent = `현재 상태: 설정됨 (${info.nidAutPreview || "NID_AUT"})`;
                cookieStatusTxt.style.color = "#00ffa3";
            } else {
                cookieBtn.classList.remove("active");
                cookieBtn.title = "네이버 로그인 쿠키 설정 (성인 인증 방송용)";
                cookieStatusTxt.textContent = "현재 상태: 미설정";
                cookieStatusTxt.style.color = "#a1a1aa";
            }
        }

        async function refreshCookieStatus() {
            try {
                const res = await fetch("/api/cookies");
                const info = await res.json();
                applyCookieUiState(info);
            } catch (e) {}
        }

        function openCookieModal() {
            cookieAutInput.value = "";
            cookieSesInput.value = "";
            refreshCookieStatus();
            cookieModalBackdrop.classList.add("open");
            setTimeout(() => cookieAutInput.focus(), 30);
        }

        function closeCookieModal() {
            cookieModalBackdrop.classList.remove("open");
        }

        cookieBtn.onclick = openCookieModal;
        document.getElementById("cookie-modal-close").onclick = closeCookieModal;
        cookieModalBackdrop.onclick = (e) => {
            if (e.target === cookieModalBackdrop) closeCookieModal();
        };

        document.getElementById("btn-cookie-save").onclick = async () => {
            const nidAut = cookieAutInput.value.trim();
            const nidSes = cookieSesInput.value.trim();
            if (!nidAut || !nidSes) {
                cookieStatusTxt.textContent = "NID_AUT와 NID_SES를 모두 입력하세요.";
                cookieStatusTxt.style.color = "#ff6b6b";
                return;
            }
            try {
                const res = await fetch("/api/cookies", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ nidAut, nidSes })
                });
                const info = await res.json();
                applyCookieUiState(info);
                cookieAutInput.value = "";
                cookieSesInput.value = "";
                cookieStatusTxt.textContent = "저장 완료! (성인 방송도 📺 새로고침 시 반영됩니다)";
                cookieStatusTxt.style.color = "#00ffa3";
            } catch (e) {
                cookieStatusTxt.textContent = "저장 중 오류가 발생했습니다.";
                cookieStatusTxt.style.color = "#ff6b6b";
            }
        };

        document.getElementById("btn-cookie-clear").onclick = async () => {
            try {
                const res = await fetch("/api/cookies", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ clear: true })
                });
                const info = await res.json();
                applyCookieUiState(info);
                cookieAutInput.value = "";
                cookieSesInput.value = "";
            } catch (e) {}
        };

        function setSearchStatus(msg) {
            searchStatus.textContent = msg;
        }

        let modalHistory = [];

        async function loadModalHistory() {
            try {
                const res = await fetch("/api/history");
                const items = await res.json();
                if (Array.isArray(items)) {
                    modalHistory = items;
                    renderModalHistory();
                }
            } catch (e) {}
        }

        function applyComboFromHistory(comboIdx) {
            const combo = modalHistory[comboIdx];
            if (!Array.isArray(combo) || combo.length === 0) return;
            activeChannels = combo.slice(0, 6).map(c => ({ id: c.id, name: c.name }));
            syncLayout();
            scheduleSaveHistoryToServer();
            closeModal();
        }

        async function deleteHistoryEntry(comboIdx) {
            try {
                const res = await fetch("/api/history/delete", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ index: comboIdx })
                });
                const data = await res.json();
                if (data.ok && Array.isArray(data.history)) {
                    modalHistory = data.history;
                    renderModalHistory();
                }
            } catch (e) {}
        }

        function renderModalHistory() {
            const listEl = document.getElementById("history-list");
            const countEl = document.getElementById("history-count-label");
            const titleEl = document.getElementById("history-section-title");
            if (!listEl) return;

            titleEl.textContent = "📜 최근 기록 (칩 클릭: 1명 추가 / 우클릭: 메뉴)";
            countEl.textContent = `${modalHistory.length} / 9`;
            listEl.innerHTML = "";

            if (modalHistory.length === 0) {
                const empty = document.createElement("div");
                empty.className = "history-empty";
                empty.textContent = "저장된 최근 기록이 없습니다.";
                listEl.appendChild(empty);
                return;
            }

            modalHistory.forEach((combo, comboIdx) => {
                const row = document.createElement("div");
                row.className = "history-row";
                row.oncontextmenu = (e) => {
                    if (!e.target.closest(".history-chip")) {
                        openHistoryContextMenu(e, comboIdx, null);
                    }
                };

                const idxSpan = document.createElement("span");
                idxSpan.className = "history-idx";
                idxSpan.textContent = `[${comboIdx + 1}]`;

                const chipsEl = document.createElement("div");
                chipsEl.className = "history-chips";

                combo.forEach((ch) => {
                    const chip = document.createElement("button");
                    chip.type = "button";
                    chip.className = "history-chip";
                    chip.textContent = ch.name;
                    chip.title = `'${ch.name}' 추가 (우클릭: 메뉴)`;
                    chip.onclick = () => applySelectedStreamer(ch);
                    chip.oncontextmenu = (e) => openHistoryContextMenu(e, comboIdx, ch);
                    chipsEl.appendChild(chip);
                });

                const actionsEl = document.createElement("div");
                actionsEl.className = "history-actions";

                const loadBtn = document.createElement("button");
                loadBtn.type = "button";
                loadBtn.className = "btn-mini";
                loadBtn.textContent = "전체";
                loadBtn.title = "이 조합 전체로 화면 전환";
                loadBtn.onclick = () => applyComboFromHistory(comboIdx);
                actionsEl.appendChild(loadBtn);

                const delBtn = document.createElement("button");
                delBtn.type = "button";
                delBtn.className = "btn-mini btn-close-col";
                delBtn.textContent = "×";
                delBtn.title = "이 기록 삭제";
                delBtn.onclick = () => deleteHistoryEntry(comboIdx);
                actionsEl.appendChild(delBtn);

                row.appendChild(idxSpan);
                row.appendChild(chipsEl);
                row.appendChild(actionsEl);
                listEl.appendChild(row);
            });
        }

        function openManageModal() {
            modalTitle.textContent = "스트리머 추가 · 편성 관리";
            activeSectionEl.style.display = "block";
            searchInput.value = "";
            searchResultsEl.innerHTML = "";
            setSearchStatus("이름, 채널 ID 또는 치지직 URL을 입력하고 엔터를 누르세요.");
            renderModalActiveList();
            renderModalHistory();
            loadModalHistory();
            modalBackdrop.classList.add("open");
            setTimeout(() => searchInput.focus(), 30);
        }

        function closeModal() {
            closeHistoryContextMenu();
            modalBackdrop.classList.remove("open");
        }

        document.getElementById("btn-manage-streamers").onclick = openManageModal;
        document.getElementById("modal-close").onclick = closeModal;
        modalBackdrop.onclick = (e) => {
            if (e.target === modalBackdrop) closeModal();
        };

        function renderModalActiveList() {
            const listEl = document.getElementById("active-list");
            const countLabel = document.getElementById("active-count-label");
            if (!listEl) return;

            countLabel.textContent = `${activeChannels.length} / 6`;
            listEl.innerHTML = "";

            activeChannels.forEach((ch, idx) => {
                const row = document.createElement("div");
                row.className = "active-item";
                row.innerHTML = `
                    <span><b>${idx + 1}.</b> ${escapeHtml(ch.name)}</span>
                    <div class="active-item-actions">
                        <button class="btn-mini" data-act="left" ${idx === 0 ? "disabled" : ""} title="왼쪽으로 이동">◀</button>
                        <button class="btn-mini" data-act="right" ${idx === activeChannels.length - 1 ? "disabled" : ""} title="오른쪽으로 이동">▶</button>
                        <button class="btn-mini btn-close-col" data-act="del" ${activeChannels.length <= 1 ? "disabled" : ""}>삭제</button>
                    </div>
                `;
                row.querySelector('[data-act="left"]').onclick = () => moveChannel(idx, -1);
                row.querySelector('[data-act="right"]').onclick = () => moveChannel(idx, 1);
                row.querySelector('[data-act="del"]').onclick = () => removeChannel(ch.id);
                listEl.appendChild(row);
            });
        }

        async function executeSearch(autoSelectFirst = false) {
            const keyword = searchInput.value.trim();
            if (!keyword) return;

            setSearchStatus(`'${keyword}' 검색 중...`);
            searchResultsEl.innerHTML = "";

            try {
                const res = await fetch(`/api/search?keyword=${encodeURIComponent(keyword)}`);
                const items = await res.json();

                if (!Array.isArray(items) || items.length === 0) {
                    setSearchStatus("검색 결과가 없습니다.");
                    return;
                }

                if (autoSelectFirst) {
                    applySelectedStreamer(items[0]);
                    return;
                }

                setSearchStatus("원하는 채널을 클릭하세요.");
                items.forEach(item => {
                    const div = document.createElement("div");
                    div.className = "result-item";
                    const liveTag = item.openLive
                        ? `<span class="live-badge on">LIVE</span>`
                        : `<span class="live-badge off">오프라인</span>`;
                    const followers = item.followerCount ? `팔로워 ${item.followerCount.toLocaleString()}명` : "";

                    div.innerHTML = `
                        <div class="result-info">
                            ${liveTag}
                            <b>${escapeHtml(item.name)}</b>
                        </div>
                        <span class="follower-txt">${followers}</span>
                    `;
                    div.onclick = () => applySelectedStreamer(item);
                    searchResultsEl.appendChild(div);
                });
            } catch (err) {
                setSearchStatus("검색 중 네트워크 오류가 발생했습니다.");
            }
        }

        function applySelectedStreamer(item) {
            if (addChannel(item)) {
                searchInput.value = "";
                searchResultsEl.innerHTML = "";
                setSearchStatus(`'${item.name}' 채널이 추가되었습니다.`);
            }
        }

        document.getElementById("btn-search").onclick = () => executeSearch(false);
        searchInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                executeSearch(true);
            }
        });

        let globalToolsIntroTimer = null;
        let globalToolsPendingCollapse = false;

        function finishGlobalToolsIntro(shouldCollapse = true) {
            const tools = document.getElementById("global-tools");
            if (!tools) return;
            if (globalToolsIntroTimer) {
                clearTimeout(globalToolsIntroTimer);
                globalToolsIntroTimer = null;
            }
            globalToolsPendingCollapse = false;
            tools.classList.remove("intro-spotlight");
            if (shouldCollapse) {
                tools.classList.add("is-collapsed");
            }
        }

        function triggerGlobalToolsIntro() {
            const tools = document.getElementById("global-tools");
            if (!tools) return;
            tools.classList.remove("is-collapsed");
            tools.classList.add("intro-spotlight");
            globalToolsPendingCollapse = false;
            if (globalToolsIntroTimer) clearTimeout(globalToolsIntroTimer);
            globalToolsIntroTimer = setTimeout(() => {
                globalToolsIntroTimer = null;
                if (tools.matches(":hover")) {
                    globalToolsPendingCollapse = true;
                    return;
                }
                finishGlobalToolsIntro(true);
            }, 5500);
        }

        (function setupGlobalTools() {
            const tools = document.getElementById("global-tools");
            const expandBtn = document.getElementById("btn-tools-expand");
            const collapseBtn = document.getElementById("btn-tools-collapse");
            const hintEl = document.getElementById("global-tools-hint");
            const hintCloseBtn = document.getElementById("btn-tools-hint-close");
            if (!tools) return;

            let expandNearHover = false;
            function setExpandNearHover(next) {
                if (!expandBtn || expandNearHover === next) return;
                expandNearHover = next;
                expandBtn.classList.toggle("near-hover", next);
            }

            tools.addEventListener("mouseleave", () => {
                if (globalToolsPendingCollapse) {
                    finishGlobalToolsIntro(true);
                }
            });

            if (expandBtn) {
                expandBtn.addEventListener("click", () => {
                    globalToolsPendingCollapse = false;
                    setExpandNearHover(false);
                    tools.classList.remove("is-collapsed");
                });
            }
            if (collapseBtn) {
                collapseBtn.addEventListener("click", () => {
                    setExpandNearHover(false);
                    finishGlobalToolsIntro(true);
                });
            }
            if (hintCloseBtn) {
                hintCloseBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    finishGlobalToolsIntro(true);
                });
            }
            if (hintEl) {
                hintEl.addEventListener("click", () => {
                    finishGlobalToolsIntro(true);
                });
            }

            document.addEventListener("mousemove", (e) => {
                if (!expandBtn || !tools.classList.contains("is-collapsed")) {
                    setExpandNearHover(false);
                    return;
                }
                if (window.innerWidth - e.clientX > 56) {
                    setExpandNearHover(false);
                    return;
                }
                const rect = expandBtn.getBoundingClientRect();
                const nearX = e.clientX >= rect.right - 56;
                const nearY = e.clientY >= rect.top - 32 && e.clientY <= rect.bottom + 32;
                setExpandNearHover(nearX && nearY);
            }, { passive: true });
            document.addEventListener("mouseleave", () => setExpandNearHover(false));
            window.addEventListener("blur", () => setExpandNearHover(false));

            document.addEventListener("pointerdown", (e) => {
                if (!tools.classList.contains("is-collapsed") && !tools.contains(e.target)) {
                    setExpandNearHover(false);
                    finishGlobalToolsIntro(true);
                }
            });

            triggerGlobalToolsIntro();
        })();

        applyFontSize(currentFontSize);
        syncLayout();
        refreshCookieStatus();
        if (activeChannels.length === 0) {
            openManageModal();
        }
    </script>
</body>
</html>
"""

class MultiChatHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_bytes(self, status, content_type, data_bytes, compress=False):
        payload = data_bytes
        use_gzip = False
        if compress and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            payload = gzip.compress(data_bytes, compresslevel=6)
            use_gzip = True

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            with history_lock:
                channels_snapshot = list(INITIAL_CHANNELS)
            html = HTML_TEMPLATE.replace(
                "__CHANNELS_JSON__", json.dumps(channels_snapshot, ensure_ascii=False)
            )
            self.send_bytes(200, "text/html; charset=utf-8", html.encode("utf-8"), compress=True)

        elif parsed.path == "/api/status":
            qs = urllib.parse.parse_qs(parsed.query)
            channel_id = (qs.get("channelId") or [""])[0]
            result = get_live_status(channel_id)
            self.send_bytes(
                200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8")
            )

        elif parsed.path == "/api/token":
            qs = urllib.parse.parse_qs(parsed.query)
            channel_id = (qs.get("channelId") or [""])[0]
            result = get_chat_credentials(channel_id)
            self.send_bytes(
                200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8")
            )

        elif parsed.path == "/api/stream":
            qs = urllib.parse.parse_qs(parsed.query)
            channel_id = (qs.get("channelId") or [""])[0]
            result = get_stream_info(channel_id)
            self.send_bytes(
                200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8")
            )

        elif parsed.path == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            keyword = (qs.get("keyword") or [""])[0].strip()
            results = search_channels(keyword, size=6)
            self.send_bytes(
                200, "application/json; charset=utf-8", json.dumps(results, ensure_ascii=False).encode("utf-8")
            )

        elif parsed.path == "/api/history":
            history = load_history()
            self.send_bytes(
                200, "application/json; charset=utf-8", json.dumps(history, ensure_ascii=False).encode("utf-8")
            )

        elif parsed.path == "/api/cookies":
            status = get_cookie_status()
            self.send_bytes(
                200, "application/json; charset=utf-8", json.dumps(status, ensure_ascii=False).encode("utf-8")
            )

        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def do_POST(self):
        global INITIAL_CHANNELS
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/history":
            body = self.read_json_body()
            channels = sanitize_channels(body)
            if body is None:
                self.send_bytes(400, "application/json; charset=utf-8", b'{"ok":false}')
                return
            if channels:
                with history_lock:
                    INITIAL_CHANNELS = channels
                record_history_entry(channels)
            self.send_bytes(200, "application/json; charset=utf-8", b'{"ok":true}')

        elif parsed.path == "/api/history/delete":
            body = self.read_json_body()
            if not isinstance(body, dict) or not isinstance(body.get("index"), int):
                self.send_bytes(400, "application/json; charset=utf-8", b'{"ok":false}')
                return
            idx = body["index"]
            history = load_history()
            if 0 <= idx < len(history):
                history.pop(idx)
                save_history(history)
            self.send_bytes(
                200,
                "application/json; charset=utf-8",
                json.dumps({"ok": True, "history": history}, ensure_ascii=False).encode("utf-8"),
            )

        elif parsed.path == "/api/cookies":
            body = self.read_json_body()
            if not isinstance(body, dict):
                self.send_bytes(400, "application/json; charset=utf-8", b'{"ok":false}')
                return
            if body.get("clear"):
                clear_cookies()
            else:
                nid_aut = str(body.get("nidAut") or "").strip()
                nid_ses = str(body.get("nidSes") or "").strip()
                if not nid_aut or not nid_ses:
                    self.send_bytes(400, "application/json; charset=utf-8", b'{"ok":false}')
                    return
                save_cookies(nid_aut, nid_ses)
            status = get_cookie_status()
            status["ok"] = True
            self.send_bytes(
                200, "application/json; charset=utf-8", json.dumps(status, ensure_ascii=False).encode("utf-8")
            )

        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

def print_history(history):
    if not history:
        return
    print("--------------------------------------------------")
    print(f" 📜 최근 기록 (최대 {MAX_HISTORY}개):")
    for idx, entry in enumerate(history, 1):
        names = ", ".join(ch["name"] for ch in entry)
        print(f"   [{idx}] {names}")
    print("--------------------------------------------------")

def load_prompt_toolkit():
    """prompt_toolkit을 불러오고, 없으면 pip로 자동 설치를 시도합니다."""
    def _import_pt():
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.layout import Layout
        from prompt_toolkit.layout.processors import AfterInput, BeforeInput, ConditionalProcessor
        from prompt_toolkit.mouse_events import MouseButton, MouseEventType
        return {
            "Application": Application,
            "Buffer": Buffer,
            "Condition": Condition,
            "FormattedText": FormattedText,
            "KeyBindings": KeyBindings,
            "HSplit": HSplit,
            "Window": Window,
            "BufferControl": BufferControl,
            "FormattedTextControl": FormattedTextControl,
            "Layout": Layout,
            "AfterInput": AfterInput,
            "BeforeInput": BeforeInput,
            "ConditionalProcessor": ConditionalProcessor,
            "MouseButton": MouseButton,
            "MouseEventType": MouseEventType,
        }

    try:
        return _import_pt()
    except ImportError:
        print(" 📦 입력창 안내·목록 기능을 위해 'prompt_toolkit'을 자동 설치합니다...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "prompt_toolkit"])
            pt = _import_pt()
            print(" ✅ 설치 완료!\n")
            return pt
        except Exception as e:
            print(f" ⚠️ 자동 설치에 실패해 기본 입력 모드로 실행합니다. ({e})\n")
            return None

def build_history_fragments(pt, history, buf):
    """입력줄 아래에 표시할 클릭 가능한 최근 기록 텍스트 조각을 생성합니다."""
    MouseEventType = pt["MouseEventType"]
    MouseButton = pt["MouseButton"]
    FormattedText = pt["FormattedText"]

    def make_click_handler(value_to_insert):
        def handler(mouse_event):
            if (
                mouse_event.event_type == MouseEventType.MOUSE_UP
                and mouse_event.button == MouseButton.LEFT
            ):
                cur = buf.text.strip()
                if cur:
                    buf.text = cur.rstrip(", ") + ", " + value_to_insert
                else:
                    buf.text = value_to_insert
                buf.cursor_position = len(buf.text)
                return None
            return NotImplemented
        return handler

    frags = [
        ("fg:#52525b", "--------------------------------------------------\n"),
        ("fg:#a1a1aa bold", f" 📜 최근 기록 (클릭해서 입력 · 최대 {MAX_HISTORY}개)\n"),
    ]
    for idx, entry in enumerate(history, 1):
        frags.append(("", "   "))
        frags.append(("fg:#00ffa3 bold", f"[{idx}]", make_click_handler(str(idx))))
        frags.append(("", " "))
        for sub_idx, ch in enumerate(entry):
            if sub_idx > 0:
                frags.append(("fg:#71717a", ", "))
            frags.append(("fg:#e4e4e7", ch["name"], make_click_handler(ch["name"])))
        frags.append(("", "\n"))
    return FormattedText(frags)

def read_cli_input(pt, history):
    """회색 placeholder와 하단 클릭형 최근 기록을 포함한 CLI 입력을 받습니다."""
    if history:
        recent_names = ", ".join(ch["name"] for ch in history[0])
        ghost = f"직전: {recent_names} (엔터 시 실행)"
    else:
        ghost = "예: 침착맨, 주호민 또는 채널 ID / 치지직 주소"

    if pt:
        try:
            buf = pt["Buffer"]()
            kb = pt["KeyBindings"]()

            @kb.add("enter")
            def _(event):
                event.app.exit(result=buf.text)

            @kb.add("c-c")
            def _(event):
                event.app.exit(exception=KeyboardInterrupt)

            @kb.add("c-d")
            def _(event):
                event.app.exit(exception=EOFError)

            input_ctrl = pt["BufferControl"](
                buffer=buf,
                input_processors=[
                    pt["BeforeInput"]("👉 입력: "),
                    pt["ConditionalProcessor"](
                        pt["AfterInput"](pt["FormattedText"]([("fg:#71717a", ghost)])),
                        pt["Condition"](lambda: not buf.text),
                    ),
                ],
            )
            input_win = pt["Window"](content=input_ctrl, wrap_lines=True, dont_extend_height=True)

            containers = [input_win]
            if history:
                hist_ctrl = pt["FormattedTextControl"](
                    text=lambda: build_history_fragments(pt, history, buf),
                    focusable=False,
                )
                containers.append(pt["Window"](content=hist_ctrl, dont_extend_height=True))

            app = pt["Application"](
                layout=pt["Layout"](pt["HSplit"](containers), focused_element=input_win),
                key_bindings=kb,
                mouse_support=True,
                full_screen=False,
            )
            return (app.run() or "").strip()
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception:
            pass

    if history:
        print_history(history)
    return input("👉 입력: ").strip()

def find_in_history_by_name(name, history):
    """입력한 이름이 최근 기록에 정확히 있으면 캐시된 채널을 즉시 반환합니다."""
    norm = "".join(name.split()).lower()
    for entry in history:
        for ch in entry:
            if "".join(ch["name"].split()).lower() == norm:
                return ch
    return None

def resolve_cli_inputs(raw_input, history):
    """콤마로 구분된 번호(1, 12, 1-2) 및 이름/ID/URL 입력을 채널 목록으로 변환합니다."""
    tokens = [tok.strip() for tok in raw_input.split(",") if tok.strip()]
    resolved = []
    seen_ids = set()

    def append_unique(ch):
        if len(resolved) >= 6:
            return
        if ch["id"] not in seen_ids:
            seen_ids.add(ch["id"])
            resolved.append({"id": ch["id"], "name": ch["name"]})

    for tok in tokens:
        if len(resolved) >= 6:
            break

        if history and len(tok) == 1 and tok in "123456789":
            c_idx = int(tok) - 1
            if 0 <= c_idx < len(history):
                for ch in history[c_idx]:
                    append_unique(ch)
                names = ", ".join(ch["name"] for ch in history[c_idx])
                print(f" ⚡ [{tok}] 불러오기 -> {names}")
                continue
            print(f" ❌ [{tok}]번 기록이 없습니다.")
            return None

        is_pair = len(tok) == 2 and tok.isdigit() and tok[0] != "0" and tok[1] != "0"
        is_dash_pair = len(tok) == 3 and tok[1] == "-" and tok[0] in "123456789" and tok[2] in "123456789"
        if history and (is_pair or is_dash_pair):
            c_idx = int(tok[0]) - 1
            s_idx = int(tok[-1]) - 1
            if 0 <= c_idx < len(history) and 0 <= s_idx < len(history[c_idx]):
                ch = history[c_idx][s_idx]
                append_unique(ch)
                print(f" ⚡ [{tok}] 불러오기 -> {ch['name']}")
                continue
            print(f" ❌ [{tok}]에 해당하는 기록이 없습니다.")
            return None

        cached = find_in_history_by_name(tok, history) if history else None
        if cached:
            if cached["id"] in seen_ids:
                print(f" ⚡ '{cached['name']}' -> [중복 제외]")
            else:
                print(f" ⚡ '{cached['name']}' -> [기록에서 즉시 불러옴]")
                append_unique(cached)
            continue

        print(f" 🔍 '{tok}' 조회 중...", end="", flush=True)
        cid, official_name = find_channel_id(tok)
        if cid:
            if cid in seen_ids:
                print(f" -> [중복 제외: {official_name}]")
            else:
                print(f" -> [성공: {official_name}]")
                append_unique({"id": cid, "name": official_name})
        else:
            print(" -> ❌ 찾지 못함")

    return resolved

def parse_version_tuple(tag):
    """버전 태그('v16', 'v16.1')를 비교 가능한 숫자 튜플로 변환합니다."""
    nums = re.findall(r"\d+", str(tag or ""))
    return tuple(int(n) for n in nums) if nums else (0,)

def github_request(url, timeout=5, as_json=True):
    """GitHub HTTP 요청을 보내고 응답(JSON 또는 텍스트)을 반환합니다."""
    req = urllib.request.Request(url, headers=GITHUB_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read().decode("utf-8")
        return json.loads(raw) if as_json else raw

def check_latest_version(timeout=2.0):
    """현재 버전보다 새로운 GitHub 릴리스가 있으면 태그 문자열을 반환합니다."""
    try:
        req = urllib.request.Request(GITHUB_LATEST_URL, headers=GITHUB_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as res:
            final_url = res.geturl()
        if "/releases/tag/" in final_url:
            latest_tag = final_url.rsplit("/releases/tag/", 1)[-1].split("?")[0].strip()
            if latest_tag and parse_version_tuple(latest_tag) > parse_version_tuple(APP_VERSION):
                return latest_tag
            return None
    except Exception:
        pass

    try:
        data = github_request(f"{GITHUB_VERSIONS_JSON}?t={int(time.time())}", timeout=timeout, as_json=True)
        latest_tag = str(data.get("latest") or "").strip()
        if latest_tag and parse_version_tuple(latest_tag) > parse_version_tuple(APP_VERSION):
            return latest_tag
    except Exception:
        pass
    return None

def fetch_available_versions():
    """GitHub Releases API(실패 시 versions.json)에서 선택 가능한 버전 목록을 가져옵니다."""
    versions = []
    try:
        releases = github_request(GITHUB_RELEASES_API, timeout=6, as_json=True)
        if isinstance(releases, list):
            for rel in releases:
                if not isinstance(rel, dict) or rel.get("draft"):
                    continue
                tag = str(rel.get("tag_name") or "").strip()
                if not tag:
                    continue
                asset_url = None
                for asset in rel.get("assets") or []:
                    name = str(asset.get("name") or "")
                    if name == GITHUB_ASSET_NAME:
                        asset_url = asset.get("browser_download_url")
                        break
                    if not asset_url and name.endswith(".py"):
                        asset_url = asset.get("browser_download_url")
                if not asset_url:
                    asset_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{tag}/{GITHUB_ASSET_NAME}"

                note_src = (rel.get("name") or rel.get("body") or "").strip().splitlines()
                note = note_src[0].strip() if note_src else ""
                date_str = str(rel.get("published_at") or rel.get("created_at") or "")[:10]
                versions.append({"tag": tag, "date": date_str, "note": note, "url": asset_url})
    except Exception:
        versions = []

    if versions:
        return versions

    try:
        data = github_request(f"{GITHUB_VERSIONS_JSON}?t={int(time.time())}", timeout=6, as_json=True)
        for item in data.get("versions") or []:
            if isinstance(item, dict) and item.get("tag") and item.get("url"):
                versions.append({
                    "tag": str(item["tag"]),
                    "date": str(item.get("date") or ""),
                    "note": str(item.get("note") or ""),
                    "url": str(item["url"]),
                })
    except Exception:
        pass
    return versions

def switch_to_version(entry, is_manual_switch=False):
    """선택한 버전의 스크립트를 내려받아 검증·백업 후 교체하고 재실행합니다."""
    tag = entry["tag"]
    script_path = os.path.abspath(__file__)
    print(f"\n ⬇ {tag} 버전을 다운로드하는 중...")

    try:
        code_text = github_request(entry["url"], timeout=15, as_json=False)
    except Exception as e:
        print(f" ❌ 다운로드 실패: {e}\n")
        return False

    if len(code_text) < 5000:
        print(" ❌ 내려받은 파일이 정상적인 스크립트가 아닙니다.\n")
        return False

    try:
        compile(code_text, script_path, "exec")
    except SyntaxError as e:
        print(f" ❌ 내려받은 코드 문법 검증 실패: {e}\n")
        return False

    backup_path = script_path + ".bak"
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            old_text = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(old_text)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code_text)
    except Exception as e:
        print(f" ❌ 파일 저장 실패: {e}\n")
        return False

    print(f" ✅ {tag} 버전으로 변경 완료! (이전 파일 백업: {os.path.basename(backup_path)})")
    print(" 🔄 새 버전으로 다시 실행합니다...\n")

    next_args = [arg for arg in sys.argv[1:] if arg != "--skip-auto-update"]
    if is_manual_switch:
        next_args.append("--skip-auto-update")

    if os.name == "nt":
        rc = subprocess.call([sys.executable, script_path] + next_args)
        sys.exit(rc)
    else:
        os.execv(sys.executable, [sys.executable, script_path] + next_args)

def auto_update_if_needed():
    """시작 시 더 새로운 GitHub 릴리스가 있으면 묻지 않고 자동 업데이트합니다."""
    if "--skip-auto-update" in sys.argv[1:]:
        return False

    newer_tag = check_latest_version(timeout=2.0)
    if not newer_tag:
        return False

    print(f" ✨ 새 버전({newer_tag})을 발견했습니다! 자동으로 업데이트합니다...")
    direct_url = f"https://github.com/{GITHUB_REPO}/releases/download/{newer_tag}/{GITHUB_ASSET_NAME}"
    if switch_to_version({"tag": newer_tag, "url": direct_url}, is_manual_switch=False):
        return True

    versions = fetch_available_versions()
    target_entry = next((v for v in versions if v["tag"] == newer_tag), None)
    if not target_entry and versions:
        cand = versions[0]
        if parse_version_tuple(cand["tag"]) > parse_version_tuple(APP_VERSION):
            target_entry = cand
    if target_entry and target_entry["url"] != direct_url:
        if switch_to_version(target_entry, is_manual_switch=False):
            return True

    print(" ⚠ 자동 업데이트에 실패해 현재 버전으로 계속 실행합니다.")
    print("--------------------------------------------------")
    return False

def run_version_selector():
    """GitHub 버전 목록을 보여주고 사용자가 고른 버전으로 전환합니다."""
    print("\n 🔍 GitHub에서 버전 목록을 조회하는 중...")
    versions = fetch_available_versions()
    if not versions:
        print(" ❌ 버전 목록을 가져오지 못했습니다. (네트워크 또는 GitHub 저장소 확인)\n")
        return

    print("--------------------------------------------------")
    print(f" 📦 GitHub 버전 목록 (현재 실행 중: {APP_VERSION})")
    for idx, v in enumerate(versions, 1):
        cur_mark = " (현재)" if v["tag"] == APP_VERSION else ""
        date_part = f" - {v['date']}" if v["date"] else ""
        note_part = f" | {v['note']}" if v["note"] else ""
        print(f"   [{idx}] {v['tag']}{cur_mark}{date_part}{note_part}")
    print("--------------------------------------------------")

    choice = input("👉 변경할 버전 번호 (엔터 = 취소): ").strip()
    if not choice:
        print()
        return
    if not choice.isdigit():
        print("\n ❌ 숫자를 입력해 주세요.\n")
        return

    idx = int(choice) - 1
    if not (0 <= idx < len(versions)):
        print(f"\n ❌ 1 ~ {len(versions)} 사이의 번호를 입력하세요.\n")
        return

    chosen = versions[idx]
    if chosen["tag"] == APP_VERSION:
        ans = input(f" 이미 {APP_VERSION} 버전입니다. 다시 내려받아 덮어쓸까요? (y/N): ").strip().lower()
        if ans != "y":
            print()
            return

    switch_to_version(chosen, is_manual_switch=True)

def run_cookie_manager():
    """터미널에서 네이버 로그인 쿠키(NID_AUT, NID_SES)를 설정하거나 삭제합니다."""
    status = get_cookie_status()
    print("\n--------------------------------------------------")
    print(" 🔑 네이버 로그인 쿠키 설정 (성인 인증 방송용)")
    print(f"   현재 상태: {'설정됨 (' + status['nidAutPreview'] + ')' if status['hasCookie'] else '미설정'}")
    print("   확인 방법: chzzk.naver.com → F12 → Application → Cookies")
    print("   [1] 쿠키 입력/변경   [2] 저장된 쿠키 삭제   [엔터] 돌아가기")
    print("--------------------------------------------------")

    choice = input("👉 선택: ").strip()
    if choice == "1":
        nid_aut = input(" 👉 NID_AUT 입력: ").strip()
        nid_ses = input(" 👉 NID_SES 입력: ").strip()
        if nid_aut and nid_ses:
            save_cookies(nid_aut, nid_ses)
            print(" ✅ 쿠키를 로컬 파일(chzzk_cookies.json)에 저장했습니다.\n")
        else:
            print(" ❌ NID_AUT와 NID_SES를 모두 입력해야 저장됩니다.\n")
    elif choice == "2":
        clear_cookies()
        print(" 🗑 저장된 쿠키를 삭제했습니다.\n")
    else:
        print()

def main():
    global INITIAL_CHANNELS
    print("==================================================")
    print(f" 🎬 치지직 실시간 채팅 멀티뷰 ({APP_VERSION} · 최대 6개)")
    print("==================================================")

    auto_update_if_needed()

    pt = load_prompt_toolkit()

    while True:
        history = load_history()
        cookie_st = get_cookie_status()

        print(" 보고 싶은 스트리머를 입력하세요 (콤마 , 로 여러 명 입력)")
        print("   • 이름 / 채널 ID / 치지직 주소 모두 가능")
        if history:
            print("   • 엔터만 누르면 직전 기록 [1] 바로 실행")
            print()
            print(" [빠른 입력]  1 = 1번 전체   12 = 1번의 2번째 스트리머")
            print("              d1 = 1번 삭제  n = 빈 화면  c = 쿠키  v = 버전")
        else:
            print("   • 엔터만 누르면 빈 화면으로 시작해 웹에서 검색 가능")
            print("   • c = 쿠키 설정   v = GitHub 버전 선택/업데이트")
        if cookie_st["hasCookie"]:
            print(f"   • 🔑 쿠키 설정됨 ({cookie_st['nidAutPreview']})")
        print("--------------------------------------------------")

        try:
            user_input = read_cli_input(pt, history)
        except (KeyboardInterrupt, EOFError):
            print("\n 종료합니다.")
            return

        if user_input.lower() == "v":
            run_version_selector()
            continue

        if user_input.lower() == "c":
            run_cookie_manager()
            continue

        if history and user_input.lower().startswith("d") and user_input[1:].strip().isdigit():
            del_idx = int(user_input[1:].strip()) - 1
            if 0 <= del_idx < len(history):
                removed = history.pop(del_idx)
                save_history(history)
                removed_names = ", ".join(ch["name"] for ch in removed)
                print(f"\n 🗑 [{del_idx + 1}] 기록을 삭제했습니다: {removed_names}\n")
            else:
                print(f"\n ❌ 1 ~ {len(history)} 사이의 번호만 삭제할 수 있습니다.\n")
            continue

        if user_input == "":
            if history:
                INITIAL_CHANNELS = list(history[0])
                names = ", ".join(ch["name"] for ch in INITIAL_CHANNELS)
                print(f"\n ⚡ 최근 기록 [1] 실행: {names}")
            else:
                print("\n 🌐 빈 화면으로 브라우저를 실행합니다...")
            break

        if history and user_input.lower() in ("n", "0"):
            INITIAL_CHANNELS = []
            print("\n 🌐 빈 화면으로 브라우저를 실행합니다...")
            break

        print()
        resolved = resolve_cli_inputs(user_input, history)
        if resolved is None:
            print()
            continue

        if resolved:
            INITIAL_CHANNELS = resolved
            record_history_entry(INITIAL_CHANNELS)
        break

    server = ThreadingHTTPServer(("127.0.0.1", 0), MultiChatHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    print("\n--------------------------------------------------")
    print(f" 🚀 로컬 채팅 서버 실행 완료: {url}")
    print(" 🌐 브라우저가 자동으로 열립니다. 시청 중에는 이 창을 끄지 마세요!")
    print("--------------------------------------------------")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()

if __name__ == "__main__":
    main()