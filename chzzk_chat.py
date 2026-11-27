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

APP_VERSION = "v51"
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
LIVE_PLAYBACK_URL = "https://api.chzzk.naver.com/service/v1/channels/{}/live-playback-json"
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
UPDATE_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chzzk_update.json")
MAX_HISTORY = 9
history_lock = threading.Lock()
cookie_lock = threading.Lock()
update_config_lock = threading.Lock()
INITIAL_CHANNELS = []

def load_update_config():
    """자동 업데이트 활성화 여부와 고정 버전 정보를 불러옵니다."""
    with update_config_lock:
        if not os.path.exists(UPDATE_CONFIG_FILE):
            return {"auto_update": True, "pinned_version": ""}
        try:
            with open(UPDATE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"auto_update": True, "pinned_version": ""}
            return {
                "auto_update": bool(data.get("auto_update", True)),
                "pinned_version": str(data.get("pinned_version") or "").strip(),
            }
        except Exception:
            return {"auto_update": True, "pinned_version": ""}

def save_update_config(auto_update, pinned_version=""):
    """자동 업데이트 설정을 로컬 JSON 파일에 저장합니다."""
    with update_config_lock:
        try:
            with open(UPDATE_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "auto_update": bool(auto_update),
                        "pinned_version": str(pinned_version or "").strip(),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass

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
    """채널 목록에서 중복과 잘못된 항목을 정리해 반환합니다."""
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
    return cleaned

def normalize_history_entry(item):
    """구형(리스트) 및 신형(딕셔너리) 기록 항목을 공통 형식으로 정규화합니다."""
    if isinstance(item, list):
        cleaned = sanitize_channels(item)
        return {"channels": cleaned, "pinned": False} if cleaned else None
    if isinstance(item, dict):
        cleaned = sanitize_channels(item.get("channels"))
        if not cleaned:
            return None
        return {"channels": cleaned, "pinned": bool(item.get("pinned", False))}
    return None

def sort_and_trim_history(entries):
    """고정(📌) 항목은 상단에 보존하고 일반 기록만 최대 개수 내로 유지합니다."""
    pinned = []
    unpinned = []
    seen_keys = set()
    for raw in entries:
        entry = normalize_history_entry(raw)
        if not entry:
            continue
        key = tuple(ch["id"] for ch in entry["channels"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if entry["pinned"]:
            pinned.append(entry)
        else:
            unpinned.append(entry)
    pinned = pinned[:MAX_HISTORY]
    avail_unpinned = max(0, MAX_HISTORY - len(pinned))
    return pinned + unpinned[:avail_unpinned]

def load_history():
    """저장된 스크립트 최근 스트리머 조합 목록을 불러옵니다."""
    with history_lock:
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            return sort_and_trim_history(data)
        except Exception:
            return []

def save_history(history):
    """기록 목록을 JSON 파일에 저장합니다."""
    normalized = sort_and_trim_history(history or [])
    with history_lock:
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(normalized, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return normalized

def record_history_entry(channels):
    """CLI 스크립트에서 입력한 채널 조합을 최근 기록에 저장합니다. (고정 상태 보존)"""
    cleaned = sanitize_channels(channels)
    if not cleaned:
        return load_history()
    key = tuple(ch["id"] for ch in cleaned)
    history = load_history()
    was_pinned = False
    new_entries = []
    for entry in history:
        if tuple(ch["id"] for ch in entry["channels"]) == key:
            was_pinned = entry["pinned"]
            continue
        new_entries.append(entry)
    new_entry = {"channels": cleaned, "pinned": was_pinned}
    if was_pinned:
        # 고정된 항목은 상단 고정 영역의 첫 자리에 갱신해 유지
        return save_history([new_entry] + new_entries)
    pinned_part = [e for e in new_entries if e["pinned"]]
    unpinned_part = [e for e in new_entries if not e["pinned"]]
    return save_history(pinned_part + [new_entry] + unpinned_part)

def toggle_history_pin(index):
    """지정 인덱스의 기록 고정(📌) 여부를 전환하고 갱신된 목록을 반환합니다."""
    history = load_history()
    if 0 <= index < len(history):
        history[index]["pinned"] = not history[index]["pinned"]
        return save_history(history)
    return history

def request_json(url, use_cookies=False):
    headers = dict(HEADERS)
    if use_cookies:
        headers["Origin"] = "https://chzzk.naver.com"
        headers["Referer"] = "https://chzzk.naver.com/"
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
        time_machine = bool((playback.get("live") or {}).get("timeMachine"))

        try:
            pb_data = request_json(LIVE_PLAYBACK_URL.format(channel_id), use_cookies=True)
            pb_content = pb_data.get("content") or {}
            tm_raw = pb_content.get("playbackJson")
            tm_parsed = json.loads(tm_raw) if isinstance(tm_raw, str) else tm_raw
            if isinstance(tm_parsed, dict) and (tm_parsed.get("media") or []):
                playback = tm_parsed
                time_machine = bool(pb_content.get("tmp")) or bool((tm_parsed.get("live") or {}).get("timeMachine"))
        except Exception:
            pass

        media_list = (playback or {}).get("media") or []
        auth_type = ((playback or {}).get("meta") or {}).get("playbackAuthType") or ""

        def extract_seg_auth(media_obj, url_str):
            cp = (media_obj or {}).get("contentProtection") or {}
            if not cp and auth_type != "SMP_ADVANCED_SEGMENT":
                return None, None
            t_name = str(cp.get("suTokenName") or "").strip() or "__bgda__"
            q_dict = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url_str).query))
            t_val = q_dict.get("hdnts") or ""
            return (t_name, t_val) if t_val else (None, None)

        def is_audio_only_media(media_obj):
            if not isinstance(media_obj, dict):
                return False
            mid = str(media_obj.get("mediaId") or "").upper()
            if media_obj.get("audioOnly") is True or "AUDIO" in mid:
                return True
            tracks = media_obj.get("encodingTrack") or []
            dict_tracks = [t for t in tracks if isinstance(t, dict)]
            return bool(dict_tracks) and all(t.get("audioOnly") is True for t in dict_tracks)

        llhls_url = None
        llhls_media = None
        hls_url = None
        hls_media = None
        audio_url = None
        audio_media = None
        for m in media_list:
            mid = m.get("mediaId")
            path = m.get("path")
            if not path:
                continue
            if is_audio_only_media(m):
                if not audio_url:
                    audio_url = path
                    audio_media = m
                continue
            if mid == "LLHLS" and not llhls_url:
                llhls_url = path
                llhls_media = m
            elif mid == "HLS" and not hls_url:
                hls_url = path
                hls_media = m
            elif m.get("protocol") == "HLS" and not hls_url:
                hls_url = path
                hls_media = m

        primary_url = llhls_url or hls_url
        primary_media = llhls_media if llhls_url else hls_media
        if not primary_url:
            return {"status": status, "error": "HLS 스트림 주소를 찾지 못했습니다."}

        seg_tok_name, seg_tok_val = extract_seg_auth(primary_media, primary_url)
        fallback_url = hls_url if (llhls_url and hls_url and hls_url != llhls_url) else None
        fb_tok_name, fb_tok_val = extract_seg_auth(hls_media, fallback_url) if fallback_url else (None, None)
        audio_tok_name, audio_tok_val = extract_seg_auth(audio_media, audio_url) if audio_url else (None, None)

        return {
            "status": status,
            "title": title,
            "category": category,
            "userCount": user_count,
            "hlsUrl": primary_url,
            "hlsFallbackUrl": fallback_url,
            "audioOnlyUrl": audio_url,
            "isLowLatency": bool(llhls_url),
            "timeMachine": time_machine,
            "segTokenName": seg_tok_name,
            "segTokenVal": seg_tok_val,
            "fallbackSegTokenName": fb_tok_name,
            "fallbackSegTokenVal": fb_tok_val,
            "audioOnlySegTokenName": audio_tok_name,
            "audioOnlySegTokenVal": audio_tok_val,
        }
    except Exception as e:
        return {"error": str(e), "retryable": True}

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
        body:not(.focus-mode).hscroll-mode .wrapper {
            overflow-x: auto;
            overflow-y: hidden;
        }
        body:not(.focus-mode).hscroll-mode .wrapper::-webkit-scrollbar { height: 7px; }
        body:not(.focus-mode).hscroll-mode .wrapper::-webkit-scrollbar-track { background: #111115; }
        body:not(.focus-mode).hscroll-mode .wrapper::-webkit-scrollbar-thumb { background: #3f3f4a; border-radius: 4px; }
        body:not(.focus-mode).hscroll-mode .wrapper::-webkit-scrollbar-thumb:hover { background: #00ffa3; }
        .global-header-tools .btn-mini.toggle-on {
            border-color: rgba(0, 255, 163, 0.65);
            color: #00ffa3;
            background: rgba(0, 255, 163, 0.16);
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
            height: 124px;
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
            height: 124px;
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
        .video-msg.is-retryable {
            pointer-events: auto;
            cursor: pointer;
            color: #f4f4f6;
        }
        .video-msg.is-retryable:hover {
            color: #00ffa3;
        }
        .video-msg.hidden { display: none; }
        .video-overlay {
            position: absolute;
            left: 0;
            right: 0;
            bottom: 0;
            display: flex;
            flex-direction: row;
            align-items: center;
            justify-content: space-between;
            gap: 6px;
            padding: 14px 8px 6px;
            background: linear-gradient(transparent, rgba(0, 0, 0, 0.86));
            opacity: 0;
            transition: opacity 0.15s;
        }
        .video-tm-bar {
            display: none;
            flex: 1 1 auto;
            min-width: 0;
            align-items: center;
            gap: 5px;
            margin: 0 4px;
        }
        body.focus-mode .video-panel.has-timemachine .video-tm-bar {
            display: flex;
        }
        .video-tm-slider-wrap {
            position: relative;
            flex: 1 1 auto;
            min-width: 48px;
            display: flex;
            align-items: center;
        }
        .video-tm-slider {
            width: 100%;
            height: 5px;
            margin: 0;
        }
        .video-tm-tooltip {
            position: absolute;
            bottom: calc(100% + 8px);
            left: 50%;
            transform: translateX(-50%);
            padding: 3px 7px;
            border-radius: 5px;
            background: rgba(14, 14, 20, 0.96);
            border: 1px solid rgba(0, 255, 163, 0.55);
            color: #f4f4f6;
            font-size: 11px;
            font-weight: 700;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
            pointer-events: none;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.72);
            z-index: 25;
        }
        .video-tm-tooltip.hidden { display: none; }
        .video-tm-time {
            font-size: 11px;
            color: #e4e4e7;
            min-width: 44px;
            text-align: right;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
            flex-shrink: 0;
            user-select: none;
        }
        .btn-tm-live {
            font-weight: 700;
            white-space: nowrap;
            flex-shrink: 0;
        }
        .btn-tm-live.is-live {
            color: #00ffa3;
            border-color: rgba(0, 255, 163, 0.55);
            background: rgba(0, 255, 163, 0.14);
        }
        .btn-tm-live:not(.is-live) {
            color: #facc15;
            border-color: rgba(250, 204, 21, 0.6);
            background: rgba(250, 204, 21, 0.18);
        }
        .video-panel:hover .video-overlay { opacity: 1; }
        .video-info-badge {
            position: absolute;
            top: 8px;
            left: 8px;
            display: flex;
            flex-direction: column;
            gap: 2px;
            font-size: 10px;
            color: #e4e4e7;
            background: rgba(16, 16, 22, 0.84);
            border: 1px solid #34343f;
            border-radius: 5px;
            padding: 3px 7px;
            max-width: calc(100% - 16px);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s;
            z-index: 15;
        }
        .video-badge-meta {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: #a1a1aa;
            font-weight: 600;
        }
        .video-badge-title {
            display: none;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: #f4f4f6;
            font-size: 12px;
            font-weight: 700;
        }
        body.focus-mode .video-info-badge {
            max-width: min(62%, 560px);
            padding: 5px 9px;
            border-color: rgba(255, 255, 255, 0.14);
            pointer-events: auto;
            cursor: pointer;
            transition: opacity 0.2s, border-color 0.15s, background-color 0.15s;
        }
        body.focus-mode .video-info-badge:hover {
            border-color: rgba(0, 255, 163, 0.72);
            background: rgba(20, 22, 30, 0.94);
        }
        body.focus-mode .video-badge-meta { font-size: 11px; color: #00ffa3; }
        body.focus-mode .video-badge-title.has-title { display: block; }
        body.focus-mode .video-panel:hover .video-info-badge { opacity: 1; }
        body.focus-mode.focus-split-active .video-info-badge {
            cursor: pointer;
        }
        body.focus-mode.focus-split-active .video-info-badge:active {
            cursor: grabbing;
        }
        body.focus-mode.focus-split-active .video-badge-meta[data-order]::before {
            content: "#" attr(data-order) " · ";
            color: #fde047;
            font-weight: 800;
        }
        body.focus-mode .video-panel.drag-over {
            outline: 2px solid #00ffa3;
            outline-offset: -2px;
        }
        .video-swipe-hud {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            z-index: 32;
            padding: 6px 12px;
            border-radius: 999px;
            background: rgba(12, 12, 18, 0.9);
            border: 1px solid rgba(0, 255, 163, 0.65);
            color: #f4f4f6;
            font-size: 12.5px;
            font-weight: 700;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
            pointer-events: none;
            box-shadow: 0 8px 22px rgba(0, 0, 0, 0.78);
        }
        .video-swipe-hud.hidden { display: none; }
        .video-overlay-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
        .video-vol-wrap {
            display: inline-flex;
            align-items: center;
            flex-shrink: 0;
        }
        .vol-slider {
            appearance: none;
            -webkit-appearance: none;
            height: 4px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.24);
            outline: none;
            cursor: pointer;
            accent-color: #00ffa3;
        }
        .vol-slider::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 11px;
            height: 11px;
            border-radius: 50%;
            background: #00ffa3;
            border: 1.5px solid #0b0b0e;
            cursor: pointer;
        }
        .master-vol-slider {
            width: 58px;
        }
        .vol-indicator {
            font-size: 11px;
            min-width: 30px;
            text-align: right;
            color: #d4d4d8;
            font-variant-numeric: tabular-nums;
            user-select: none;
        }
        .btn-mini.muted-active,
        .btn-video-ctrl.muted-active {
            color: #ff6b6b;
            border-color: rgba(255, 107, 107, 0.5);
        }
        .video-vol-slider {
            width: 0;
            opacity: 0;
            pointer-events: none;
            margin: 0;
            transition: width 0.16s, opacity 0.16s, margin 0.16s;
        }
        .video-vol-wrap:hover .video-vol-slider,
        .video-vol-wrap:focus-within .video-vol-slider {
            width: 58px;
            opacity: 1;
            pointer-events: auto;
            margin: 0 2px 0 4px;
        }
        body.focus-mode .video-vol-slider {
            width: 66px;
            opacity: 1;
            pointer-events: auto;
            margin: 0 2px 0 4px;
        }
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

        /* 더블클릭 전체화면 집중 모드 (영상 그리드 + 선택/합치기 채팅) */
        .focus-video-stage {
            display: none;
            position: relative;
            flex: 1 1 auto;
            min-width: 0;
            height: 100vh;
            background: #000;
            overflow: hidden;
        }
        .focus-video-grid {
            display: grid;
            width: 100%;
            height: 100%;
            gap: 2px;
            background: #0b0b0e;
        }
        .focus-video-hud {
            position: absolute;
            top: 10px;
            right: 10px;
            z-index: 40;
            display: flex;
            flex-wrap: wrap;
            justify-content: flex-end;
            gap: 5px;
            max-width: calc(100% - 24px);
            padding: 5px 7px;
            border-radius: 10px;
            background: rgba(14, 14, 20, 0.78);
            border: 1px solid rgba(255, 255, 255, 0.14);
            backdrop-filter: blur(6px);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.24s ease;
        }
        .focus-video-stage.top-hud-active .focus-video-hud {
            opacity: 1;
            pointer-events: auto;
        }
        body.focus-mode .focus-video-grid .video-panel.badge-yield-top .video-info-badge {
            top: var(--badge-yield-top, 46px);
        }
        .focus-hud-chip.fs-btn {
            padding: 0 7px;
        }
        .focus-hud-chip.fs-btn.active {
            background: rgba(192, 132, 252, 0.22);
            border-color: #c084fc;
            color: #e9d5ff;
        }
        .focus-hud-chip {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            height: 24px;
            padding: 0 9px;
            border-radius: 999px;
            border: 1px solid #35394b;
            background: rgba(24, 26, 36, 0.9);
            color: #cbd5e1;
            font-size: 11.5px;
            font-weight: 600;
            cursor: pointer;
            max-width: 132px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            transition: border-color 0.15s, background 0.15s, color 0.15s;
        }
        .focus-hud-chip:hover { border-color: #94a3b8; color: #fff; }
        .focus-hud-chip.active {
            background: rgba(0, 255, 163, 0.18);
            border-color: rgba(0, 255, 163, 0.72);
            color: #00ffa3;
        }
        .focus-hud-chip.audio-only {
            background: rgba(56, 189, 248, 0.18);
            border-color: rgba(56, 189, 248, 0.75);
            color: #7dd3fc;
        }
        .focus-hud-chip.audio-only.is-muted {
            background: rgba(148, 163, 184, 0.14);
            border-color: rgba(148, 163, 184, 0.45);
            color: #94a3b8;
        }
        .focus-audio-sink {
            position: fixed;
            left: 0;
            bottom: 0;
            width: 1px;
            height: 1px;
            opacity: 0.001;
            pointer-events: none;
            overflow: hidden;
            z-index: -1;
        }
        .focus-radio-dock {
            position: absolute;
            top: 12px;
            right: 12px;
            z-index: 38;
            display: flex;
            flex-wrap: wrap;
            justify-content: flex-end;
            gap: 7px;
            max-width: min(45%, 360px);
            pointer-events: none;
            transition: top 0.18s ease;
        }
        .focus-radio-dock:empty { display: none; }
        .focus-radio-circle {
            position: relative;
            width: 38px;
            height: 38px;
            padding: 0 2px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: rgba(14, 16, 24, 0.56);
            border: 1.5px solid var(--room-color, #38bdf8);
            color: #f4f4f6;
            font-size: 10.5px;
            font-weight: 800;
            line-height: 1.05;
            letter-spacing: -0.02em;
            backdrop-filter: blur(4px);
            opacity: 0.78;
            cursor: pointer;
            pointer-events: auto;
            user-select: none;
            box-shadow: 0 3px 10px rgba(0, 0, 0, 0.45);
            transition: opacity 0.15s ease, transform 0.15s ease, background-color 0.15s ease, border-color 0.15s ease;
        }
        .focus-radio-circle:hover {
            opacity: 1;
            background: rgba(18, 22, 32, 0.9);
            transform: scale(1.06);
        }
        .focus-radio-circle.is-muted {
            opacity: 0.45;
            border-style: dashed;
            border-color: #64748b;
            color: #94a3b8;
        }
        .focus-radio-circle.is-muted:hover {
            opacity: 0.85;
            color: #e2e8f0;
        }
        .focus-radio-circle-name {
            max-width: 100%;
            overflow: hidden;
            white-space: nowrap;
            text-overflow: clip;
        }
        .focus-radio-circle-mute {
            position: absolute;
            right: -3px;
            bottom: -3px;
            font-size: 9px;
            line-height: 1;
            background: rgba(14, 16, 24, 0.92);
            border: 1px solid #475569;
            border-radius: 999px;
            padding: 1px 2px;
        }
        .focus-hud-chip.active[draggable="true"] { cursor: grab; }
        .focus-hud-chip.drag-over {
            border-color: #fde047;
            box-shadow: 0 0 0 1px #fde047;
        }
        .focus-hud-chip.overlay-btn.active {
            background: rgba(96, 165, 250, 0.22);
            border-color: #60a5fa;
            color: #93c5fd;
        }
        .focus-hud-chip.style-btn {
            padding: 0 7px;
        }
        .focus-hud-chip.style-btn.active {
            background: rgba(250, 204, 21, 0.18);
            border-color: rgba(250, 204, 21, 0.72);
            color: #fde047;
        }
        .focus-overlay-popover {
            display: none;
            position: absolute;
            top: 48px;
            right: 10px;
            z-index: 45;
            min-width: 242px;
            padding: 9px 11px;
            border-radius: 10px;
            background: rgba(16, 18, 26, 0.96);
            border: 1px solid rgba(0, 255, 163, 0.45);
            box-shadow: 0 10px 26px rgba(0, 0, 0, 0.78);
            backdrop-filter: blur(8px);
            flex-direction: column;
            gap: 8px;
        }
        .focus-overlay-popover.open {
            display: flex;
        }
        body.focus-mode.focus-chat-overlay .focus-overlay-popover {
            right: calc(var(--focus-chat-width, clamp(250px, 18vw, 330px)) + 16px);
        }
        .focus-overlay-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
        }
        .focus-overlay-label {
            font-size: 11.5px;
            font-weight: 600;
            color: #e4e4e7;
            min-width: 64px;
        }
        .focus-overlay-row .vol-slider {
            flex: 1 1 auto;
            min-width: 90px;
        }
        .focus-overlay-val {
            font-size: 11px;
            font-variant-numeric: tabular-nums;
            color: #00ffa3;
            min-width: 38px;
            text-align: right;
        }
        body.focus-mode.focus-chat-overlay .focus-video-stage {
            position: fixed;
            inset: 0;
            width: 100vw;
            height: 100vh;
            z-index: 1;
        }
        body.focus-mode.focus-chat-overlay .chat-box.is-focused {
            position: fixed;
            top: 0;
            right: 0;
            bottom: 0;
            z-index: 50;
            background: transparent;
        }
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .chat-pane {
            background: var(--focus-overlay-chat-bg, rgba(11, 11, 16, 0.55));
            backdrop-filter: blur(var(--focus-overlay-blur, 3.8px));
        }
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .focus-chat-bar {
            background: var(--focus-overlay-bar-bg, rgba(16, 18, 26, 0.67));
        }
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .pinned-panel {
            background: var(--focus-overlay-pin-bg, rgba(14, 16, 24, 0.61));
        }
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .pinned-header {
            background: var(--focus-overlay-pinhead-bg, rgba(20, 23, 34, 0.72));
        }
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .focus-resizer {
            background-color: rgba(31, 34, 46, 0.45);
            border-color: rgba(255, 255, 255, 0.14);
        }
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .msg-row .nickname,
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .msg-row .msg-text,
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .msg-donation .donation-header,
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .msg-system,
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .pinned-item .nickname,
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .pinned-item .msg-text,
        body.focus-mode.focus-chat-overlay .chat-box.is-focused .pinned-item .pin-time {
            paint-order: stroke fill;
            -webkit-text-stroke: var(--focus-text-stroke, 1.5px rgba(0, 0, 0, 0.94));
            text-shadow: var(--focus-text-shadow, 0 1px 3.3px rgba(0, 0, 0, 0.96), 0 0 2.1px rgba(0, 0, 0, 0.85));
        }
        body.focus-mode.focus-chat-overlay .focus-video-hud,
        body.focus-mode.focus-chat-overlay .focus-radio-dock {
            right: calc(var(--focus-chat-width, clamp(250px, 18vw, 330px)) + 16px);
            max-width: calc(100% - var(--focus-chat-width, clamp(250px, 18vw, 330px)) - 28px);
        }
        body.focus-mode.focus-chat-overlay .focus-video-grid .video-panel.focus-right-col .video-overlay {
            padding-right: calc(var(--focus-chat-width, clamp(250px, 18vw, 330px)) + 16px);
        }
        body.focus-mode.focus-chat-overlay .focus-video-grid.focus-dense-right .video-panel.focus-right-col .video-overlay {
            flex-wrap: wrap;
            row-gap: 4px;
        }
        body.focus-mode.focus-chat-overlay .focus-video-grid .video-panel.focus-right-col .video-info-badge {
            max-width: calc(100% - var(--focus-chat-width, clamp(250px, 18vw, 330px)) - 24px);
        }
        body.focus-mode.focus-chat-overlay.alt-passthrough .chat-box.is-focused,
        body.focus-mode.focus-chat-overlay.alt-passthrough .chat-box.is-focused * {
            pointer-events: none !important;
        }
        body.focus-mode.focus-chat-overlay.alt-passthrough .chat-box.is-focused .chat-pane {
            opacity: 0.42;
            outline: 1px dashed rgba(0, 255, 163, 0.55);
            outline-offset: -1px;
        }
        .mode-dock-wrap {
            position: fixed;
            top: 58px;
            left: 0;
            z-index: 90;
            display: inline-flex;
            flex-direction: row;
            align-items: stretch;
            height: 76px;
            box-sizing: border-box;
            padding: 0;
            border-radius: 0 10px 10px 0;
            background: rgba(20, 20, 28, 0.95);
            border: 1px solid rgba(0, 255, 163, 0.45);
            border-left: none;
            box-shadow: 4px 6px 18px rgba(0, 0, 0, 0.68);
            transition: background-color 0.2s, border-color 0.2s, box-shadow 0.2s;
        }
        .mode-dock-wrap.is-hidden { display: none !important; }
        .mode-dock-wrap.is-collapsed {
            background: transparent;
            border: none;
            box-shadow: none;
        }
        .mode-dock-wrap:not(.is-collapsed):hover,
        .mode-dock-wrap:not(.is-collapsed):focus-within,
        .mode-dock-wrap.intro-spotlight {
            background: rgba(22, 22, 30, 0.98);
            border-color: #00ffa3;
            box-shadow: 4px 8px 24px rgba(0, 0, 0, 0.78);
        }
        .mode-dock-wrap.intro-spotlight {
            animation: modeIntroPulse 1.4s ease-out 3;
        }
        @keyframes modeIntroPulse {
            0% { box-shadow: 0 0 0 0 rgba(0, 255, 163, 0.6), 4px 8px 24px rgba(0, 0, 0, 0.78); }
            70% { box-shadow: 0 0 0 10px rgba(0, 255, 163, 0), 4px 8px 24px rgba(0, 0, 0, 0.78); }
            100% { box-shadow: 0 0 0 0 rgba(0, 255, 163, 0), 4px 8px 24px rgba(0, 0, 0, 0.78); }
        }
        .mode-dock-expanded {
            display: inline-flex;
            align-items: stretch;
            height: 100%;
        }
        .mode-dock-inner {
            display: flex;
            flex-direction: column;
            justify-content: center;
            gap: 5px;
            padding: 6px 7px;
            height: 100%;
            box-sizing: border-box;
        }
        .mode-dock-wrap.is-collapsed .mode-dock-expanded {
            display: none;
        }
        .btn-mode-expand-tab {
            display: none;
            width: 10px;
            min-width: 10px;
            height: 76px;
            box-sizing: border-box;
            border-radius: 0 8px 8px 0;
            background: rgba(22, 22, 30, 0.72);
            border: 1px solid rgba(255, 255, 255, 0.16);
            border-left: none;
            color: rgba(212, 212, 216, 0.75);
            font-size: 9px;
            opacity: 0.45;
            box-shadow: none;
        }
        .btn-mode-expand-tab:hover,
        .btn-mode-expand-tab.near-hover {
            width: 24px;
            min-width: 24px;
            opacity: 1;
            background: rgba(24, 26, 36, 0.98);
            border-color: #00ffa3;
            color: #00ffa3;
            font-size: 11px;
            box-shadow: 3px 4px 12px rgba(0, 0, 0, 0.6);
        }
        .mode-dock-wrap.is-collapsed .btn-mode-expand-tab {
            display: inline-flex;
        }
        .btn-mode-collapse-tab {
            width: 14px;
            min-width: 14px;
            height: 100%;
            font-size: 10px;
            border-radius: 0 9px 9px 0;
            background: rgba(255, 255, 255, 0.04);
            border: none;
            border-left: 1px solid rgba(255, 255, 255, 0.14);
            color: #a1a1aa;
        }
        .btn-mode-collapse-tab:hover {
            width: 18px;
            min-width: 18px;
            background: rgba(0, 255, 163, 0.18);
            color: #00ffa3;
        }
        .mode-dock-btn {
            border: 1px solid rgba(255, 255, 255, 0.12);
            background: rgba(255, 255, 255, 0.04);
            color: #cbd5e1;
            font-size: 11.5px;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 7px;
            cursor: pointer;
            white-space: nowrap;
            text-align: left;
            transition: background-color 0.14s, color 0.14s, border-color 0.14s;
        }
        .mode-dock-btn:hover {
            color: #fff;
            background: rgba(255, 255, 255, 0.1);
            border-color: rgba(255, 255, 255, 0.25);
        }
        .mode-dock-btn.active {
            background: rgba(0, 255, 163, 0.18);
            border-color: rgba(0, 255, 163, 0.7);
            color: #00ffa3;
        }
        .mode-dock-hint {
            position: absolute;
            top: calc(100% + 12px);
            left: 12px;
            width: min(276px, calc(100vw - 28px));
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
        .mode-dock-hint::before {
            content: "";
            position: absolute;
            top: -8px;
            left: 22px;
            width: 12px;
            height: 12px;
            background: rgba(16, 18, 24, 0.98);
            border-left: 2px solid #00ffa3;
            border-top: 2px solid #00ffa3;
            transform: rotate(45deg);
        }
        .mode-dock-hint-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            font-size: 14px;
            font-weight: 800;
            color: #00ffa3;
        }
        .mode-dock-hint-body {
            margin-top: 6px;
            font-size: 12.5px;
            line-height: 1.5;
            color: #f4f4f6;
        }
        .mode-dock-hint-sub {
            margin-top: 5px;
            font-size: 11.5px;
            line-height: 1.4;
            color: #9ca3af;
        }
        .mode-dock-wrap.intro-spotlight .mode-dock-hint {
            opacity: 1;
            transform: translateY(0);
            pointer-events: auto;
        }
        body.focus-mode {
            display: flex;
            flex-direction: row;
        }
        body.focus-mode .focus-video-stage {
            display: block;
        }
        body.focus-mode .wrapper {
            display: flex;
            flex: 0 0 auto;
            width: auto;
            overflow: visible;
        }
        body.focus-mode .chat-box:not(.is-focused) {
            display: none !important;
        }
        body.focus-mode .chat-box.is-focused {
            flex: 0 0 auto;
            width: auto;
            height: 100vh;
            border-right: none;
        }
        body.focus-mode .chat-box.is-focused > .header {
            display: none;
        }
        body.focus-mode .chat-box.is-focused .col-body {
            flex-direction: row;
        }
        body.focus-mode .chat-box > .col-body > .video-panel {
            display: none !important;
        }
        body.focus-mode .focus-video-grid .video-panel {
            width: 100%;
            height: 100%;
            max-height: none;
            aspect-ratio: auto;
            border-bottom: none;
            border-right: none;
        }
        /* 전체화면 아이들 상태 시 모든 영상 UI 숨김 */
        body.focus-mode .focus-video-stage:not(.hud-active) .video-overlay,
        body.focus-mode .focus-video-stage:not(.hud-active) .video-info-badge {
            opacity: 0 !important;
            pointer-events: none !important;
        }
        body.focus-mode .focus-video-stage.hud-active .focus-video-grid[data-count="1"] .video-panel .video-overlay,
        body.focus-mode .focus-video-stage.hud-active .focus-video-grid[data-count="1"] .video-panel .video-info-badge {
            opacity: 1;
        }
        body.focus-mode .focus-video-stage.hud-active .focus-video-grid:not([data-count="1"]) .video-panel:not(:hover) .video-overlay,
        body.focus-mode .focus-video-stage.hud-active .focus-video-grid:not([data-count="1"]) .video-panel:not(:hover) .video-info-badge {
            opacity: 0;
        }
        body.focus-mode .focus-video-stage.hud-active .focus-video-grid:not([data-count="1"]) .video-panel:hover .video-overlay,
        body.focus-mode .focus-video-stage.hud-active .focus-video-grid:not([data-count="1"]) .video-panel:hover .video-info-badge {
            opacity: 1;
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
        .context-menu-vol-row {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 10px 4px;
            color: #cbd5e1;
            font-size: 11px;
        }
        .context-menu-vol-slider {
            width: 96px;
            flex: 1 1 auto;
        }
        .context-menu-vol-val {
            min-width: 34px;
            text-align: right;
            font-variant-numeric: tabular-nums;
            color: #00ffa3;
            font-weight: 700;
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
        .history-row.pinned {
            border-color: rgba(0, 255, 163, 0.45);
            background: rgba(0, 255, 163, 0.05);
        }
        .btn-history-pin.pinned {
            color: #00ffa3;
            border-color: rgba(0, 255, 163, 0.6);
            background: rgba(0, 255, 163, 0.14);
        }
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
        .undo-toast {
            position: fixed;
            left: 50%;
            bottom: 20px;
            transform: translate(-50%, 14px);
            z-index: 95;
            display: inline-flex;
            align-items: center;
            gap: 10px;
            padding: 8px 14px;
            border-radius: 999px;
            background: rgba(18, 18, 26, 0.96);
            border: 1px solid rgba(0, 255, 163, 0.55);
            color: #f4f4f6;
            font-size: 12.5px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.72);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s, transform 0.2s;
        }
        .undo-toast.visible {
            opacity: 1;
            transform: translate(-50%, 0);
            pointer-events: auto;
        }
        .undo-toast-btn {
            background: rgba(0, 255, 163, 0.14);
            color: #00ffa3;
            border: 1px solid rgba(0, 255, 163, 0.5);
            border-radius: 999px;
            padding: 2px 9px;
            font-size: 11.5px;
            font-weight: 700;
            cursor: pointer;
        }
        .undo-toast-btn:hover {
            background: #00ffa3;
            color: #0b0b0e;
        }
        .video-sync-wrap {
            display: inline-flex;
            align-items: center;
            gap: 2px;
            flex-shrink: 0;
        }
        .btn-video-ctrl.btn-sync-step {
            padding: 2px 4px;
            font-size: 10.5px;
            font-variant-numeric: tabular-nums;
        }
        .video-sync-badge {
            font-size: 10px;
            color: #facc15;
            background: rgba(250, 204, 21, 0.14);
            border: 1px solid rgba(250, 204, 21, 0.45);
            border-radius: 4px;
            padding: 1px 4px;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
            cursor: pointer;
            user-select: none;
        }
        .video-sync-badge.adjusting {
            color: #00ffa3;
            border-color: rgba(0, 255, 163, 0.55);
            background: rgba(0, 255, 163, 0.14);
        }
        .video-sync-badge.hidden { display: none; }
        .focus-chat-bar-wrap {
            display: none;
            position: relative;
            flex: 0 0 32px;
            height: 32px;
            z-index: 30;
        }
        body.focus-mode .chat-box.is-focused .focus-chat-bar-wrap {
            display: block;
        }
        .focus-chat-bar {
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            align-content: flex-start;
            row-gap: 6px;
            column-gap: 4px;
            padding: 5px 8px;
            min-height: 32px;
            max-height: 32px;
            overflow: hidden;
            background: #151720;
            border-bottom: 1px solid #262938;
            transition: max-height 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease;
        }
        .focus-chat-bar-wrap.has-overflow .focus-chat-bar {
            padding-right: 22px;
        }
        .focus-chat-bar-wrap.has-overflow:hover .focus-chat-bar,
        .focus-chat-bar-wrap.has-overflow:focus-within .focus-chat-bar {
            max-height: 180px;
            padding-right: 8px;
            background: rgba(21, 23, 32, 0.98);
            border-bottom-color: rgba(0, 255, 163, 0.45);
            box-shadow: 0 8px 20px rgba(0, 0, 0, 0.72);
        }
        .focus-chat-more {
            display: none;
            position: absolute;
            top: 5px;
            right: 6px;
            height: 22px;
            line-height: 22px;
            font-size: 11px;
            color: #9ca3af;
            pointer-events: none;
            z-index: 31;
            transition: opacity 0.12s ease;
        }
        .focus-chat-bar-wrap.has-overflow .focus-chat-more {
            display: inline-block;
        }
        .focus-chat-bar-wrap.has-overflow:hover .focus-chat-more,
        .focus-chat-bar-wrap.has-overflow:focus-within .focus-chat-more {
            opacity: 0;
        }
        .focus-chat-chip {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            height: 22px;
            box-sizing: border-box;
            background: #1f222e;
            color: #cbd5e1;
            border: 1px solid #323648;
            border-radius: 999px;
            padding: 0 7px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            max-width: 114px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            transition: border-color 0.15s, background 0.15s, color 0.15s;
        }
        .focus-chat-chip:hover {
            border-color: #64748b;
            color: #fff;
        }
        .focus-chat-chip.active {
            background: rgba(0, 255, 163, 0.14);
            border-color: rgba(0, 255, 163, 0.65);
            color: #00ffa3;
        }
        .focus-chat-chip.merge-btn.active {
            background: rgba(96, 165, 250, 0.18);
            border-color: #60a5fa;
            color: #93c5fd;
        }
        .focus-chat-chip.pin-btn {
            padding: 0 6px;
            font-size: 11px;
        }
        .focus-room-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            flex-shrink: 0;
        }
        .focus-pinned-panel,
        .focus-inner-resizer,
        .focus-messages-wrap {
            display: none !important;
        }
        .chat-pane.focus-custom-active > .pinned-panel:not(.focus-pinned-panel),
        .chat-pane.focus-custom-active > .resizer:not(.focus-inner-resizer),
        .chat-pane.focus-custom-active > .messages-wrap:not(.focus-messages-wrap) {
            display: none !important;
        }
        body.focus-mode .chat-box.is-focused .chat-pane.focus-custom-active > .focus-pinned-panel {
            display: flex !important;
        }
        body.focus-mode .chat-box.is-focused .chat-pane.focus-custom-active > .focus-pinned-panel.hidden {
            display: none !important;
        }
        body.focus-mode .chat-box.is-focused .chat-pane.focus-custom-active > .focus-inner-resizer {
            display: block !important;
        }
        body.focus-mode .chat-box.is-focused .chat-pane.focus-custom-active > .focus-inner-resizer.hidden {
            display: none !important;
        }
        body.focus-mode .chat-box.is-focused .chat-pane.focus-custom-active > .focus-messages-wrap {
            display: flex !important;
        }
        .room-tag {
            display: inline-block;
            font-size: 10px;
            font-weight: 700;
            line-height: 1.25;
            padding: 1px 5px;
            margin-right: 5px;
            border-radius: 4px;
            vertical-align: middle;
            max-width: 58px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--room-color, #00ffa3);
            color: var(--room-color, #00ffa3);
        }
    </style>
</head>
<body>
    <div class="focus-video-stage" id="focus-video-stage">
        <div class="focus-video-grid" id="focus-video-grid" data-count="1"></div>
        <div class="focus-audio-sink" id="focus-audio-sink" aria-hidden="true"></div>
        <div class="focus-radio-dock" id="focus-radio-dock" aria-label="라디오 재생 중 방송"></div>
        <div class="focus-video-hud" id="focus-video-hud" aria-label="전체화면 스트림 선택"></div>
        <div class="focus-overlay-popover" id="focus-overlay-popover" aria-label="채팅 오버레이 설정">
            <div class="focus-overlay-row">
                <span class="focus-overlay-label">배경 투명도</span>
                <input type="range" class="vol-slider" id="overlay-alpha-slider" min="0" max="90" step="5" value="55">
                <span class="focus-overlay-val" id="overlay-alpha-val">55%</span>
            </div>
            <div class="focus-overlay-row">
                <span class="focus-overlay-label">글자 테두리</span>
                <input type="range" class="vol-slider" id="overlay-stroke-slider" min="0" max="4" step="0.5" value="1.5">
                <span class="focus-overlay-val" id="overlay-stroke-val">1.5px</span>
            </div>
        </div>
    </div>
    <div class="wrapper" id="wrapper"></div>
    <!-- 좌측 화면 모드 전환 탭 -->
    <div class="mode-dock-wrap is-hidden is-collapsed" id="mode-dock-wrap" aria-label="화면 모드 전환">
        <div class="mode-dock-expanded" id="mode-dock-expanded">
            <div class="mode-dock-inner">
                <button type="button" class="mode-dock-btn active" id="btn-mode-chat" title="세로 분할 멀티 채팅 기본 화면">💬 채팅 모드</button>
                <button type="button" class="mode-dock-btn" id="btn-mode-watch" title="영상 중심 시청 모드 (분할·오버레이)">📺 시청 모드</button>
            </div>
            <button type="button" class="btn-tools-handle btn-mode-collapse-tab" id="btn-mode-collapse" title="모드 탭 접기">◀</button>
        </div>
        <button type="button" class="btn-tools-handle btn-mode-expand-tab" id="btn-mode-expand" title="화면 모드 전환 (채팅 · 시청) 펼치기">▶</button>
        <div class="mode-dock-hint" id="mode-dock-hint" title="클릭하면 닫히고 탭이 접힙니다">
            <div class="mode-dock-hint-head">
                <span>🔄 채팅 · 시청 모드 전환</span>
                <button type="button" class="global-tools-hint-close" id="btn-mode-hint-close" title="닫기">✕</button>
            </div>
            <div class="mode-dock-hint-body">
                멀티 채팅 화면과 영상 중심 시청 모드를 여기서 한 번에 전환할 수 있어요.
            </div>
            <div class="mode-dock-hint-sub">
                잠시 후 왼쪽 끝 얇은 <b>[▶]</b> 버튼으로 접히며, 눌러서 언제든 다시 펼칠 수 있습니다.
            </div>
        </div>
    </div>
    <div class="undo-toast" id="undo-toast" role="status" aria-live="polite">
        <span id="undo-toast-msg"></span>
        <button type="button" class="undo-toast-btn" id="undo-toast-btn" style="display:none;">Ctrl+Z 되돌리기</button>
    </div>

    <!-- 전역 컨트롤 묶음 -->
    <div class="global-header-tools" id="global-tools">
        <button type="button" class="btn-tools-handle btn-tools-expand-tab" id="btn-tools-expand" title="설정 · 글자크기 툴바 펼치기">◀</button>
        <div class="global-tools-expanded" id="global-tools-expanded">
            <button type="button" class="btn-tools-handle btn-tools-collapse-tab" id="btn-tools-collapse" title="툴바 접기">▶</button>
            <div class="global-tools-inner">
                <div class="global-tools-section" aria-label="관리 및 인증">
                    <button class="btn-mini btn-tools-primary" id="btn-manage-streamers" title="스트리머 추가 및 편성 관리 (E)">⚙ 설정</button>
                    <button class="btn-mini btn-cookie" id="btn-cookie-settings" title="네이버 로그인 쿠키 설정 (성인 인증 방송용)">🔑</button>
                    <button class="btn-mini" id="btn-scroll-mode" title="가로 스크롤 ON/OFF (채널이 많을 때 최소 너비 유지)">↔</button>
                </div>
                <div class="global-tools-section" aria-label="글자 크기 조절">
                    <button class="btn-mini" id="btn-font-down" title="글자 작게 (-)">-</button>
                    <span class="font-indicator" id="font-size-val">20</span>
                    <button class="btn-mini" id="btn-font-up" title="글자 크게 (+)">+</button>
                </div>
                <div class="global-tools-section" aria-label="전체 볼륨 조절">
                    <button class="btn-mini" id="btn-master-mute" title="전체 방송 일괄 음소거">🔊</button>
                    <input type="range" class="vol-slider master-vol-slider" id="master-vol-slider" min="0" max="100" value="50" title="전체 마스터 볼륨 (개별 볼륨 비율 유지)">
                    <span class="vol-indicator" id="master-vol-val">50%</span>
                </div>
            </div>
        </div>
        <div class="global-tools-hint" id="global-tools-hint" title="클릭하면 닫히고 툴바가 접힙니다">
            <div class="global-tools-hint-head">
                <span>⚙ 설정 · 쿠키 · 글자 · 볼륨 (E)</span>
                <button type="button" class="global-tools-hint-close" id="btn-tools-hint-close" title="닫기">✕</button>
            </div>
            <div class="global-tools-hint-body">
                스트리머 편성, 성인 인증 쿠키, 글자 크기, 전체 마스터 볼륨을 여기서 바로 조절할 수 있어요.
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
        <button class="context-menu-item" id="hist-ctx-toggle-pin">
            <span id="hist-ctx-pin-label">📌 이 기록 고정</span>
        </button>
        <div class="context-menu-sep"></div>
        <button class="context-menu-item" id="hist-ctx-delete">
            <span>이 기록 삭제</span>
        </button>
    </div>

    <!-- 시청 모드 상단 칩 우클릭 컨텍스트 메뉴 -->
    <div class="context-menu" id="focus-hud-context-menu" style="z-index: 300;">
        <div class="context-menu-header" id="hud-ctx-title">방송 설정</div>
        <button class="context-menu-item" id="hud-ctx-video">
            <span id="hud-ctx-video-label">📺 영상 켜기</span>
            <span class="context-menu-shortcut" id="hud-ctx-video-hint">좌클릭</span>
        </button>
        <button class="context-menu-item" id="hud-ctx-radio">
            <span id="hud-ctx-radio-label">🎧 소리만 켜기 (라디오)</span>
            <span class="context-menu-shortcut" id="hud-ctx-radio-hint">오디오 전용</span>
        </button>
        <div class="context-menu-sep"></div>
        <button class="context-menu-item" id="hud-ctx-mute">
            <span id="hud-ctx-mute-label">🔊 음소거</span>
            <span class="context-menu-shortcut" id="hud-ctx-mute-hint"></span>
        </button>
        <div class="context-menu-vol-row" id="hud-ctx-vol-row">
            <span>볼륨</span>
            <input type="range" class="vol-slider context-menu-vol-slider" id="hud-ctx-vol-slider" min="0" max="100" value="50">
            <span class="context-menu-vol-val" id="hud-ctx-vol-val">50%</span>
        </div>
        <div class="context-menu-sep"></div>
        <button class="context-menu-item" id="hud-ctx-open-live">
            <span>↗ 해당 채널로 이동</span>
            <span class="context-menu-shortcut">새 탭</span>
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
                    <span>현재 표시 중인 채널</span>
                    <span id="active-count-label">0개</span>
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

        const HSCROLL_MODE_KEY = "chzzk_hscroll_mode";
        let isHScrollMode = localStorage.getItem(HSCROLL_MODE_KEY) === "1";
        const scrollModeBtn = document.getElementById("btn-scroll-mode");

        function applyHScrollMode(enabled, persist = true, relayout = true) {
            isHScrollMode = !!enabled;
            document.body.classList.toggle("hscroll-mode", isHScrollMode);
            if (scrollModeBtn) {
                scrollModeBtn.classList.toggle("toggle-on", isHScrollMode);
                scrollModeBtn.title = isHScrollMode
                    ? "가로 스크롤 켜짐 (클릭: 한 화면에 균등 분할)"
                    : "가로 스크롤 꺼짐 (클릭: 최소 너비 유지 + 가로 스크롤)";
            }
            if (persist) {
                localStorage.setItem(HSCROLL_MODE_KEY, isHScrollMode ? "1" : "0");
            }
            if (relayout && typeof syncLayout === "function") {
                syncLayout();
            }
        }
        if (scrollModeBtn) {
            scrollModeBtn.onclick = () => applyHScrollMode(!isHScrollMode, true, true);
            applyHScrollMode(isHScrollMode, false, false);
        }

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
            const pinLabel = document.getElementById("hist-ctx-pin-label");
            const entry = modalHistory[comboIdx];

            if (histCtxTargetChannel) {
                titleEl.textContent = `${histCtxTargetChannel.name} · 기록 #${comboIdx + 1}`;
                pickOneLabel.textContent = "이 스트리머 추가";
                pickOneBtn.style.display = "flex";
            } else {
                titleEl.textContent = `스크립트 기록 #${comboIdx + 1}`;
                pickOneBtn.style.display = "none";
            }
            if (pinLabel) {
                pinLabel.textContent = entry && entry.pinned ? "📌 고정 해제" : "📌 이 기록 고정";
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

        document.getElementById("hist-ctx-toggle-pin").onclick = () => {
            if (histCtxTargetComboIdx >= 0) {
                toggleHistoryPin(histCtxTargetComboIdx);
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

        const hudCtxMenuEl = document.getElementById("focus-hud-context-menu");
        let hudCtxChannelId = null;
        let hudCtxSource = "hud";

        function refreshHudContextMenu() {
            if (!hudCtxMenuEl || !hudCtxChannelId) return;
            const c = clientMap.get(hudCtxChannelId);
            if (!c || c.isDestroyed) {
                closeHudContextMenu();
                return;
            }
            const isVideoOn = focusActiveVideoIds.includes(hudCtxChannelId);
            const isAudioOn = focusAudioOnlyIds.includes(hudCtxChannelId);
            const isSelfMuted = c.isChannelMuted || c.channelVolume <= 0;

            document.getElementById("hud-ctx-title").textContent =
                `${c.channel.name} · ${isVideoOn ? "영상" : (isAudioOn ? "라디오" : "대기")}`;
            document.getElementById("hud-ctx-video-label").textContent =
                isVideoOn ? "📺 영상 끄기" : (isAudioOn ? "📺 영상으로 전환" : "📺 영상 켜기");
            document.getElementById("hud-ctx-radio-label").textContent =
                isAudioOn ? "🎧 라디오 끄기" : "🎧 소리만 켜기 (라디오)";
            document.getElementById("hud-ctx-mute-label").textContent =
                isSelfMuted ? "🔊 음소거 해제" : "🔇 이 방송 음소거";

            const videoHintEl = document.getElementById("hud-ctx-video-hint");
            const radioHintEl = document.getElementById("hud-ctx-radio-hint");
            const muteHintEl = document.getElementById("hud-ctx-mute-hint");
            if (videoHintEl) {
                videoHintEl.textContent = (hudCtxSource === "hud" && !isAudioOn) ? "좌클릭" : (isAudioOn ? "전환" : "");
            }
            if (radioHintEl) {
                radioHintEl.textContent = (hudCtxSource === "hud" && isAudioOn) ? "좌클릭" : "오디오 전용";
            }
            if (muteHintEl) {
                muteHintEl.textContent = hudCtxSource === "circle" ? "좌클릭" : "";
            }

            const slider = document.getElementById("hud-ctx-vol-slider");
            const valEl = document.getElementById("hud-ctx-vol-val");
            if (slider) slider.value = String(isSelfMuted ? 0 : c.channelVolume);
            if (valEl) valEl.textContent = isSelfMuted ? "음소거" : `${c.channelVolume}%`;
        }

        function openHudContextMenu(e, channelId, sourceType = "hud") {
            if (!hudCtxMenuEl || !focusedClient) return;
            e.preventDefault();
            e.stopPropagation();
            hudCtxChannelId = channelId;
            hudCtxSource = sourceType;
            refreshHudContextMenu();
            positionContextMenu(hudCtxMenuEl, e);
            wakeFocusHud();
            if (sourceType === "hud") wakeTopHud(3000);
        }

        function closeHudContextMenu() {
            if (hudCtxMenuEl) hudCtxMenuEl.classList.remove("open");
            hudCtxChannelId = null;
        }

        if (hudCtxMenuEl) {
            hudCtxMenuEl.addEventListener("click", (e) => e.stopPropagation());
            hudCtxMenuEl.addEventListener("mousedown", (e) => e.stopPropagation());
            document.getElementById("hud-ctx-video").onclick = () => {
                const cid = hudCtxChannelId;
                closeHudContextMenu();
                if (!cid || !focusedClient) return;
                const c = clientMap.get(cid);
                if (!c || c.isDestroyed) return;
                if (focusActiveVideoIds.includes(cid)) {
                    removeStreamFromFocus(c);
                } else {
                    addStreamToFocus(c, false);
                }
            };
            document.getElementById("hud-ctx-radio").onclick = () => {
                const cid = hudCtxChannelId;
                closeHudContextMenu();
                if (cid) toggleFocusAudioStream(cid);
            };
            document.getElementById("hud-ctx-mute").onclick = () => {
                if (!hudCtxChannelId) return;
                const c = clientMap.get(hudCtxChannelId);
                if (c && !c.isDestroyed) {
                    c.toggleVideoMute();
                    refreshHudContextMenu();
                }
            };
            document.getElementById("hud-ctx-open-live").onclick = () => {
                const cid = hudCtxChannelId;
                closeHudContextMenu();
                if (cid) {
                    window.open(`https://chzzk.naver.com/live/${cid}`, "_blank", "noopener");
                }
            };
            const hudVolSlider = document.getElementById("hud-ctx-vol-slider");
            if (hudVolSlider) {
                hudVolSlider.addEventListener("input", (e) => {
                    if (!hudCtxChannelId) return;
                    const c = clientMap.get(hudCtxChannelId);
                    if (c && !c.isDestroyed) {
                        c.setChannelVolume(e.target.value);
                        refreshHudContextMenu();
                    }
                });
            }
        }

        window.addEventListener("click", (e) => {
            if (!contextMenuEl.contains(e.target)) {
                closePinContextMenu();
            }
            if (!historyCtxMenuEl.contains(e.target)) {
                closeHistoryContextMenu();
            }
            if (hudCtxMenuEl && !hudCtxMenuEl.contains(e.target)) {
                closeHudContextMenu();
            }
        });
        window.addEventListener("contextmenu", (e) => {
            if (!e.target.closest(".btn-pin-toggle")) {
                closePinContextMenu();
            }
            if (!e.target.closest(".history-row")) {
                closeHistoryContextMenu();
            }
            if (hudCtxMenuEl && !e.target.closest(".focus-hud-chip.stream-chip, .focus-radio-circle") && !hudCtxMenuEl.contains(e.target)) {
                closeHudContextMenu();
            }
        });
        window.addEventListener("blur", () => {
            closePinContextMenu();
            closeHistoryContextMenu();
            closeHudContextMenu();
        });
        window.addEventListener("resize", () => {
            closePinContextMenu();
            closeHistoryContextMenu();
            closeHudContextMenu();
        });

        const MASTER_VOL_KEY = "chzzk_master_volume";
        const MASTER_MUTE_KEY = "chzzk_master_muted";

        function loadSavedPct(key, fallback = 100) {
            const raw = localStorage.getItem(key);
            if (raw === null) return fallback;
            const num = Number(raw);
            return Number.isFinite(num) ? Math.max(0, Math.min(100, Math.round(num))) : fallback;
        }

        let masterVolume = loadSavedPct(MASTER_VOL_KEY, 50);
        let masterLastNonZero = masterVolume > 0 ? masterVolume : 50;
        let masterMuted = localStorage.getItem(MASTER_MUTE_KEY) === "1";

        function getEffectiveVolume(channelVolPct) {
            if (masterMuted || masterVolume <= 0) return 0;
            return Math.max(0, Math.min(1, (masterVolume / 100) * (channelVolPct / 100)));
        }

        function syncMasterVolumeUI() {
            const muteBtn = document.getElementById("btn-master-mute");
            const slider = document.getElementById("master-vol-slider");
            const valEl = document.getElementById("master-vol-val");
            const isMuted = masterMuted || masterVolume <= 0;
            if (muteBtn) {
                muteBtn.textContent = isMuted ? "🔇" : "🔊";
                muteBtn.classList.toggle("muted-active", isMuted);
                muteBtn.title = isMuted ? "전체 방송 음소거 해제" : "전체 방송 일괄 음소거";
            }
            if (slider) slider.value = String(isMuted ? 0 : masterVolume);
            if (valEl) valEl.textContent = isMuted ? "음소거" : `${masterVolume}%`;
        }

        function syncAllClientAudio() {
            syncMasterVolumeUI();
            clientMap.forEach(c => c.applyAudioState());
            if (focusedClient) {
                renderFocusVideoHud();
                renderFocusRadioDock();
            }
        }

        const FOCUS_ROOM_COLORS = ["#00ffa3", "#60a5fa", "#f472b6", "#facc15", "#c084fc", "#fb923c"];
        const focusVideoStageEl = document.getElementById("focus-video-stage");
        const focusVideoGridEl = document.getElementById("focus-video-grid");
        const focusAudioSinkEl = document.getElementById("focus-audio-sink");
        const focusRadioDockEl = document.getElementById("focus-radio-dock");
        const focusVideoHudEl = document.getElementById("focus-video-hud");
        const focusOverlayPopoverEl = document.getElementById("focus-overlay-popover");
        const overlayAlphaSliderEl = document.getElementById("overlay-alpha-slider");
        const overlayAlphaValEl = document.getElementById("overlay-alpha-val");
        const overlayStrokeSliderEl = document.getElementById("overlay-stroke-slider");
        const overlayStrokeValEl = document.getElementById("overlay-stroke-val");
        let focusedClient = null;
        let focusPrevFullscreen = false;
        let focusSuspendedClients = [];
        let focusActiveVideoIds = [];
        let focusAudioOnlyIds = [];
        const FOCUS_CHAT_OVERLAY_KEY = "chzzk_focus_chat_overlay";
        const FOCUS_OVERLAY_ALPHA_KEY = "chzzk_focus_overlay_alpha";
        const FOCUS_OVERLAY_STROKE_KEY = "chzzk_focus_overlay_stroke";
        const FOCUS_PRESET_KEY = "chzzk_focus_preset";
        const focusStreamSavedStates = new Map();
        let focusHudTimer = null;
        let focusTopHudTimer = null;
        let focusChatOverlay = localStorage.getItem(FOCUS_CHAT_OVERLAY_KEY) === "1";
        let focusOverlayAlphaPct = loadSavedPct(FOCUS_OVERLAY_ALPHA_KEY, 55);
        focusOverlayAlphaPct = Math.max(0, Math.min(90, focusOverlayAlphaPct));
        const savedStrokeRaw = Number(localStorage.getItem(FOCUS_OVERLAY_STROKE_KEY));
        let focusOverlayStrokePx = Number.isFinite(savedStrokeRaw) ? Math.max(0, Math.min(4, savedStrokeRaw)) : 1.5;
        let dragSourceFocusId = null;
        let lastFocusAutoTier = null;
        let focusRestoring = false;
        let focusExiting = false;
        let focusChatMerged = false;
        let focusChatSelectedIds = [];
        let focusPendingMainQueue = [];
        let focusPendingPinQueue = [];
        let _needScrollFocusMain = false;
        let _needScrollFocusPin = false;

        function applyFocusOverlayStyle(alphaPct = focusOverlayAlphaPct, strokePx = focusOverlayStrokePx, persist = true) {
            focusOverlayAlphaPct = Math.max(0, Math.min(90, Math.round(Number(alphaPct) || 0)));
            const parsedStroke = Number(strokePx);
            focusOverlayStrokePx = Number.isFinite(parsedStroke) ? Math.max(0, Math.min(4, Math.round(parsedStroke * 2) / 2)) : 1.5;

            const a = focusOverlayAlphaPct / 100;
            const root = document.documentElement;
            root.style.setProperty("--focus-overlay-chat-bg", `rgba(11, 11, 16, ${a.toFixed(2)})`);
            root.style.setProperty("--focus-overlay-bar-bg", `rgba(16, 18, 26, ${(a <= 0 ? 0 : Math.min(0.92, a + 0.12)).toFixed(2)})`);
            root.style.setProperty("--focus-overlay-pin-bg", `rgba(14, 16, 24, ${(a <= 0 ? 0 : Math.min(0.88, a + 0.06)).toFixed(2)})`);
            root.style.setProperty("--focus-overlay-pinhead-bg", `rgba(20, 23, 34, ${(a <= 0 ? 0 : Math.min(0.94, a + 0.15)).toFixed(2)})`);
            root.style.setProperty("--focus-overlay-blur", a <= 0 ? "0px" : `${(a * 7).toFixed(1)}px`);

            if (focusOverlayStrokePx > 0) {
                const s = focusOverlayStrokePx;
                root.style.setProperty("--focus-text-stroke", `${s}px rgba(0, 0, 0, 0.94)`);
                root.style.setProperty("--focus-text-shadow", `0 1px ${(s * 2.2).toFixed(1)}px rgba(0, 0, 0, 0.96), 0 0 ${(s * 1.4).toFixed(1)}px rgba(0, 0, 0, 0.85)`);
            } else {
                root.style.setProperty("--focus-text-stroke", "0px transparent");
                root.style.setProperty("--focus-text-shadow", "none");
            }

            if (overlayAlphaSliderEl) overlayAlphaSliderEl.value = String(focusOverlayAlphaPct);
            if (overlayAlphaValEl) overlayAlphaValEl.textContent = `${focusOverlayAlphaPct}%`;
            if (overlayStrokeSliderEl) overlayStrokeSliderEl.value = String(focusOverlayStrokePx);
            if (overlayStrokeValEl) overlayStrokeValEl.textContent = focusOverlayStrokePx === 0 ? "없음" : `${focusOverlayStrokePx}px`;

            if (persist) {
                localStorage.setItem(FOCUS_OVERLAY_ALPHA_KEY, String(focusOverlayAlphaPct));
                localStorage.setItem(FOCUS_OVERLAY_STROKE_KEY, String(focusOverlayStrokePx));
            }
        }
        applyFocusOverlayStyle(focusOverlayAlphaPct, focusOverlayStrokePx, false);

        if (overlayAlphaSliderEl) {
            overlayAlphaSliderEl.oninput = (e) => {
                applyFocusOverlayStyle(Number(e.target.value), focusOverlayStrokePx, true);
                wakeFocusHud();
            };
        }
        if (overlayStrokeSliderEl) {
            overlayStrokeSliderEl.oninput = (e) => {
                applyFocusOverlayStyle(focusOverlayAlphaPct, Number(e.target.value), true);
                wakeFocusHud();
            };
        }

        function closeFocusOverlayPopover() {
            if (focusOverlayPopoverEl) focusOverlayPopoverEl.classList.remove("open");
            const styleBtn = focusVideoHudEl?.querySelector(".focus-hud-chip.style-btn");
            if (styleBtn) styleBtn.classList.remove("active");
        }

        function loadSavedFocusPreset() {
            try {
                const raw = JSON.parse(localStorage.getItem(FOCUS_PRESET_KEY) || "null");
                if (!raw || typeof raw !== "object") return null;
                const rawMuted = (raw.mutedById && typeof raw.mutedById === "object") ? raw.mutedById : {};
                const mutedById = {};
                for (const k of Object.keys(rawMuted)) {
                    if (typeof rawMuted[k] === "boolean") mutedById[k] = rawMuted[k];
                }
                return {
                    videoIds: Array.isArray(raw.videoIds) ? raw.videoIds.filter(Boolean) : [],
                    audioOnlyIds: Array.isArray(raw.audioOnlyIds) ? raw.audioOnlyIds.filter(Boolean) : [],
                    chatMerged: !!raw.chatMerged,
                    chatSelectedIds: Array.isArray(raw.chatSelectedIds) ? raw.chatSelectedIds.filter(Boolean) : [],
                    mutedById,
                };
            } catch (e) {
                return null;
            }
        }

        function saveCurrentFocusPreset() {
            if (!focusedClient || focusExiting || focusRestoring) return;
            try {
                const prevMutedMap = loadSavedFocusPreset()?.mutedById || {};
                const mutedById = { ...prevMutedMap };
                [...focusActiveVideoIds, ...focusAudioOnlyIds].forEach((cid) => {
                    const c = clientMap.get(cid);
                    if (c && !c.isDestroyed) {
                        mutedById[cid] = !!c.isChannelMuted;
                    }
                });
                const payload = {
                    videoIds: focusActiveVideoIds.slice(),
                    audioOnlyIds: focusAudioOnlyIds.slice(),
                    chatMerged: !!focusChatMerged,
                    chatSelectedIds: focusChatSelectedIds.slice(),
                    mutedById,
                };
                localStorage.setItem(FOCUS_PRESET_KEY, JSON.stringify(payload));
            } catch (e) {}
        }

        function getFocusAutoQuality(count) {
            if (count <= 1) return 0;     // 1분할: 원본
            if (count <= 4) return 720;   // 2~4분할: 720p
            return 480;                   // 5분할+(6분할): 480p
        }

        function syncTopHudClearance() {
            if (!focusedClient || !focusVideoStageEl || !focusVideoHudEl || !focusVideoGridEl) return;
            const isTopVisible = focusVideoStageEl.classList.contains("top-hud-active");
            const panels = focusVideoGridEl.querySelectorAll(".video-panel");
            if (!isTopVisible) {
                panels.forEach(p => p.classList.remove("badge-yield-top"));
                if (focusRadioDockEl) focusRadioDockEl.style.top = "";
                return;
            }
            const hudRect = focusVideoHudEl.getBoundingClientRect();
            if (focusRadioDockEl) {
                focusRadioDockEl.style.top = `${Math.max(48, Math.round(hudRect.bottom + 8))}px`;
            }
            if (focusOverlayPopoverEl) {
                focusOverlayPopoverEl.style.top = `${Math.max(46, Math.round(hudRect.bottom + 6))}px`;
            }
            panels.forEach((p) => {
                const pr = p.getBoundingClientRect();
                const badgeReachRight = Math.min(pr.right, pr.left + Math.min(pr.width * 0.62, 560));
                const overlapsTopRow = pr.top < hudRect.bottom + 8;
                const overlapsX = badgeReachRight > hudRect.left - 12 && pr.left < hudRect.right + 12;
                if (overlapsTopRow && overlapsX) {
                    const yieldPx = Math.max(44, Math.round(hudRect.bottom - pr.top + 8));
                    p.style.setProperty("--badge-yield-top", `${yieldPx}px`);
                    p.classList.add("badge-yield-top");
                } else {
                    p.classList.remove("badge-yield-top");
                }
            });
        }

        function wakeTopHud(durationMs = 2000) {
            if (!focusedClient || !focusVideoStageEl) return;
            focusVideoStageEl.classList.add("top-hud-active");
            requestAnimationFrame(syncTopHudClearance);
            if (focusTopHudTimer) clearTimeout(focusTopHudTimer);
            focusTopHudTimer = setTimeout(() => {
                focusTopHudTimer = null;
                if (!focusedClient || !focusVideoStageEl) return;
                const keepTop =
                    (hudCtxMenuEl && hudCtxMenuEl.classList.contains("open") && hudCtxSource === "hud") ||
                    (focusOverlayPopoverEl && focusOverlayPopoverEl.classList.contains("open")) ||
                    (focusVideoHudEl && focusVideoHudEl.matches(":hover"));
                if (keepTop) {
                    wakeTopHud(1200);
                    return;
                }
                focusVideoStageEl.classList.remove("top-hud-active");
                syncTopHudClearance();
            }, durationMs);
        }

        function wakeFocusHud() {
            if (!focusedClient || !focusVideoStageEl) return;
            focusVideoStageEl.classList.add("hud-active");
            if (focusHudTimer) clearTimeout(focusHudTimer);
            focusHudTimer = setTimeout(() => {
                focusHudTimer = null;
                if (!focusedClient || !focusVideoStageEl) return;
                const overControls =
                    (hudCtxMenuEl && hudCtxMenuEl.classList.contains("open")) ||
                    (focusOverlayPopoverEl && focusOverlayPopoverEl.classList.contains("open")) ||
                    (focusVideoHudEl && focusVideoHudEl.matches(":hover")) ||
                    (focusVideoGridEl && Array.from(focusVideoGridEl.querySelectorAll(".video-overlay")).some(el => el.matches(":hover")));
                if (overControls) {
                    wakeFocusHud();
                    return;
                }
                focusVideoStageEl.classList.remove("hud-active");
            }, 2200);
        }

        if (focusVideoStageEl) {
            focusVideoStageEl.addEventListener("mousemove", (e) => {
                wakeFocusHud();
                if (focusRadioDockEl && focusRadioDockEl.contains(e.target)) return;
                if (e.clientY <= 84 || (focusVideoHudEl && focusVideoHudEl.contains(e.target))) {
                    wakeTopHud();
                }
            }, { passive: true });
            ["mousedown", "touchstart"].forEach(evtName => {
                focusVideoStageEl.addEventListener(evtName, (e) => {
                    wakeFocusHud();
                    if (focusRadioDockEl && focusRadioDockEl.contains(e.target)) return;
                    if ((e.clientY && e.clientY <= 84) || (focusVideoHudEl && focusVideoHudEl.contains(e.target))) {
                        wakeTopHud();
                    }
                }, { passive: true });
            });
            focusVideoStageEl.addEventListener("mouseleave", () => {
                if ((focusOverlayPopoverEl && focusOverlayPopoverEl.classList.contains("open")) ||
                    (hudCtxMenuEl && hudCtxMenuEl.classList.contains("open"))) return;
                if (focusHudTimer) {
                    clearTimeout(focusHudTimer);
                    focusHudTimer = null;
                }
                if (focusTopHudTimer) {
                    clearTimeout(focusTopHudTimer);
                    focusTopHudTimer = null;
                }
                focusVideoStageEl.classList.remove("hud-active", "top-hud-active");
                syncTopHudClearance();
            });
            window.addEventListener("click", (e) => {
                if (!focusOverlayPopoverEl || !focusOverlayPopoverEl.classList.contains("open")) return;
                if (focusOverlayPopoverEl.contains(e.target) || e.target.closest(".focus-hud-chip.style-btn")) return;
                closeFocusOverlayPopover();
            });
        }

        function updateFocusVideoGridLayout() {
            if (!focusVideoGridEl) return;
            const n = Math.max(1, focusVideoGridEl.children.length);
            focusVideoGridEl.setAttribute("data-count", String(n));
            const cols = Math.ceil(Math.sqrt(n));
            const rows = Math.ceil(n / cols);
            focusVideoGridEl.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
            focusVideoGridEl.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
            focusVideoGridEl.classList.toggle("focus-dense-right", cols >= 3);
            return cols;
        }

        function layoutFocusVideoPanels() {
            if (!focusedClient || !focusVideoGridEl) return;
            const validIds = focusActiveVideoIds.filter(id => {
                const c = clientMap.get(id);
                return c && !c.isDestroyed;
            });
            focusActiveVideoIds = validIds;
            const n = validIds.length;
            const cols = Math.max(1, Math.ceil(Math.sqrt(Math.max(1, n))));

            document.body.classList.toggle("focus-split-active", n >= 2);
            document.body.classList.toggle("focus-chat-overlay", !!focusChatOverlay);
            if (!focusChatOverlay) {
                document.body.classList.remove("alt-passthrough");
            }

            const targetQual = getFocusAutoQuality(n);
            const tierChanged = targetQual !== lastFocusAutoTier;
            lastFocusAutoTier = targetQual;

            validIds.forEach((cid, idx) => {
                const c = clientMap.get(cid);
                const moved = c.videoPanelEl.parentElement !== focusVideoGridEl;
                focusVideoGridEl.appendChild(c.videoPanelEl);
                c.videoPanelEl.classList.remove("hidden");
                c.videoPanelEl.classList.toggle("focus-right-col", (idx % cols) === (cols - 1));

                if (c.videoBadgeEl) {
                    c.videoBadgeEl.draggable = n >= 2;
                    c.videoBadgeEl.title = n >= 2
                        ? "클릭: 치지직 방송 페이지 열기 · 드래그: 화면 순서 바꾸기"
                        : "클릭: 치지직 방송 페이지 새 탭 열기";
                }
                if (c.videoBadgeMetaEl) {
                    if (n >= 2) {
                        c.videoBadgeMetaEl.dataset.order = String(idx + 1);
                    } else {
                        delete c.videoBadgeMetaEl.dataset.order;
                    }
                }

                if (tierChanged && c.preferredQuality !== targetQual) {
                    c.preferredQuality = targetQual;
                    c.videoQualBtn.textContent = targetQual === 0 ? "원본" : `${targetQual}p`;
                    if (c.hls) {
                        c.applyPreferredVideoLevel(true);
                    }
                }

                if (moved && c.isVideoOpen) {
                    c.videoEl.play().catch(() => {});
                    requestAnimationFrame(() => {
                        if (!c.isDestroyed && c.isVideoOpen) {
                            c.videoEl.play().catch(() => {});
                        }
                    });
                }
            });

            updateFocusVideoGridLayout();
            renderFocusVideoHud();
            requestAnimationFrame(syncTopHudClearance);
            saveCurrentFocusPreset();
        }

        function swapFocusStreams(idA, idB) {
            if (!focusedClient || !idA || !idB || idA === idB) return;
            const idxA = focusActiveVideoIds.indexOf(idA);
            const idxB = focusActiveVideoIds.indexOf(idB);
            if (idxA === -1 || idxB === -1) return;
            focusActiveVideoIds[idxA] = idB;
            focusActiveVideoIds[idxB] = idA;
            layoutFocusVideoPanels();
            wakeFocusHud();
        }

        function toggleOsFullscreen() {
            if (document.fullscreenElement) {
                if (document.exitFullscreen) document.exitFullscreen().catch(() => {});
            } else if (document.documentElement.requestFullscreen) {
                document.documentElement.requestFullscreen().catch(() => {});
            }
        }

        function renderFocusVideoHud() {
            if (!focusVideoHudEl) return;
            focusVideoHudEl.innerHTML = "";
            if (!focusedClient) return;

            const fsBtn = document.createElement("button");
            fsBtn.type = "button";
            const isFsNow = !!document.fullscreenElement;
            fsBtn.className = `focus-hud-chip fs-btn${isFsNow ? " active" : ""}`;
            fsBtn.textContent = isFsNow ? "⛶ 창으로" : "⛶ 전체화면";
            fsBtn.title = "OS 전체화면 켜기/끄기";
            fsBtn.onclick = (e) => {
                e.stopPropagation();
                toggleOsFullscreen();
            };
            focusVideoHudEl.appendChild(fsBtn);

            const overlayBtn = document.createElement("button");
            overlayBtn.type = "button";
            overlayBtn.className = `focus-hud-chip overlay-btn${focusChatOverlay ? " active" : ""}`;
            overlayBtn.textContent = "◫ 채팅 오버레이";
            overlayBtn.title = focusChatOverlay
                ? "켜짐: 영상을 화면 100%로 깔고 채팅을 반투명 오버레이로 표시 (Alt 홀드: 뒤 클릭)"
                : "꺼짐: 영상과 채팅을 나란히 배치";
            overlayBtn.onclick = (e) => {
                e.stopPropagation();
                focusChatOverlay = !focusChatOverlay;
                localStorage.setItem(FOCUS_CHAT_OVERLAY_KEY, focusChatOverlay ? "1" : "0");
                if (!focusChatOverlay) closeFocusOverlayPopover();
                layoutFocusVideoPanels();
                wakeFocusHud();
            };
            focusVideoHudEl.appendChild(overlayBtn);

            if (focusChatOverlay) {
                const isPopOpen = !!(focusOverlayPopoverEl && focusOverlayPopoverEl.classList.contains("open"));
                const styleBtn = document.createElement("button");
                styleBtn.type = "button";
                styleBtn.className = `focus-hud-chip style-btn${isPopOpen ? " active" : ""}`;
                styleBtn.textContent = "🎨";
                styleBtn.title = "채팅 오버레이 배경 투명도 · 글자 테두리 설정";
                styleBtn.onclick = (e) => {
                    e.stopPropagation();
                    if (!focusOverlayPopoverEl) return;
                    const nextOpen = !focusOverlayPopoverEl.classList.contains("open");
                    focusOverlayPopoverEl.classList.toggle("open", nextOpen);
                    styleBtn.classList.toggle("active", nextOpen);
                    wakeFocusHud();
                };
                focusVideoHudEl.appendChild(styleBtn);
            }

            const isSplit = focusActiveVideoIds.length >= 2;
            activeChannels.forEach((ch) => {
                const orderIdx = focusActiveVideoIds.indexOf(ch.id);
                const isOn = orderIdx !== -1;
                const isAudioOnly = !isOn && focusAudioOnlyIds.includes(ch.id);
                const c = clientMap.get(ch.id);
                const isRadioMuted = !!(c && (c.isChannelMuted || c.channelVolume <= 0 || masterMuted || masterVolume <= 0));

                const chip = document.createElement("button");
                chip.type = "button";
                chip.className = `focus-hud-chip stream-chip${isOn ? " active" : ""}${isAudioOnly ? " audio-only" : ""}${isAudioOnly && isRadioMuted ? " is-muted" : ""}`;
                chip.draggable = isOn && isSplit;
                if (isOn) {
                    chip.title = isSplit
                        ? `'${ch.name}' 좌클릭: 영상 끄기 · 드래그: 순서 바꾸기 · 우클릭: 라디오/볼륨 메뉴`
                        : `'${ch.name}' 영상 켜짐 · 우클릭: 라디오/볼륨 메뉴`;
                } else if (isAudioOnly) {
                    chip.title = `'${ch.name}' 🎧 라디오(소리만) 재생 중 · 좌클릭: 라디오 끄기 · 우클릭: 메뉴(영상 전환/볼륨/이동)`;
                } else {
                    chip.title = `'${ch.name}' 좌클릭: 영상 추가 · 우클릭: 🎧 소리만 켜기 / 볼륨`;
                }

                const dot = document.createElement("span");
                dot.className = "focus-room-dot";
                dot.style.backgroundColor = getFocusRoomColor(ch.id);

                const label = document.createElement("span");
                if (isSplit && isOn) {
                    label.textContent = `${orderIdx + 1}·${ch.name}`;
                } else if (isAudioOnly) {
                    label.textContent = `${isRadioMuted ? "🔇" : "🎧"} ${ch.name}`;
                } else {
                    label.textContent = ch.name;
                }

                chip.appendChild(dot);
                chip.appendChild(label);
                chip.onclick = (e) => {
                    e.stopPropagation();
                    toggleFocusVideoStream(ch.id);
                };
                chip.oncontextmenu = (e) => {
                    openHudContextMenu(e, ch.id);
                };
                if (isOn && isSplit) {
                    chip.addEventListener("dragstart", (e) => {
                        dragSourceFocusId = ch.id;
                        if (e.dataTransfer) {
                            e.dataTransfer.effectAllowed = "move";
                            e.dataTransfer.setData("text/plain", ch.id);
                        }
                    });
                    chip.addEventListener("dragover", (e) => {
                        if (dragSourceFocusId && dragSourceFocusId !== ch.id) {
                            e.preventDefault();
                            chip.classList.add("drag-over");
                        }
                    });
                    chip.addEventListener("dragleave", () => {
                        chip.classList.remove("drag-over");
                    });
                    chip.addEventListener("drop", (e) => {
                        e.preventDefault();
                        chip.classList.remove("drag-over");
                        const srcId = dragSourceFocusId || e.dataTransfer?.getData("text/plain");
                        dragSourceFocusId = null;
                        if (srcId && srcId !== ch.id) {
                            swapFocusStreams(srcId, ch.id);
                        }
                    });
                    chip.addEventListener("dragend", () => {
                        dragSourceFocusId = null;
                        chip.classList.remove("drag-over");
                    });
                }
                focusVideoHudEl.appendChild(chip);
            });
            renderFocusRadioDock();
        }

        function renderFocusRadioDock() {
            if (!focusRadioDockEl) return;
            focusRadioDockEl.innerHTML = "";
            if (!focusedClient || focusAudioOnlyIds.length === 0) return;

            focusAudioOnlyIds.forEach((cid) => {
                const c = clientMap.get(cid);
                if (!c || c.isDestroyed) return;
                const isRadioMuted = !!(c.isChannelMuted || c.channelVolume <= 0 || masterMuted || masterVolume <= 0);
                const shortName = Array.from((c.channel.name || "").trim()).slice(0, 3).join("") || "?";

                const circle = document.createElement("button");
                circle.type = "button";
                circle.className = `focus-radio-circle${isRadioMuted ? " is-muted" : ""}`;
                circle.style.setProperty("--room-color", getFocusRoomColor(cid));
                circle.title = `'${c.channel.name}' 🎧 라디오 (${isRadioMuted ? "음소거" : c.channelVolume + "%"})\n좌클릭: 음소거 토글 · 우클릭: 끄기 / 채널 이동 / 볼륨`;

                const nameSpan = document.createElement("span");
                nameSpan.className = "focus-radio-circle-name";
                nameSpan.textContent = shortName;
                circle.appendChild(nameSpan);

                if (isRadioMuted) {
                    const muteTag = document.createElement("span");
                    muteTag.className = "focus-radio-circle-mute";
                    muteTag.textContent = "🔇";
                    circle.appendChild(muteTag);
                }

                circle.onclick = (e) => {
                    e.stopPropagation();
                    c.toggleVideoMute();
                };
                circle.oncontextmenu = (e) => {
                    openHudContextMenu(e, cid, "circle");
                };
                focusRadioDockEl.appendChild(circle);
            });
        }

        function saveFocusStreamState(targetClient) {
            if (!targetClient) return;
            const cid = targetClient.channel.id;
            if (!focusStreamSavedStates.has(cid)) {
                focusStreamSavedStates.set(cid, {
                    wasVideoOpen: targetClient.isVideoOpen,
                    prevQuality: targetClient.preferredQuality,
                    prevMuted: targetClient.isChannelMuted,
                });
            }
        }

        function returnVideoPanelToColumn(targetClient) {
            if (!targetClient) return;
            if (targetClient.colBodyEl && targetClient.focusResizerElNav) {
                targetClient.colBodyEl.insertBefore(targetClient.videoPanelEl, targetClient.focusResizerElNav);
            } else if (targetClient.colBodyEl) {
                targetClient.colBodyEl.prepend(targetClient.videoPanelEl);
            }
            targetClient.videoPanelEl.classList.remove("drag-over", "badge-yield-top", "focus-right-col");
            if (targetClient.videoBadgeEl) {
                targetClient.videoBadgeEl.draggable = false;
                targetClient.videoBadgeEl.title = "";
            }
            if (targetClient.videoBadgeMetaEl) delete targetClient.videoBadgeMetaEl.dataset.order;
            targetClient.videoFocusBtn.textContent = "⤢";
            targetClient.videoFocusBtn.title = "시청 모드 (더블클릭)";
        }

        function addStreamToFocus(targetClient, isInitial = false) {
            if (!targetClient || targetClient.isDestroyed || !focusVideoGridEl) return;
            const cid = targetClient.channel.id;
            saveFocusStreamState(targetClient);
            focusAudioOnlyIds = focusAudioOnlyIds.filter(id => id !== cid);
            const wasAudioOnly = !!targetClient.isAudioOnlyStream;
            targetClient.isAudioOnlyStream = false;

            if (!focusActiveVideoIds.includes(cid)) {
                focusActiveVideoIds.push(cid);
            }
            focusSuspendedClients = focusSuspendedClients.filter(c => c !== targetClient);

            const targetQual = getFocusAutoQuality(focusActiveVideoIds.length);
            targetClient.videoFocusBtn.textContent = "↩";
            targetClient.videoFocusBtn.title = "멀티뷰로 복귀 (더블클릭 / Esc)";
            targetClient.preferredQuality = targetQual;
            targetClient.videoQualBtn.textContent = targetQual === 0 ? "원본" : `${targetQual}p`;

            const savedMuted = loadSavedFocusPreset()?.mutedById?.[cid];
            if (typeof savedMuted === "boolean") {
                targetClient.setVideoMuted(savedMuted, false);
            } else if (isInitial) {
                targetClient.setVideoMuted(false, false);
            } else {
                targetClient.applyAudioState();
            }

            if (!targetClient.isVideoOpen) {
                targetClient.isVideoOpen = true;
                targetClient.toggleVideoBtn.classList.add("active");
                targetClient.startVideo();
            } else if (wasAudioOnly || (!targetClient.hls && !targetClient.videoEl.src)) {
                targetClient.startVideo();
            } else {
                targetClient.applyPreferredVideoLevel(true);
                targetClient.snapToLiveEdge();
            }

            layoutFocusVideoPanels();
            wakeFocusHud();
        }

        function removeStreamFromFocus(targetClient) {
            if (!targetClient) return;
            if (focusActiveVideoIds.length <= 1) {
                showUndoToast("최소 1개 영상은 유지됩니다. 멀티뷰 복귀는 ↩ 또는 Esc를 누르세요.", null);
                return;
            }
            const cid = targetClient.channel.id;
            focusActiveVideoIds = focusActiveVideoIds.filter(id => id !== cid);
            targetClient.isAudioOnlyStream = false;

            const saved = focusStreamSavedStates.get(cid);
            focusStreamSavedStates.delete(cid);

            returnVideoPanelToColumn(targetClient);

            if (saved) {
                targetClient.preferredQuality = saved.prevQuality;
                targetClient.videoQualBtn.textContent = saved.prevQuality === 0 ? "원본" : `${saved.prevQuality}p`;
                targetClient.setVideoMuted(saved.prevMuted, false);
                targetClient.isVideoOpen = saved.wasVideoOpen;
                targetClient.toggleVideoBtn.classList.toggle("active", saved.wasVideoOpen);
                targetClient.videoPanelEl.classList.toggle("hidden", !saved.wasVideoOpen);
                if (saved.wasVideoOpen && !focusSuspendedClients.includes(targetClient)) {
                    focusSuspendedClients.push(targetClient);
                }
            }
            targetClient.stopVideo();

            layoutFocusVideoPanels();
            wakeFocusHud();
        }

        function addAudioOnlyToFocus(targetClient, forceUnmute = true) {
            if (!targetClient || targetClient.isDestroyed || !focusAudioSinkEl) return;
            const cid = targetClient.channel.id;
            if (focusActiveVideoIds.includes(cid) && focusActiveVideoIds.length <= 1) {
                showUndoToast("최소 1개 영상은 유지됩니다. 다른 영상을 먼저 켠 뒤 소리만 전환하세요.", null);
                return;
            }

            saveFocusStreamState(targetClient);
            focusActiveVideoIds = focusActiveVideoIds.filter(id => id !== cid);
            if (!focusAudioOnlyIds.includes(cid)) {
                focusAudioOnlyIds.push(cid);
            }
            focusSuspendedClients = focusSuspendedClients.filter(c => c !== targetClient);

            const wasVideoStream = !!(targetClient.isVideoOpen && !targetClient.isAudioOnlyStream && (targetClient.hls || targetClient.videoEl.src));
            targetClient.isAudioOnlyStream = true;
            targetClient.videoPanelEl.classList.remove("hidden", "drag-over", "badge-yield-top", "focus-right-col");
            if (targetClient.videoBadgeEl) targetClient.videoBadgeEl.draggable = false;
            if (targetClient.videoBadgeMetaEl) delete targetClient.videoBadgeMetaEl.dataset.order;
            focusAudioSinkEl.appendChild(targetClient.videoPanelEl);

            const savedMuted = loadSavedFocusPreset()?.mutedById?.[cid];
            if (forceUnmute) {
                targetClient.setVideoMuted(false, false);
            } else if (typeof savedMuted === "boolean") {
                targetClient.setVideoMuted(savedMuted, false);
            } else {
                targetClient.setVideoMuted(false, false);
            }

            if (!targetClient.isVideoOpen || !targetClient.hls || wasVideoStream) {
                targetClient.isVideoOpen = true;
                targetClient.toggleVideoBtn.classList.add("active");
                targetClient.startVideo();
            } else {
                targetClient.applyPreferredVideoLevel(true);
                targetClient.videoEl.play().catch(() => {});
            }

            layoutFocusVideoPanels();
            wakeFocusHud();
        }

        function removeAudioOnlyFromFocus(targetClient) {
            if (!targetClient) return;
            const cid = targetClient.channel.id;
            focusAudioOnlyIds = focusAudioOnlyIds.filter(id => id !== cid);
            targetClient.isAudioOnlyStream = false;

            const saved = focusStreamSavedStates.get(cid);
            focusStreamSavedStates.delete(cid);

            returnVideoPanelToColumn(targetClient);
            if (saved) {
                targetClient.preferredQuality = saved.prevQuality;
                targetClient.videoQualBtn.textContent = saved.prevQuality === 0 ? "원본" : `${saved.prevQuality}p`;
                targetClient.setVideoMuted(saved.prevMuted, false);
                targetClient.isVideoOpen = saved.wasVideoOpen;
                targetClient.toggleVideoBtn.classList.toggle("active", saved.wasVideoOpen);
                targetClient.videoPanelEl.classList.toggle("hidden", !saved.wasVideoOpen);
                if (saved.wasVideoOpen && !focusSuspendedClients.includes(targetClient)) {
                    focusSuspendedClients.push(targetClient);
                }
            }
            targetClient.stopVideo();

            layoutFocusVideoPanels();
            wakeFocusHud();
        }

        function toggleFocusAudioStream(channelId) {
            if (!focusedClient) return;
            const target = clientMap.get(channelId);
            if (!target || target.isDestroyed) return;
            if (focusAudioOnlyIds.includes(channelId)) {
                removeAudioOnlyFromFocus(target);
            } else {
                addAudioOnlyToFocus(target, true);
            }
        }

        function toggleFocusVideoStream(channelId) {
            if (!focusedClient) return;
            const target = clientMap.get(channelId);
            if (!target || target.isDestroyed) return;
            if (focusActiveVideoIds.includes(channelId)) {
                removeStreamFromFocus(target);
            } else if (focusAudioOnlyIds.includes(channelId)) {
                removeAudioOnlyFromFocus(target);
            } else {
                addStreamToFocus(target, false);
            }
        }

        function getFocusRoomColor(channelId) {
            const idx = activeChannels.findIndex(c => c.id === channelId);
            return FOCUS_ROOM_COLORS[(idx >= 0 ? idx : 0) % FOCUS_ROOM_COLORS.length];
        }

        function createRoomTagEl(channelId, channelName) {
            const tag = document.createElement("span");
            tag.className = "room-tag";
            tag.style.setProperty("--room-color", getFocusRoomColor(channelId));
            tag.textContent = channelName || "";
            tag.title = `${channelName || ""} 방`;
            return tag;
        }

        function isCustomFocusChatView() {
            if (!focusedClient) return false;
            if (focusChatMerged) return true;
            return focusChatSelectedIds.length !== 1 || focusChatSelectedIds[0] !== focusedClient.channel.id;
        }

        function getFocusedChatSourceSet() {
            if (!focusedClient) return new Set();
            const valid = focusChatSelectedIds.filter(id => clientMap.has(id));
            return new Set(valid.length > 0 ? valid : [focusedClient.channel.id]);
        }

        function updateFocusChatBarOverflow() {
            if (!focusedClient || !focusedClient.focusChatBarEl || !focusedClient.focusChatBarWrapEl) return;
            const bar = focusedClient.focusChatBarEl;
            const wrap = focusedClient.focusChatBarWrapEl;
            const hasMore = bar.scrollHeight > 34;
            wrap.classList.toggle("has-overflow", hasMore);
        }

        function renderFocusChatBar() {
            if (!focusedClient || !focusedClient.focusChatBarEl) return;
            const bar = focusedClient.focusChatBarEl;
            bar.innerHTML = "";

            const pinToggleBtn = document.createElement("button");
            pinToggleBtn.type = "button";
            pinToggleBtn.className = `focus-chat-chip pin-btn${focusedClient.isPinHidden ? "" : " active"}`;
            pinToggleBtn.textContent = "📌";
            pinToggleBtn.title = "스트리머·매니저 칸 접기/펴기 (H)";
            pinToggleBtn.onclick = () => {
                if (!focusedClient) return;
                focusedClient.setPinHidden(!focusedClient.isPinHidden);
                pinToggleBtn.classList.toggle("active", !focusedClient.isPinHidden);
            };
            bar.appendChild(pinToggleBtn);

            const mergeBtn = document.createElement("button");
            mergeBtn.type = "button";
            mergeBtn.className = `focus-chat-chip merge-btn${focusChatMerged ? " active" : ""}`;
            mergeBtn.textContent = focusChatMerged ? "✔ 합치기" : "합치기";
            mergeBtn.title = focusChatMerged
                ? "통합 채팅 끄고 현재 방송 채팅만 보기"
                : "열려 있는 방 채팅을 시간순으로 합쳐 보기";
            mergeBtn.onclick = () => {
                if (!focusedClient) return;
                if (focusChatMerged) {
                    focusChatMerged = false;
                    focusChatSelectedIds = [focusedClient.channel.id];
                } else {
                    focusChatMerged = true;
                    focusChatSelectedIds = activeChannels.map(c => c.id);
                }
                rebuildFocusChatView();
            };
            bar.appendChild(mergeBtn);

            const selectedSet = getFocusedChatSourceSet();
            activeChannels.forEach((ch) => {
                const isSelected = selectedSet.has(ch.id);
                const chip = document.createElement("button");
                chip.type = "button";
                chip.className = `focus-chat-chip${isSelected ? " active" : ""}`;
                chip.title = focusChatMerged
                    ? `'${ch.name}' 방을 합치기에 포함/제외`
                    : `'${ch.name}' 방 채팅만 보기`;

                const dot = document.createElement("span");
                dot.className = "focus-room-dot";
                dot.style.backgroundColor = getFocusRoomColor(ch.id);

                const label = document.createElement("span");
                label.textContent = ch.name;

                chip.appendChild(dot);
                chip.appendChild(label);
                chip.onclick = () => {
                    if (!focusedClient) return;
                    if (focusChatMerged) {
                        const exists = focusChatSelectedIds.includes(ch.id);
                        if (exists) {
                            if (focusChatSelectedIds.length <= 1) return;
                            focusChatSelectedIds = focusChatSelectedIds.filter(id => id !== ch.id);
                        } else {
                            focusChatSelectedIds = [...focusChatSelectedIds, ch.id];
                        }
                    } else {
                        focusChatSelectedIds = [ch.id];
                    }
                    rebuildFocusChatView();
                };
                bar.appendChild(chip);
            });

            requestAnimationFrame(updateFocusChatBarOverflow);
        }

        function rebuildFocusChatView() {
            if (!focusedClient) return;
            renderFocusChatBar();

            const custom = isCustomFocusChatView();
            focusedClient.chatPaneEl.classList.toggle("focus-custom-active", custom);
            focusPendingMainQueue.length = 0;
            focusPendingPinQueue.length = 0;
            _needScrollFocusMain = false;
            _needScrollFocusPin = false;

            if (!custom) {
                focusedClient.runWithAnchorPreserved(() => {});
                saveCurrentFocusPreset();
                return;
            }

            const sources = getFocusedChatSourceSet();
            if (focusedClient.focusPinTitleEl) {
                if (focusChatMerged) {
                    focusedClient.focusPinTitleEl.textContent = "📌 통합 스트리머 · 매니저";
                } else {
                    const onlyId = Array.from(sources)[0];
                    const targetClient = clientMap.get(onlyId);
                    const roomName = targetClient ? targetClient.channel.name : "";
                    focusedClient.focusPinTitleEl.textContent = `📌 스트리머 · 매니저 (${roomName})`;
                }
            }

            // 최근 메인 채팅 시간순 백필
            const chatPool = [];
            sources.forEach((cid) => {
                const srcClient = clientMap.get(cid);
                if (srcClient && srcClient.recentChats.length > 0) {
                    for (let i = 0; i < srcClient.recentChats.length; i++) {
                        chatPool.push(srcClient.recentChats[i]);
                    }
                }
            });
            chatPool.sort((a, b) => a.ts - b.ts);
            const slicedChats = chatPool.slice(-MAX_MAIN_NODES_AUTO);

            const mainFrag = document.createDocumentFragment();
            for (let i = 0; i < slicedChats.length; i++) {
                const entry = slicedChats[i];
                const roomInfo = focusChatMerged ? { id: entry.channelId, name: entry.channelName } : null;
                mainFrag.appendChild(
                    focusedClient.buildChatNode(entry.item, entry.isDonation, entry.parsedProfile, entry.parsedExtras, roomInfo)
                );
            }
            focusedClient.focusMsgsEl.innerHTML = "";
            focusedClient.focusMsgsEl.appendChild(mainFrag);
            focusedClient.autoScrollFocusMain = true;
            focusedClient.focusScrollBtnEl.classList.remove("visible");

            // 최근 스트리머·매니저 채팅 백필
            const pinPool = [];
            sources.forEach((cid) => {
                const srcClient = clientMap.get(cid);
                if (srcClient && srcClient.recentPins.length > 0) {
                    for (let i = 0; i < srcClient.recentPins.length; i++) {
                        pinPool.push(srcClient.recentPins[i]);
                    }
                }
            });
            pinPool.sort((a, b) => a.ts - b.ts);
            const slicedPins = pinPool.slice(-MAX_PIN_NODES);

            focusedClient.focusPinListEl.innerHTML = "";
            if (slicedPins.length === 0) {
                focusedClient.focusPinListEl.innerHTML = `<div class="pinned-empty">스트리머·매니저 채팅이 여기에 표시됩니다.</div>`;
            } else {
                const pinFrag = document.createDocumentFragment();
                for (let i = 0; i < slicedPins.length; i++) {
                    const p = slicedPins[i];
                    const roomInfo = focusChatMerged ? { id: p.channelId, name: p.channelName } : null;
                    pinFrag.appendChild(focusedClient.buildPinnedNode(p, roomInfo));
                }
                focusedClient.focusPinListEl.appendChild(pinFrag);
            }
            focusedClient.autoScrollFocusPin = true;

            requestAnimationFrame(() => {
                if (focusedClient && !focusedClient.isDestroyed) {
                    focusedClient.keepBottomAnchor();
                }
            });
            saveCurrentFocusPreset();
        }

        function enterFocusMode(client) {
            if (!client || client.isDestroyed) return;
            if (focusedClient === client) return;
            if (focusedClient) exitFocusMode(true);

            focusRestoring = true;
            focusedClient = client;
            focusPrevFullscreen = !!document.fullscreenElement;
            focusSuspendedClients = [];
            lastFocusAutoTier = null;
            focusStreamSavedStates.clear();
            focusPendingMainQueue.length = 0;
            focusPendingPinQueue.length = 0;

            const preset = loadSavedFocusPreset();
            const validSavedVideos = (preset?.videoIds || []).filter(id => {
                const c = clientMap.get(id);
                return c && !c.isDestroyed;
            });
            const canRestorePreset = validSavedVideos.includes(client.channel.id) && validSavedVideos.length >= 1;
            const targetVideoIds = canRestorePreset ? validSavedVideos : [client.channel.id];
            const targetVideoSet = new Set(targetVideoIds);
            const targetAudioIds = canRestorePreset
                ? (preset?.audioOnlyIds || []).filter(id => {
                    const c = clientMap.get(id);
                    return c && !c.isDestroyed && !targetVideoSet.has(id);
                })
                : [];
            const targetAudioSet = new Set(targetAudioIds);
            focusActiveVideoIds = targetVideoIds.slice();
            focusAudioOnlyIds = targetAudioIds.slice();

            if (canRestorePreset) {
                const validChatIds = (preset.chatSelectedIds || []).filter(id => {
                    const c = clientMap.get(id);
                    return c && !c.isDestroyed;
                });
                focusChatMerged = !!preset.chatMerged && validChatIds.length > 0;
                focusChatSelectedIds = validChatIds.length > 0 ? validChatIds : [client.channel.id];
            } else {
                focusChatMerged = false;
                focusChatSelectedIds = [client.channel.id];
            }

            clientMap.forEach((other) => {
                if (!targetVideoSet.has(other.channel.id) && !targetAudioSet.has(other.channel.id) && other.isVideoOpen) {
                    focusSuspendedClients.push(other);
                    other.stopVideo();
                }
            });

            document.body.classList.add("focus-mode");
            client.container.classList.add("is-focused");

            targetVideoIds.forEach((cid) => {
                const targetClient = clientMap.get(cid);
                if (targetClient && !targetClient.isDestroyed) {
                    addStreamToFocus(targetClient, cid === client.channel.id);
                }
            });
            targetAudioIds.forEach((cid) => {
                const targetClient = clientMap.get(cid);
                if (targetClient && !targetClient.isDestroyed) {
                    addAudioOnlyToFocus(targetClient, false);
                }
            });
            rebuildFocusChatView();

            focusRestoring = false;
            saveCurrentFocusPreset();
            syncModeDockUI();
            wakeFocusHud();
            wakeTopHud(1800);
        }

        function exitFocusMode(skipFullscreenExit = false) {
            if (!focusedClient || focusExiting) return;
            saveCurrentFocusPreset();
            focusExiting = true;
            closeFocusOverlayPopover();
            closeHudContextMenu();

            const target = focusedClient;
            focusedClient = null;
            if (focusHudTimer) {
                clearTimeout(focusHudTimer);
                focusHudTimer = null;
            }
            if (focusTopHudTimer) {
                clearTimeout(focusTopHudTimer);
                focusTopHudTimer = null;
            }
            if (focusVideoStageEl) {
                focusVideoStageEl.classList.remove("hud-active", "top-hud-active");
            }
            focusChatMerged = false;
            focusChatSelectedIds = [];
            focusPendingMainQueue.length = 0;
            focusPendingPinQueue.length = 0;
            _needScrollFocusMain = false;
            _needScrollFocusPin = false;

            document.body.classList.remove("focus-mode", "focus-split-active", "focus-chat-overlay", "alt-passthrough");
            target.container.classList.remove("is-focused");
            target.chatPaneEl.classList.remove("focus-custom-active");
            lastFocusAutoTier = null;
            dragSourceFocusId = null;

            const activeIdsCopy = focusActiveVideoIds.slice();
            const audioIdsCopy = focusAudioOnlyIds.slice();
            focusActiveVideoIds = [];
            focusAudioOnlyIds = [];

            const toRestore = focusSuspendedClients.slice();
            focusSuspendedClients = [];

            for (let i = 0; i < activeIdsCopy.length; i++) {
                const c = clientMap.get(activeIdsCopy[i]);
                const saved = focusStreamSavedStates.get(activeIdsCopy[i]);
                if (!c || c.isDestroyed) continue;
                c.isAudioOnlyStream = false;
                returnVideoPanelToColumn(c);
                if (saved) {
                    c.setVideoMuted(saved.prevMuted, false);
                    c.preferredQuality = saved.prevQuality;
                    c.videoQualBtn.textContent = saved.prevQuality === 0 ? "원본" : `${saved.prevQuality}p`;
                    c.applyPreferredVideoLevel(true);
                    if (!c.isLivePinned) {
                        c.returnToLiveEdge();
                    }
                    if (!saved.wasVideoOpen) {
                        c.isVideoOpen = false;
                        c.toggleVideoBtn.classList.remove("active");
                        c.videoPanelEl.classList.add("hidden");
                        c.stopVideo();
                    } else {
                        c.videoEl.play().catch(() => {});
                        requestAnimationFrame(() => {
                            if (!c.isDestroyed && c.isVideoOpen) {
                                c.videoEl.play().catch(() => {});
                            }
                        });
                    }
                }
            }

            for (let i = 0; i < audioIdsCopy.length; i++) {
                const c = clientMap.get(audioIdsCopy[i]);
                const saved = focusStreamSavedStates.get(audioIdsCopy[i]);
                if (!c || c.isDestroyed) continue;
                const wasUsingAudioSource = !!c.isAudioOnlyStream;
                c.isAudioOnlyStream = false;
                returnVideoPanelToColumn(c);
                if (saved) {
                    c.setVideoMuted(saved.prevMuted, false);
                    c.preferredQuality = saved.prevQuality;
                    c.videoQualBtn.textContent = saved.prevQuality === 0 ? "원본" : `${saved.prevQuality}p`;
                    if (!saved.wasVideoOpen) {
                        c.isVideoOpen = false;
                        c.toggleVideoBtn.classList.remove("active");
                        c.videoPanelEl.classList.add("hidden");
                        c.stopVideo();
                    } else {
                        c.isVideoOpen = true;
                        c.toggleVideoBtn.classList.add("active");
                        c.videoPanelEl.classList.remove("hidden");
                        if (wasUsingAudioSource) {
                            c.stopVideo();
                            if (!toRestore.includes(c)) toRestore.push(c);
                        } else {
                            c.applyPreferredVideoLevel(true);
                            c.videoEl.play().catch(() => {});
                        }
                    }
                } else {
                    c.isVideoOpen = false;
                    c.toggleVideoBtn.classList.remove("active");
                    c.videoPanelEl.classList.add("hidden");
                    c.stopVideo();
                }
            }

            focusStreamSavedStates.clear();
            updateFocusVideoGridLayout();
            if (focusVideoHudEl) focusVideoHudEl.innerHTML = "";
            if (focusRadioDockEl) {
                focusRadioDockEl.innerHTML = "";
                focusRadioDockEl.style.top = "";
            }

            for (let i = 0; i < toRestore.length; i++) {
                const other = toRestore[i];
                if (!other.isDestroyed && other.isVideoOpen) {
                    if (i === 0) {
                        other.startVideo();
                    } else {
                        setTimeout(() => {
                            if (!other.isDestroyed && other.isVideoOpen && !focusedClient) {
                                other.startVideo();
                            }
                        }, i * 180);
                    }
                }
            }

            clientMap.forEach(c => c.runWithAnchorPreserved(() => {}));

            if (!skipFullscreenExit && !focusPrevFullscreen && document.fullscreenElement && document.exitFullscreen) {
                document.exitFullscreen().catch(() => {});
            }
            endFocusResize();
            focusExiting = false;
            syncModeDockUI();
        }

        const modeDockWrapEl = document.getElementById("mode-dock-wrap");
        const modeChatBtnEl = document.getElementById("btn-mode-chat");
        const modeWatchBtnEl = document.getElementById("btn-mode-watch");
        const modeExpandBtnEl = document.getElementById("btn-mode-expand");
        const modeCollapseBtnEl = document.getElementById("btn-mode-collapse");
        const modeHintEl = document.getElementById("mode-dock-hint");
        const modeHintCloseBtnEl = document.getElementById("btn-mode-hint-close");

        let modeDockIntroTimer = null;
        let modeDockPendingCollapse = false;
        let modeDockIntroShown = false;
        let modeExpandNearHover = false;

        function setModeExpandNearHover(next) {
            if (!modeExpandBtnEl || modeExpandNearHover === next) return;
            modeExpandNearHover = next;
            modeExpandBtnEl.classList.toggle("near-hover", next);
        }

        function finishModeDockIntro(shouldCollapse = true) {
            if (!modeDockWrapEl) return;
            if (modeDockIntroTimer) {
                clearTimeout(modeDockIntroTimer);
                modeDockIntroTimer = null;
            }
            modeDockPendingCollapse = false;
            modeDockWrapEl.classList.remove("intro-spotlight");
            if (shouldCollapse) {
                modeDockWrapEl.classList.add("is-collapsed");
            }
        }

        function triggerModeDockIntro() {
            if (!modeDockWrapEl || modeDockWrapEl.classList.contains("is-hidden")) return;
            modeDockIntroShown = true;
            setModeExpandNearHover(false);
            modeDockWrapEl.classList.remove("is-collapsed");
            modeDockWrapEl.classList.add("intro-spotlight");
            modeDockPendingCollapse = false;
            if (modeDockIntroTimer) clearTimeout(modeDockIntroTimer);
            modeDockIntroTimer = setTimeout(() => {
                modeDockIntroTimer = null;
                if (modeDockWrapEl.matches(":hover")) {
                    modeDockPendingCollapse = true;
                    return;
                }
                finishModeDockIntro(true);
            }, 5500);
        }

        function syncModeDockUI() {
            if (!modeDockWrapEl) return;
            const hasChannels = activeChannels.length > 0;
            modeDockWrapEl.classList.toggle("is-hidden", !hasChannels);
            const inWatch = !!focusedClient;
            if (modeChatBtnEl) modeChatBtnEl.classList.toggle("active", !inWatch);
            if (modeWatchBtnEl) modeWatchBtnEl.classList.toggle("active", inWatch);
            if (hasChannels && !modeDockIntroShown) {
                triggerModeDockIntro();
            } else if (!hasChannels) {
                setModeExpandNearHover(false);
                finishModeDockIntro(true);
            }
        }

        function enterDefaultWatchMode() {
            if (focusedClient || activeChannels.length === 0) return;
            const preset = loadSavedFocusPreset();
            let pickClient = null;
            if (preset && Array.isArray(preset.videoIds)) {
                for (let i = 0; i < preset.videoIds.length; i++) {
                    const c = clientMap.get(preset.videoIds[i]);
                    if (c && !c.isDestroyed) {
                        pickClient = c;
                        break;
                    }
                }
            }
            if (!pickClient) {
                for (let i = 0; i < activeChannels.length; i++) {
                    const c = clientMap.get(activeChannels[i].id);
                    if (c && !c.isDestroyed && c.isVideoOpen) {
                        pickClient = c;
                        break;
                    }
                }
            }
            if (!pickClient) {
                pickClient = clientMap.get(activeChannels[0].id);
            }
            if (pickClient) {
                enterFocusMode(pickClient);
            }
        }

        if (modeDockWrapEl) {
            modeDockWrapEl.addEventListener("mouseleave", () => {
                if (modeDockPendingCollapse) {
                    finishModeDockIntro(true);
                }
            });
            if (modeExpandBtnEl) {
                modeExpandBtnEl.addEventListener("click", () => {
                    modeDockPendingCollapse = false;
                    setModeExpandNearHover(false);
                    modeDockWrapEl.classList.remove("is-collapsed");
                });
            }
            if (modeCollapseBtnEl) {
                modeCollapseBtnEl.addEventListener("click", () => {
                    setModeExpandNearHover(false);
                    finishModeDockIntro(true);
                });
            }
            if (modeHintCloseBtnEl) {
                modeHintCloseBtnEl.addEventListener("click", (e) => {
                    e.stopPropagation();
                    finishModeDockIntro(true);
                });
            }
            if (modeHintEl) {
                modeHintEl.addEventListener("click", () => {
                    finishModeDockIntro(true);
                });
            }
            document.addEventListener("mousemove", (e) => {
                if (!modeExpandBtnEl || modeDockWrapEl.classList.contains("is-hidden") || !modeDockWrapEl.classList.contains("is-collapsed")) {
                    setModeExpandNearHover(false);
                    return;
                }
                if (e.clientX > 56) {
                    setModeExpandNearHover(false);
                    return;
                }
                const rect = modeExpandBtnEl.getBoundingClientRect();
                const nearX = e.clientX <= rect.left + 56;
                const nearY = e.clientY >= rect.top - 32 && e.clientY <= rect.bottom + 32;
                setModeExpandNearHover(nearX && nearY);
            }, { passive: true });
            document.addEventListener("mouseleave", () => setModeExpandNearHover(false));
            window.addEventListener("blur", () => setModeExpandNearHover(false));
            document.addEventListener("pointerdown", (e) => {
                if (!modeDockWrapEl.classList.contains("is-collapsed") && !modeDockWrapEl.contains(e.target)) {
                    setModeExpandNearHover(false);
                    finishModeDockIntro(true);
                }
            });
        }

        if (modeChatBtnEl) {
            modeChatBtnEl.onclick = () => {
                finishModeDockIntro(true);
                if (focusedClient) exitFocusMode();
            };
        }
        if (modeWatchBtnEl) {
            modeWatchBtnEl.onclick = () => {
                finishModeDockIntro(true);
                if (!focusedClient) enterDefaultWatchMode();
            };
        }

        const FOCUS_CHAT_WIDTH_KEY = "chzzk_focus_chat_width";
        let activeFocusResizer = null;

        function applyFocusChatWidth(px, persist = true) {
            if (px == null) {
                document.documentElement.style.removeProperty("--focus-chat-width");
                if (persist) localStorage.removeItem(FOCUS_CHAT_WIDTH_KEY);
                if (focusedClient) requestAnimationFrame(syncTopHudClearance);
                return;
            }
            const maxPx = Math.max(260, Math.min(560, Math.floor(window.innerWidth * 0.48)));
            const clamped = Math.max(220, Math.min(maxPx, Math.round(px)));
            document.documentElement.style.setProperty("--focus-chat-width", `${clamped}px`);
            if (persist) localStorage.setItem(FOCUS_CHAT_WIDTH_KEY, String(clamped));
            if (focusedClient) requestAnimationFrame(syncTopHudClearance);
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
            if (focusedClient) {
                exitFocusMode();
            } else {
                enterFocusMode(client);
            }
        }

        document.addEventListener("fullscreenchange", () => {
            if (focusedClient) {
                renderFocusVideoHud();
            }
        });

        window.addEventListener("keydown", (e) => {
            if (e.key === "Alt" && focusedClient && focusChatOverlay) {
                e.preventDefault();
                document.body.classList.add("alt-passthrough");
                return;
            }
            if (e.key === "Escape") {
                if (hudCtxMenuEl && hudCtxMenuEl.classList.contains("open")) {
                    closeHudContextMenu();
                    return;
                }
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
            if ((e.ctrlKey || e.metaKey) && !e.altKey) {
                const isUndo = !e.shiftKey && (e.code === "KeyZ" || e.key.toLowerCase() === "z");
                const isRedo =
                    (e.shiftKey && (e.code === "KeyZ" || e.key.toLowerCase() === "z")) ||
                    (!e.shiftKey && (e.code === "KeyY" || e.key.toLowerCase() === "y"));
                if (isUndo || isRedo) {
                    const tag = e.target.tagName;
                    const isTextField =
                        e.target.isContentEditable ||
                        tag === "TEXTAREA" ||
                        (tag === "INPUT" && !["range", "button", "checkbox", "radio"].includes(e.target.type));
                    if (!isTextField && !focusedClient) {
                        const handled = isUndo ? undoCloseChannel() : redoCloseChannel();
                        if (handled) {
                            e.preventDefault();
                        }
                    }
                    return;
                }
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

        window.addEventListener("keyup", (e) => {
            if (e.key === "Alt") {
                document.body.classList.remove("alt-passthrough");
            }
        });
        window.addEventListener("blur", () => {
            document.body.classList.remove("alt-passthrough");
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
            const needFocusScroll = flushFocusChatDomOnly();

            // Phase 2: 모든 채널의 스크롤 갱신을 한 번에 처리 (Forced Reflow 최소화)
            for (let i = 0; i < scrollTargets.length; i++) {
                scrollTargets[i].flushScrollOnly();
            }
            if (needFocusScroll) {
                flushFocusChatScrollOnly();
            }
        }

        function flushFocusChatDomOnly() {
            if (!focusedClient || focusedClient.isDestroyed || !isCustomFocusChatView()) {
                focusPendingMainQueue.length = 0;
                focusPendingPinQueue.length = 0;
                return false;
            }
            if (focusPendingMainQueue.length > 0) {
                const maxDom = focusedClient.autoScrollFocusMain ? MAX_MAIN_NODES_AUTO : MAX_MAIN_NODES_PAUSED;
                const startIdx = Math.max(0, focusPendingMainQueue.length - maxDom);
                const frag = document.createDocumentFragment();
                for (let i = startIdx; i < focusPendingMainQueue.length; i++) {
                    const entry = focusPendingMainQueue[i];
                    const roomInfo = focusChatMerged ? { id: entry.channelId, name: entry.channelName } : null;
                    frag.appendChild(
                        focusedClient.buildChatNode(entry.item, entry.isDonation, entry.parsedProfile, entry.parsedExtras, roomInfo)
                    );
                }
                focusPendingMainQueue.length = 0;
                focusedClient.focusMsgsEl.appendChild(frag);
                while (focusedClient.focusMsgsEl.children.length > maxDom) {
                    focusedClient.focusMsgsEl.removeChild(focusedClient.focusMsgsEl.firstChild);
                }
                if (focusedClient.autoScrollFocusMain) {
                    _needScrollFocusMain = true;
                }
            }
            if (focusPendingPinQueue.length > 0) {
                const emptyPlaceholder = focusedClient.focusPinListEl.querySelector(".pinned-empty");
                if (emptyPlaceholder) emptyPlaceholder.remove();

                const startIdx = Math.max(0, focusPendingPinQueue.length - MAX_PIN_NODES);
                const frag = document.createDocumentFragment();
                for (let i = startIdx; i < focusPendingPinQueue.length; i++) {
                    const entry = focusPendingPinQueue[i];
                    const roomInfo = focusChatMerged ? { id: entry.channelId, name: entry.channelName } : null;
                    frag.appendChild(focusedClient.buildPinnedNode(entry, roomInfo));
                }
                focusPendingPinQueue.length = 0;
                focusedClient.focusPinListEl.appendChild(frag);
                while (focusedClient.focusPinListEl.children.length > MAX_PIN_NODES) {
                    focusedClient.focusPinListEl.removeChild(focusedClient.focusPinListEl.firstChild);
                }
                if (!focusedClient.isPinHidden && focusedClient.autoScrollFocusPin) {
                    _needScrollFocusPin = true;
                }
            }
            return _needScrollFocusMain || _needScrollFocusPin;
        }

        function flushFocusChatScrollOnly() {
            if (!focusedClient || focusedClient.isDestroyed) return;
            if (_needScrollFocusMain && focusedClient.autoScrollFocusMain) {
                focusedClient.focusMsgsEl.scrollTop = focusedClient.focusMsgsEl.scrollHeight;
            }
            if (_needScrollFocusPin && focusedClient.autoScrollFocusPin && !focusedClient.isPinHidden) {
                focusedClient.focusPinListEl.scrollTop = focusedClient.focusPinListEl.scrollHeight;
            }
            _needScrollFocusMain = false;
            _needScrollFocusPin = false;
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

        function formatClockTime(ms) {
            const d = new Date(ms);
            const hh = String(d.getHours()).padStart(2, "0");
            const mm = String(d.getMinutes()).padStart(2, "0");
            const ss = String(d.getSeconds()).padStart(2, "0");
            return `${hh}:${mm}:${ss}`;
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
            const cdnUrls = [
                "https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js",
                "https://unpkg.com/hls.js@1/dist/hls.min.js"
            ];
            hlsLibPromise = new Promise((resolve, reject) => {
                let idx = 0;
                const tryNext = () => {
                    if (window.Hls) {
                        resolve(window.Hls);
                        return;
                    }
                    if (idx >= cdnUrls.length) {
                        hlsLibPromise = null;
                        reject(new Error("hls.js 로드 실패"));
                        return;
                    }
                    const s = document.createElement("script");
                    s.src = cdnUrls[idx++];
                    s.onload = () => {
                        if (window.Hls) {
                            resolve(window.Hls);
                        } else {
                            tryNext();
                        }
                    };
                    s.onerror = () => {
                        s.remove();
                        tryNext();
                    };
                    document.head.appendChild(s);
                };
                tryNext();
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
                this.isAudioOnlyStream = false;
                this.videoLoadSeq = 0;
                this.videoRetryTimer = null;
                this.preferredQuality = 360;
                this.isTimeMachine = false;
                this.isLivePinned = true;
                this.isScrubbingTm = false;
                this.hasManualSyncHold = false;
                this.fineSyncRemainingSec = 0;
                this.manualSyncOffsetSec = 0;
                this.fineSyncTimer = null;
                this.fineSyncLastTs = 0;
                this.channelVolume = loadSavedPct(`chzzk_channel_vol_${channel.id}`, 50);
                this.lastNonZeroVol = this.channelVolume > 0 ? this.channelVolume : 50;
                this.isChannelMuted = true;

                this.isDestroyed = false;
                this.isConnecting = false;
                this.connectSeq = 0;
                this.retryCount = 0;
                this.currentChatChannelId = null;
                this.liveStatus = "UNKNOWN";
                this.statusLineEl = null;

                this.pendingMainQueue = [];
                this.pendingPinQueue = [];
                this.recentChats = [];
                this.recentPins = [];
                this._needScrollMain = false;
                this._needScrollPin = false;

                this.autoScrollMain = true;
                this.autoScrollPin = true;
                this.autoScrollFocusMain = true;
                this.autoScrollFocusPin = true;
                this.ignoreScroll = false;
                this.suppressDblClickUntil = 0;

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
                        <div class="video-panel hidden" title="더블클릭: 전체화면 | 꾹 누르고 좌우 스와이프: 타임머신">
                            <video class="video-player" muted playsinline autoplay></video>
                            <div class="video-swipe-hud hidden"></div>
                            <div class="video-msg hidden"></div>
                            <span class="video-info-badge">
                                <span class="video-badge-meta">LIVE</span>
                                <span class="video-badge-title"></span>
                            </span>
                            <div class="video-overlay">
                                <div class="video-vol-wrap">
                                    <button type="button" class="btn-video-ctrl btn-video-mute" title="음소거 / 해제">🔇</button>
                                    <input type="range" class="vol-slider video-vol-slider" min="0" max="100" value="50" title="이 방송 개별 볼륨">
                                </div>
                                <div class="video-sync-wrap">
                                    <button type="button" class="btn-video-ctrl btn-sync-step btn-sync-back" title="미세 지연 (-0.5초 배속 · Shift: -0.2초)">-0.5</button>
                                    <span class="video-sync-badge hidden" title="클릭: 배속 싱크 초기화 (실시간 복귀)"></span>
                                    <button type="button" class="btn-video-ctrl btn-sync-step btn-sync-fwd" title="미세 당김 (+0.5초 배속 · Shift: +0.2초)">+0.5</button>
                                </div>
                                <div class="video-tm-bar">
                                    <button type="button" class="btn-video-ctrl btn-tm-back" title="10초 뒤로">-10s</button>
                                    <div class="video-tm-slider-wrap">
                                        <span class="video-tm-tooltip hidden"></span>
                                        <input type="range" class="vol-slider video-tm-slider" min="0" max="1000" value="1000" title="타임머신 위치 탐색">
                                    </div>
                                    <button type="button" class="btn-video-ctrl btn-tm-fwd" title="10초 앞으로">+10s</button>
                                    <span class="video-tm-time" title="실시간 대비 시간 차이">실시간</span>
                                    <button type="button" class="btn-video-ctrl btn-tm-live is-live" title="실시간 시점으로 복귀">● LIVE</button>
                                </div>
                                <div class="video-overlay-actions">
                                    <button type="button" class="btn-video-ctrl btn-video-qual" title="화질 전환 (360p / 480p / 720p / 원본)">360p</button>
                                    <button type="button" class="btn-video-ctrl btn-video-focus" title="전체화면 집중 모드 (더블클릭)">⤢</button>
                                    <button type="button" class="btn-video-ctrl btn-video-reload" title="영상 새로고침">↻</button>
                                </div>
                            </div>
                        </div>
                        <div class="focus-resizer" title="드래그: 채팅창 너비 조절 | 더블클릭: 기본 너비 초기화"></div>
                        <div class="chat-pane">
                            <div class="focus-chat-bar-wrap" aria-label="전체화면 채팅 선택 및 합치기">
                                <div class="focus-chat-bar"></div>
                                <span class="focus-chat-more" title="마우스를 올려 더 많은 방 보기">▾</span>
                            </div>
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
                            <div class="pinned-panel focus-pinned-panel">
                                <div class="pinned-header">
                                    <span class="focus-pin-title">📌 스트리머 · 매니저</span>
                                    <div class="pinned-actions">
                                        <button class="btn-pin-sub btn-focus-clear-pin">비우기</button>
                                        <button class="btn-pin-sub btn-focus-hide-pin">접기</button>
                                    </div>
                                </div>
                                <div class="pinned-list focus-pinned-list">
                                    <div class="pinned-empty">스트리머·매니저 채팅이 여기에 표시됩니다.</div>
                                </div>
                            </div>
                            <div class="resizer focus-inner-resizer" title="드래그: 칸 높이 조절 | 더블클릭: 1/5 초기화"></div>
                            <div class="messages-wrap focus-messages-wrap">
                                <div class="messages focus-messages"></div>
                                <button class="btn-scroll-bottom btn-focus-scroll-bottom">↓ 최신 채팅</button>
                            </div>
                        </div>
                    </div>
                `;

                this.headerEl = container.querySelector(".header");
                this.nameEl = container.querySelector(".streamer-name");
                this.controlsEl = container.querySelector(".header-controls");
                this.dotEl = container.querySelector(".status-dot");
                this.chatPaneEl = container.querySelector(".chat-pane");
                this.colBodyEl = container.querySelector(".col-body");
                this.focusResizerElNav = container.querySelector(".focus-resizer");
                this.focusChatBarWrapEl = container.querySelector(".focus-chat-bar-wrap");
                this.focusChatBarEl = container.querySelector(".focus-chat-bar");
                this.msgsEl = container.querySelector(".messages:not(.focus-messages)");
                this.scrollBtnEl = container.querySelector(".btn-scroll-bottom:not(.btn-focus-scroll-bottom)");
                this.focusMsgsEl = container.querySelector(".focus-messages");
                this.focusScrollBtnEl = container.querySelector(".btn-focus-scroll-bottom");
                this.videoPanelEl = container.querySelector(".video-panel");
                this.videoEl = container.querySelector(".video-player");
                this.videoSwipeHudEl = container.querySelector(".video-swipe-hud");
                this.videoMsgEl = container.querySelector(".video-msg");
                if (this.videoMsgEl) {
                    this.videoMsgEl.onclick = () => {
                        if (this.videoMsgEl.classList.contains("is-retryable")) {
                            this.startVideo();
                        }
                    };
                }
                this.videoBadgeEl = container.querySelector(".video-info-badge");
                this.videoBadgeMetaEl = container.querySelector(".video-badge-meta");
                this.videoBadgeTitleEl = container.querySelector(".video-badge-title");
                this.toggleVideoBtn = container.querySelector(".btn-video-toggle");
                this.videoMuteBtn = container.querySelector(".btn-video-mute");
                this.videoVolSlider = container.querySelector(".video-vol-slider");
                this.syncBackBtn = container.querySelector(".btn-sync-back");
                this.syncFwdBtn = container.querySelector(".btn-sync-fwd");
                this.syncBadgeEl = container.querySelector(".video-sync-badge");
                this.videoQualBtn = container.querySelector(".btn-video-qual");
                this.videoFocusBtn = container.querySelector(".btn-video-focus");
                this.videoReloadBtn = container.querySelector(".btn-video-reload");
                this.tmBackBtn = container.querySelector(".btn-tm-back");
                this.tmFwdBtn = container.querySelector(".btn-tm-fwd");
                this.tmSlider = container.querySelector(".video-tm-slider");
                this.tmTooltipEl = container.querySelector(".video-tm-tooltip");
                this.tmTimeEl = container.querySelector(".video-tm-time");
                this.tmLiveBtn = container.querySelector(".btn-tm-live");
                this.pinPanelEl = container.querySelector(".pinned-panel:not(.focus-pinned-panel)");
                this.pinListEl = container.querySelector(".pinned-list:not(.focus-pinned-list)");
                this.resizerEl = container.querySelector(".resizer:not(.focus-inner-resizer)");
                this.focusPinPanelEl = container.querySelector(".focus-pinned-panel");
                this.focusPinTitleEl = container.querySelector(".focus-pin-title");
                this.focusPinListEl = container.querySelector(".focus-pinned-list");
                this.focusResizerEl = container.querySelector(".focus-inner-resizer");
                this.togglePinBtn = container.querySelector(".btn-pin-toggle");
                this.reconnectBtn = container.querySelector(".btn-reconnect");
                this.closeColBtn = container.querySelector(".btn-close-col");
                this.clearPinBtn = container.querySelector(".btn-clear-pin");
                this.hidePinBtn = container.querySelector(".btn-hide-pin");
                this.focusClearPinBtn = container.querySelector(".btn-focus-clear-pin");
                this.focusHidePinBtn = container.querySelector(".btn-focus-hide-pin");

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
                    if (Date.now() < this.suppressDblClickUntil) return;
                    toggleFocusMode(this);
                };
                this.setupSwipeTimeMachine();
                this.setupBadgeDragSwap();
                this.videoMuteBtn.onclick = () => this.toggleVideoMute();
                this.videoVolSlider.oninput = (e) => {
                    this.setChannelVolume(Number(e.target.value));
                };
                this.applyAudioState();
                this.syncBackBtn.onclick = (e) => this.adjustFineSync(e.shiftKey ? -0.2 : -0.5);
                this.syncFwdBtn.onclick = (e) => this.adjustFineSync(e.shiftKey ? 0.2 : 0.5);
                this.syncBadgeEl.onclick = () => this.returnToLiveEdge();
                this.videoQualBtn.onclick = () => this.cycleVideoQuality();
                this.videoFocusBtn.onclick = () => toggleFocusMode(this);
                this.videoReloadBtn.onclick = () => this.startVideo();

                this.tmBackBtn.onclick = () => this.seekRelative(-10);
                this.tmFwdBtn.onclick = () => this.seekRelative(10);
                this.tmLiveBtn.onclick = () => this.returnToLiveEdge();
                const updateHoverTmTooltip = (ratio, pctLeft) => {
                    if (!this.tmTooltipEl) return;
                    const range = this.getSeekRange();
                    if (!range || range.safeSpan <= 0.5) {
                        this.tmTooltipEl.classList.add("hidden");
                        return;
                    }
                    const targetTime = range.safeStart + range.safeSpan * ratio;
                    const behind = Math.max(0, range.end - targetTime);
                    const clockStr = formatClockTime(Date.now() - behind * 1000);
                    this.tmTooltipEl.textContent = behind <= 4
                        ? `${clockStr} (실시간)`
                        : `${clockStr} (${this.formatOffsetLabel(behind)})`;
                    this.tmTooltipEl.style.left = `${Math.max(8, Math.min(92, pctLeft)).toFixed(1)}%`;
                    this.tmTooltipEl.classList.remove("hidden");
                };
                this.tmSlider.addEventListener("mousemove", (e) => {
                    const rect = this.tmSlider.getBoundingClientRect();
                    if (rect.width <= 0) return;
                    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                    updateHoverTmTooltip(ratio, ratio * 100);
                });
                this.tmSlider.addEventListener("mouseleave", () => {
                    if (this.tmTooltipEl && !this.isScrubbingTm) {
                        this.tmTooltipEl.classList.add("hidden");
                    }
                });
                this.tmSlider.oninput = (e) => {
                    this.isScrubbingTm = true;
                    const range = this.getSeekRange();
                    if (!range || range.safeSpan <= 0.5) return;
                    const ratio = Math.max(0, Math.min(1, Number(e.target.value) / 1000));
                    const targetTime = range.safeStart + range.safeSpan * ratio;
                    const behind = Math.max(0, range.end - targetTime);
                    this.tmTimeEl.textContent = behind <= 4 ? "실시간" : this.formatOffsetLabel(behind);
                    updateHoverTmTooltip(ratio, ratio * 100);
                };
                this.tmSlider.onchange = (e) => {
                    const range = this.getSeekRange();
                    this.isScrubbingTm = false;
                    if (this.tmTooltipEl) this.tmTooltipEl.classList.add("hidden");
                    if (!range || range.safeSpan <= 0.5) return;
                    const ratio = Math.max(0, Math.min(1, Number(e.target.value) / 1000));
                    this.seekToTime(range.safeStart + range.safeSpan * ratio);
                };
                this.videoEl.addEventListener("timeupdate", () => this.updateTimeMachineUI());
                this.videoEl.addEventListener("seeked", () => this.updateTimeMachineUI());

                const savedRatio = localStorage.getItem(`chzzk_pin_ratio_${channel.id}`);
                if (savedRatio) {
                    this.pinPanelEl.style.height = savedRatio;
                    if (this.focusPinPanelEl) this.focusPinPanelEl.style.height = savedRatio;
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
                this.focusHidePinBtn.onclick = () => this.setPinHidden(true);

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

                this.focusMsgsEl.addEventListener("scroll", () => {
                    if (this.ignoreScroll) return;
                    const dist = this.focusMsgsEl.scrollHeight - this.focusMsgsEl.scrollTop - this.focusMsgsEl.clientHeight;
                    this.autoScrollFocusMain = dist < 35;
                    this.focusScrollBtnEl.classList.toggle("visible", !this.autoScrollFocusMain);
                }, { passive: true });

                this.focusPinListEl.addEventListener("scroll", () => {
                    const dist = this.focusPinListEl.scrollHeight - this.focusPinListEl.scrollTop - this.focusPinListEl.clientHeight;
                    this.autoScrollFocusPin = dist < 25;
                }, { passive: true });

                this.scrollBtnEl.onclick = () => {
                    this.autoScrollMain = true;
                    while (this.msgsEl.children.length > MAX_MAIN_NODES_AUTO) {
                        this.msgsEl.removeChild(this.msgsEl.firstChild);
                    }
                    this.msgsEl.scrollTop = this.msgsEl.scrollHeight;
                    this.scrollBtnEl.classList.remove("visible");
                };

                this.focusScrollBtnEl.onclick = () => {
                    this.autoScrollFocusMain = true;
                    while (this.focusMsgsEl.children.length > MAX_MAIN_NODES_AUTO) {
                        this.focusMsgsEl.removeChild(this.focusMsgsEl.firstChild);
                    }
                    this.focusMsgsEl.scrollTop = this.focusMsgsEl.scrollHeight;
                    this.focusScrollBtnEl.classList.remove("visible");
                };

                this.ro = new ResizeObserver(() => {
                    this.keepBottomAnchor();
                    if (focusedClient === this) updateFocusChatBarOverflow();
                });
                this.ro.observe(this.msgsEl);
                this.ro.observe(this.pinListEl);
                this.ro.observe(this.focusMsgsEl);
                this.ro.observe(this.focusPinListEl);

                this.initResizer();

                this.reconnectBtn.onclick = () => {
                    this.retryCount = 0;
                    this.statusLineEl = null;
                    this.connect(false);
                };
                this.clearPinBtn.onclick = () => this.clearPinned();
                this.focusClearPinBtn.onclick = () => {
                    focusPendingPinQueue.length = 0;
                    this.focusPinListEl.innerHTML = `<div class="pinned-empty">스트리머·매니저 채팅이 여기에 표시됩니다.</div>`;
                };

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
                this.recentPins.length = 0;
                this.pinListEl.innerHTML = `<div class="pinned-empty">스트리머·매니저 채팅이 여기에 표시됩니다.</div>`;
                this.togglePinBtn.classList.remove("has-new");
            }

            runWithAnchorPreserved(fn) {
                const keepMain = this.autoScrollMain;
                const keepPin = this.autoScrollPin;
                const keepFocusMain = this.autoScrollFocusMain;
                const keepFocusPin = this.autoScrollFocusPin;
                this.ignoreScroll = true;
                fn();
                if (keepMain) this.autoScrollMain = true;
                if (keepPin) this.autoScrollPin = true;
                if (keepFocusMain) this.autoScrollFocusMain = true;
                if (keepFocusPin) this.autoScrollFocusPin = true;
                this.keepBottomAnchor();
                requestAnimationFrame(() => {
                    if (this.isDestroyed) return;
                    if (keepMain) {
                        this.autoScrollMain = true;
                        this.scrollBtnEl.classList.remove("visible");
                    }
                    if (keepPin) this.autoScrollPin = true;
                    if (keepFocusMain) {
                        this.autoScrollFocusMain = true;
                        if (this.focusScrollBtnEl) this.focusScrollBtnEl.classList.remove("visible");
                    }
                    if (keepFocusPin) this.autoScrollFocusPin = true;
                    this.keepBottomAnchor();
                    this.ignoreScroll = false;
                });
            }

            setPinHidden(hidden) {
                this.isPinHidden = hidden;
                localStorage.setItem(`chzzk_pin_hidden_${this.channel.id}`, hidden ? "1" : "0");

                this.runWithAnchorPreserved(() => {
                    const panels = [this.pinPanelEl, this.focusPinPanelEl];
                    const resizers = [this.resizerEl, this.focusResizerEl];
                    for (let i = 0; i < panels.length; i++) {
                        if (panels[i]) panels[i].classList.toggle("hidden", hidden);
                    }
                    for (let i = 0; i < resizers.length; i++) {
                        if (resizers[i]) resizers[i].classList.toggle("hidden", hidden);
                    }
                    const focusPinChip = this.focusChatBarEl?.querySelector(".focus-chat-chip.pin-btn");
                    if (focusPinChip) focusPinChip.classList.toggle("active", !hidden);
                    if (hidden) {
                        this.togglePinBtn.classList.remove("active");
                        this.togglePinBtn.classList.add("muted");
                    } else {
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
                    if (this.focusPinPanelEl) this.focusPinPanelEl.style.height = val;
                    localStorage.setItem(`chzzk_pin_ratio_${this.channel.id}`, val);
                });
            }

            initResizer() {
                let isDragging = false;

                this.onMouseMove = (e) => {
                    if (!isDragging) return;
                    const paneRect = this.chatPaneEl.getBoundingClientRect();
                    const topOffset = (focusedClient === this && this.focusChatBarWrapEl && this.focusChatBarWrapEl.offsetHeight)
                        ? this.focusChatBarWrapEl.offsetHeight
                        : 0;
                    const availableH = paneRect.height - topOffset;
                    if (availableH <= 0) return;

                    const newPinH = e.clientY - (paneRect.top + topOffset);
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
                    if (this.focusResizerEl) this.focusResizerEl.classList.remove("dragging");
                    document.body.style.cursor = "";
                    document.body.style.userSelect = "";
                };

                const bindResizerHandle = (handleEl) => {
                    if (!handleEl) return;
                    handleEl.addEventListener("mousedown", (e) => {
                        e.preventDefault();
                        isDragging = true;
                        handleEl.classList.add("dragging");
                        document.body.style.cursor = "ns-resize";
                        document.body.style.userSelect = "none";
                    });
                    handleEl.addEventListener("dblclick", () => {
                        this.setPinHeightPercent(20);
                    });
                };

                bindResizerHandle(this.resizerEl);
                bindResizerHandle(this.focusResizerEl);

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
                if (focusedClient === this && this.focusMsgsEl && this.autoScrollFocusMain) {
                    this.focusMsgsEl.scrollTop = this.focusMsgsEl.scrollHeight;
                }
                if (focusedClient === this && this.focusPinListEl && this.autoScrollFocusPin && !this.isPinHidden) {
                    this.focusPinListEl.scrollTop = this.focusPinListEl.scrollHeight;
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
                const pinEntry = {
                    ts: Date.now(),
                    channelId: this.channel.id,
                    channelName: this.channel.name,
                    role,
                    nickname,
                    color,
                    rawMsg,
                    extras,
                    timeStr: getTimeString()
                };
                this.recentPins.push(pinEntry);
                if (this.recentPins.length > 30) {
                    this.recentPins.shift();
                }
                this.pendingPinQueue.push(pinEntry);
                if (this.pendingPinQueue.length > MAX_PIN_NODES + 20) {
                    this.pendingPinQueue.splice(0, this.pendingPinQueue.length - MAX_PIN_NODES);
                }
                if (isCustomFocusChatView() && getFocusedChatSourceSet().has(this.channel.id)) {
                    focusPendingPinQueue.push(pinEntry);
                    if (focusPendingPinQueue.length > MAX_PIN_NODES + 20) {
                        focusPendingPinQueue.splice(0, focusPendingPinQueue.length - MAX_PIN_NODES);
                    }
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

                const chatEntry = {
                    ts: Number(item?.msgTime) || Date.now(),
                    channelId: this.channel.id,
                    channelName: this.channel.name,
                    type: "chat",
                    item,
                    isDonation,
                    parsedProfile,
                    parsedExtras
                };
                this.recentChats.push(chatEntry);
                if (this.recentChats.length > 100) {
                    this.recentChats.shift();
                }
                this.pendingMainQueue.push(chatEntry);
                const maxPending = this.autoScrollMain ? MAX_MAIN_NODES_AUTO : MAX_MAIN_NODES_PAUSED;
                if (this.pendingMainQueue.length > maxPending + 40) {
                    this.pendingMainQueue.splice(0, this.pendingMainQueue.length - maxPending);
                }
                if (isCustomFocusChatView() && getFocusedChatSourceSet().has(this.channel.id)) {
                    focusPendingMainQueue.push(chatEntry);
                    if (focusPendingMainQueue.length > MAX_MAIN_NODES_PAUSED + 40) {
                        focusPendingMainQueue.splice(0, focusPendingMainQueue.length - MAX_MAIN_NODES_PAUSED);
                    }
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

            buildPinnedNode(entry, roomInfo = null) {
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
                if (roomInfo) {
                    item.appendChild(createRoomTagEl(roomInfo.id, roomInfo.name));
                }
                item.appendChild(this.createRoleBadge(isStreamer));
                item.appendChild(nickSpan);
                item.appendChild(textSpan);
                return item;
            }

            buildChatNode(item, isDonation, parsedProfile, parsedExtras, roomInfo = null) {
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
                    if (roomInfo) {
                        head.appendChild(createRoomTagEl(roomInfo.id, roomInfo.name));
                    }
                    head.appendChild(document.createTextNode(`🧀 ${nickname} · ${amount}치즈`));

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

                if (roomInfo) {
                    div.appendChild(createRoomTagEl(roomInfo.id, roomInfo.name));
                }

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

            setVideoMessage(msg, isRetryable = false) {
                if (msg) {
                    this.videoMsgEl.textContent = msg;
                    this.videoMsgEl.classList.toggle("is-retryable", !!isRetryable);
                    this.videoMsgEl.classList.remove("hidden");
                } else {
                    this.videoMsgEl.textContent = "";
                    this.videoMsgEl.classList.remove("is-retryable");
                    this.videoMsgEl.classList.add("hidden");
                }
            }

            setVideoOpen(open) {
                if (!open && focusedClient === this) {
                    exitFocusMode();
                } else if (!open && focusedClient && focusAudioOnlyIds.includes(this.channel.id)) {
                    removeAudioOnlyFromFocus(this);
                    return;
                }
                if (!open) this.isAudioOnlyStream = false;
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

            applyAudioState() {
                const eff = getEffectiveVolume(this.channelVolume);
                const isGloballyMuted = masterMuted || masterVolume <= 0;
                const isSelfMuted = this.isChannelMuted || this.channelVolume <= 0;
                const isAudible = !isSelfMuted && !isGloballyMuted && eff > 0;
                this.videoEl.volume = eff;
                this.videoEl.muted = !isAudible;
                this.videoMuteBtn.textContent = isAudible ? "🔊" : "🔇";
                this.videoMuteBtn.classList.toggle("muted-active", !isAudible);
                if (isGloballyMuted && !isSelfMuted) {
                    this.videoMuteBtn.title = "전체 음소거 적용 중 (개별 음소거 전환 가능)";
                } else {
                    this.videoMuteBtn.title = `음소거 / 해제 (개별 ${isSelfMuted ? "음소거" : this.channelVolume + "%"})`;
                }
                if (this.videoVolSlider) {
                    this.videoVolSlider.value = String(isSelfMuted ? 0 : this.channelVolume);
                    this.videoVolSlider.title = `이 방송 개별 볼륨: ${isSelfMuted ? 0 : this.channelVolume}% (실제 ${Math.round(eff * 100)}%)`;
                }
            }

            setChannelVolume(pct) {
                const clamped = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
                this.channelVolume = clamped;
                if (clamped > 0) {
                    this.lastNonZeroVol = clamped;
                    this.isChannelMuted = false;
                } else {
                    this.isChannelMuted = true;
                }
                localStorage.setItem(`chzzk_channel_vol_${this.channel.id}`, String(this.channelVolume));
                this.applyAudioState();
                if (focusedClient && !focusRestoring && !focusExiting &&
                    (focusActiveVideoIds.includes(this.channel.id) || focusAudioOnlyIds.includes(this.channel.id))) {
                    saveCurrentFocusPreset();
                    if (focusAudioOnlyIds.includes(this.channel.id)) {
                        renderFocusVideoHud();
                        renderFocusRadioDock();
                    }
                }
            }

            setVideoMuted(muted, saveWatchPreset = true) {
                const nextMuted = !!muted;
                if (!nextMuted && this.channelVolume <= 0) {
                    this.channelVolume = this.lastNonZeroVol > 0 ? this.lastNonZeroVol : 50;
                    localStorage.setItem(`chzzk_channel_vol_${this.channel.id}`, String(this.channelVolume));
                }
                this.isChannelMuted = nextMuted;
                this.applyAudioState();
                if (saveWatchPreset && focusedClient && !focusRestoring && !focusExiting &&
                    (focusActiveVideoIds.includes(this.channel.id) || focusAudioOnlyIds.includes(this.channel.id))) {
                    saveCurrentFocusPreset();
                    if (focusAudioOnlyIds.includes(this.channel.id)) {
                        renderFocusVideoHud();
                        renderFocusRadioDock();
                    }
                }
            }

            toggleVideoMute() {
                this.setVideoMuted(!this.isChannelMuted);
            }

            applyPreferredVideoLevel(smooth = false) {
                if (!this.hls || !Array.isArray(this.hls.levels) || this.hls.levels.length === 0) return;
                const levels = this.hls.levels;
                let chosenIdx = -1;

                if (this.isAudioOnlyStream) {
                    let bestAudioBitrate = -1;
                    for (let i = 0; i < levels.length; i++) {
                        const lv = levels[i];
                        const isPureAudio = (!lv.height && !lv.width) || !lv.videoCodec;
                        const b = lv.bitrate || 0;
                        if (isPureAudio && b > bestAudioBitrate) {
                            bestAudioBitrate = b;
                            chosenIdx = i;
                        }
                    }
                    if (chosenIdx === -1) {
                        let minH = Infinity;
                        let minB = Infinity;
                        for (let i = 0; i < levels.length; i++) {
                            const h = levels[i].height || Infinity;
                            const b = levels[i].bitrate || Infinity;
                            if (h < minH || (h === minH && b < minB)) {
                                minH = h;
                                minB = b;
                                chosenIdx = i;
                            }
                        }
                    }
                } else if (this.preferredQuality === 0) {
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
                    if (smooth) {
                        this.hls.nextLevel = chosenIdx;
                    } else {
                        this.hls.currentLevel = chosenIdx;
                    }
                    if (this.isAudioOnlyStream) {
                        this.videoQualBtn.textContent = "라디오";
                    } else {
                        const actualH = levels[chosenIdx].height;
                        this.videoQualBtn.textContent = actualH
                            ? `${actualH}p`
                            : (this.preferredQuality === 0 ? "원본" : `${this.preferredQuality}p`);
                    }
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
                if (this.videoRetryTimer) {
                    clearTimeout(this.videoRetryTimer);
                    this.videoRetryTimer = null;
                }
                if (this._cancelSwipeScrub) this._cancelSwipeScrub();
                this.isTimeMachine = false;
                this.isScrubbingTm = false;
                this.clearFineSync(true);
                this.setLiveSyncActive(true);
                this.videoPanelEl.classList.remove("has-timemachine");
                if (this.hls) {
                    this.hls.destroy();
                    this.hls = null;
                }
                this.videoEl.pause();
                this.videoEl.removeAttribute("src");
                this.videoEl.load();
                this.setVideoMessage("");
                this.updateTimeMachineUI();
            }

            async startVideo(retryAttempt = 0) {
                if (this.isDestroyed || !this.isVideoOpen) return;
                if (retryAttempt === 0) {
                    this.stopVideo();
                    this.setVideoMessage("방송 스트림 불러오는 중...");
                } else if (this.videoRetryTimer) {
                    clearTimeout(this.videoRetryTimer);
                    this.videoRetryTimer = null;
                }
                const seq = this.videoLoadSeq;

                try {
                    const [res, HlsLib] = await Promise.all([
                        fetch(`/api/stream?channelId=${this.channel.id}`),
                        ensureHlsLib()
                    ]);
                    if (this.isDestroyed || !this.isVideoOpen || seq !== this.videoLoadSeq) return;
                    if (!res.ok) {
                        throw new Error(`HTTP ${res.status}`);
                    }

                    const info = await res.json();
                    if (info.error || !info.hlsUrl) {
                        if (info.retryable && retryAttempt < 2) {
                            throw new Error(info.error || "스트림 일시 오류");
                        }
                        this.setVideoMessage(info.error || "재생할 수 있는 방송이 없습니다.");
                        return;
                    }

                    const badgeTxt = info.category || "LIVE";
                    if (this.videoBadgeMetaEl) {
                        this.videoBadgeMetaEl.textContent = info.userCount
                            ? `${badgeTxt} · ${info.userCount.toLocaleString()}명`
                            : badgeTxt;
                    }
                    if (this.videoBadgeTitleEl) {
                        const liveTitle = (info.title || "").trim();
                        this.videoBadgeTitleEl.textContent = liveTitle;
                        this.videoBadgeTitleEl.classList.toggle("has-title", !!liveTitle);
                    }
                    this.videoPanelEl.title = info.title || "";
                    this.isTimeMachine = !!info.timeMachine;
                    this.videoPanelEl.classList.toggle("has-timemachine", this.isTimeMachine);
                    this.setLiveSyncActive(true);
                    this.applyAudioState();

                    const wantAudioOnly = !!this.isAudioOnlyStream;
                    const primaryUrl = (wantAudioOnly && info.audioOnlyUrl) ? info.audioOnlyUrl : info.hlsUrl;
                    const primaryTokName = (wantAudioOnly && info.audioOnlyUrl) ? (info.audioOnlySegTokenName || "") : (info.segTokenName || "");
                    const primaryTokVal = (wantAudioOnly && info.audioOnlyUrl) ? (info.audioOnlySegTokenVal || "") : (info.segTokenVal || "");
                    const fallbackUrl = (wantAudioOnly && info.audioOnlyUrl) ? info.hlsUrl : info.hlsFallbackUrl;
                    const fallbackTokName = (wantAudioOnly && info.audioOnlyUrl) ? (info.segTokenName || "") : (info.fallbackSegTokenName || "");
                    const fallbackTokVal = (wantAudioOnly && info.audioOnlyUrl) ? (info.segTokenVal || "") : (info.fallbackSegTokenVal || "");

                    if (HlsLib && HlsLib.isSupported()) {
                        let activeTokName = primaryTokName;
                        let activeTokVal = primaryTokVal;
                        const DefaultLoader = HlsLib.DefaultConfig.loader;
                        class ChzzkSegLoader extends DefaultLoader {
                            load(context, config, callbacks) {
                                if (activeTokName && activeTokVal && context && typeof context.url === "string") {
                                    if (
                                        /\.(m4v|m4s|mp4|ts)(\?|$)/i.test(context.url) &&
                                        !context.url.includes(`${activeTokName}=`)
                                    ) {
                                        const sep = context.url.includes("?") ? "&" : "?";
                                        context.url = `${context.url}${sep}${activeTokName}=${activeTokVal}`;
                                    }
                                }
                                super.load(context, config, callbacks);
                            }
                        }

                        const hls = new HlsLib({
                            loader: ChzzkSegLoader,
                            lowLatencyMode: true,
                            autoStartLoad: false,
                            liveSyncDuration: 2.0,
                            liveMaxLatencyDuration: 3.5,
                            maxLiveSyncPlaybackRate: 1.25,
                            maxBufferLength: this.isTimeMachine ? 6 : 4,
                            maxMaxBufferLength: this.isTimeMachine ? 12 : 8,
                            backBufferLength: this.isTimeMachine ? 30 : 4,
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
                            this.updateTimeMachineUI();
                        });

                        hls.on(HlsLib.Events.LEVEL_UPDATED, () => {
                            if (this.isDestroyed || seq !== this.videoLoadSeq) return;
                            this.keepInsideSlidingWindow();
                            this.updateTimeMachineUI();
                        });

                        hls.on(HlsLib.Events.ERROR, (evt, data) => {
                            if (this.isDestroyed || seq !== this.videoLoadSeq || !data?.fatal) return;
                            if (!retriedMedia && data.type === HlsLib.ErrorTypes.MEDIA_ERROR) {
                                retriedMedia = true;
                                hls.recoverMediaError();
                                return;
                            }
                            if (!usedFallback && fallbackUrl) {
                                usedFallback = true;
                                activeTokName = fallbackTokName;
                                activeTokVal = fallbackTokVal;
                                hls.loadSource(fallbackUrl);
                                return;
                            }
                            this.setVideoMessage("영상 연결이 끊어졌습니다. (↻ 새로고침)");
                        });

                        hls.loadSource(primaryUrl);
                        hls.attachMedia(this.videoEl);
                    } else if (this.videoEl.canPlayType("application/vnd.apple.mpegurl")) {
                        this.videoEl.src = primaryUrl;
                        this.setVideoMessage("");
                        this.videoEl.play().catch(() => {});
                        this.updateTimeMachineUI();
                    } else {
                        this.setVideoMessage("이 브라우저는 HLS 재생을 지원하지 않습니다.");
                    }
                } catch (err) {
                    if (this.isDestroyed || !this.isVideoOpen || seq !== this.videoLoadSeq) return;
                    if (retryAttempt < 2) {
                        const nextAttempt = retryAttempt + 1;
                        const delayMs = retryAttempt === 0 ? 700 : 1600;
                        this.setVideoMessage(`영상 연결 재시도 중... (${nextAttempt}/2)`);
                        this.videoRetryTimer = setTimeout(() => {
                            this.videoRetryTimer = null;
                            if (!this.isDestroyed && this.isVideoOpen && seq === this.videoLoadSeq) {
                                this.startVideo(nextAttempt);
                            }
                        }, delayMs);
                        return;
                    }
                    const reason = err && err.message ? err.message : "일시 오류";
                    this.setVideoMessage(`영상 로드 오류 (${reason}) · 클릭하여 재시도`, true);
                }
            }

            getSeekRange() {
                try {
                    const sk = this.videoEl.seekable;
                    if (!sk || sk.length === 0) return null;
                    const start = sk.start(0);
                    const end = sk.end(sk.length - 1);
                    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
                    const span = end - start;
                    const leftMargin = span > 40 ? 18 : Math.min(6, span * 0.15);
                    const safeEnd = Math.max(start, end - 1.2);
                    const safeStart = Math.min(safeEnd, start + leftMargin);
                    return {
                        start,
                        end,
                        span,
                        safeStart,
                        safeEnd,
                        safeSpan: Math.max(0, end - safeStart),
                    };
                } catch (e) {
                    return null;
                }
            }

            keepInsideSlidingWindow() {
                if (!this.isTimeMachine || this.isLivePinned || this.isScrubbingTm) return;
                const range = this.getSeekRange();
                if (!range || range.span <= 15) return;
                const cur = Number.isFinite(this.videoEl.currentTime) ? this.videoEl.currentTime : range.end;
                if (cur < range.start + 8 && range.end - (range.start + 14) > 4.5) {
                    try {
                        this.videoEl.currentTime = range.start + 14;
                    } catch (e) {}
                }
            }

            formatOffsetLabel(behindSec) {
                const sec = Math.max(0, Math.round(behindSec));
                const h = Math.floor(sec / 3600);
                const m = Math.floor((sec % 3600) / 60);
                const s = sec % 60;
                const mm = String(m).padStart(2, "0");
                const ss = String(s).padStart(2, "0");
                return h > 0 ? `-${h}:${mm}:${ss}` : `-${mm}:${ss}`;
            }

            applyHlsLiveConfig() {
                const allowCatchUp = this.isLivePinned && !this.hasManualSyncHold;
                if (this.hls && this.hls.config) {
                    this.hls.config.liveMaxLatencyDuration = allowCatchUp ? 3.5 : Infinity;
                    this.hls.config.maxLiveSyncPlaybackRate = allowCatchUp ? 1.25 : 1;
                }
                if (!allowCatchUp && !this.fineSyncTimer && this.videoEl.playbackRate !== 1) {
                    this.videoEl.playbackRate = 1;
                }
            }

            setLiveSyncActive(active) {
                this.isLivePinned = !!active;
                this.applyHlsLiveConfig();
            }

            updateFineSyncBadge(note = "") {
                if (!this.syncBadgeEl) return;
                if (note) {
                    this.syncBadgeEl.textContent = note;
                    this.syncBadgeEl.classList.remove("hidden", "adjusting");
                    return;
                }
                const adjusting = Math.abs(this.fineSyncRemainingSec) >= 0.015;
                const rounded = Math.round(this.manualSyncOffsetSec * 10) / 10;
                if (!adjusting && Math.abs(rounded) < 0.05) {
                    this.syncBadgeEl.textContent = "";
                    this.syncBadgeEl.classList.add("hidden");
                    this.syncBadgeEl.classList.remove("adjusting");
                    return;
                }
                const sign = rounded > 0 ? "+" : "";
                this.syncBadgeEl.textContent = `${adjusting ? "▶" : ""}${sign}${rounded.toFixed(1)}s`;
                this.syncBadgeEl.classList.remove("hidden");
                this.syncBadgeEl.classList.toggle("adjusting", adjusting);
            }

            clearFineSync(resetOffset = true) {
                if (this.fineSyncTimer) {
                    clearInterval(this.fineSyncTimer);
                    this.fineSyncTimer = null;
                }
                this.fineSyncRemainingSec = 0;
                if (resetOffset) {
                    this.hasManualSyncHold = false;
                    this.manualSyncOffsetSec = 0;
                }
                if (this.videoEl && this.videoEl.playbackRate !== 1) {
                    this.videoEl.playbackRate = 1;
                }
                this.applyHlsLiveConfig();
                this.updateFineSyncBadge();
            }

            getForwardHeadroomSec() {
                try {
                    const cur = Number.isFinite(this.videoEl.currentTime) ? this.videoEl.currentTime : 0;
                    const range = this.getSeekRange();
                    if (range) {
                        return Math.max(0, range.end - cur);
                    }
                    const buf = this.videoEl.buffered;
                    if (buf && buf.length > 0) {
                        return Math.max(0, buf.end(buf.length - 1) - cur);
                    }
                } catch (e) {}
                return 0;
            }

            adjustFineSync(deltaSec) {
                if (this.isDestroyed || !this.isVideoOpen || !this.videoEl) return;
                if (deltaSec > 0 && this.getForwardHeadroomSec() <= 0.55) {
                    if (this.fineSyncRemainingSec > 0) {
                        this.fineSyncRemainingSec = 0;
                        this.videoEl.playbackRate = 1;
                    }
                    showUndoToast("이미 최신 라이브 끝단에 가까워 더 당길 수 없습니다.", null);
                    return;
                }

                this.hasManualSyncHold = true;
                this.fineSyncRemainingSec += deltaSec;
                this.applyHlsLiveConfig();
                this.updateFineSyncBadge();

                if (this.fineSyncTimer) return;
                this.fineSyncLastTs = performance.now();
                this.fineSyncTimer = setInterval(() => {
                    const now = performance.now();
                    const dt = Math.min(0.25, Math.max(0, (now - this.fineSyncLastTs) / 1000));
                    this.fineSyncLastTs = now;

                    if (this.isDestroyed || !this.isVideoOpen || !this.videoEl) {
                        this.clearFineSync(true);
                        return;
                    }
                    if (this.videoEl.paused || this.videoEl.seeking || this.videoEl.readyState < 2) {
                        return;
                    }
                    if (Math.abs(this.fineSyncRemainingSec) < 0.015) {
                        this.clearFineSync(false);
                        return;
                    }

                    if (this.fineSyncRemainingSec < 0) {
                        const rate = 0.75;
                        this.videoEl.playbackRate = rate;
                        const achieved = (rate - 1.0) * dt; // 음수 (지연량)
                        const step = Math.max(this.fineSyncRemainingSec, achieved);
                        this.fineSyncRemainingSec -= step;
                        this.manualSyncOffsetSec += step;
                    } else {
                        if (this.getForwardHeadroomSec() <= 0.45) {
                            this.fineSyncRemainingSec = 0;
                            this.clearFineSync(false);
                            return;
                        }
                        const rate = 1.25;
                        this.videoEl.playbackRate = rate;
                        const achieved = (rate - 1.0) * dt; // 양수 (당김량)
                        const step = Math.min(this.fineSyncRemainingSec, achieved);
                        this.fineSyncRemainingSec -= step;
                        this.manualSyncOffsetSec += step;
                    }

                    if (Math.abs(this.fineSyncRemainingSec) < 0.015) {
                        this.clearFineSync(false);
                    } else {
                        this.updateFineSyncBadge();
                    }
                }, 40);
            }

            seekRelative(deltaSec) {
                if (!this.isTimeMachine) return;
                const range = this.getSeekRange();
                if (!range) return;
                const base = Number.isFinite(this.videoEl.currentTime) ? this.videoEl.currentTime : range.end;
                this.seekToTime(base + deltaSec);
            }

            seekToTime(targetSec) {
                if (!this.isTimeMachine) return;
                const range = this.getSeekRange();
                if (!range) return;
                this.clearFineSync(true);
                const clamped = Math.max(range.safeStart, Math.min(range.safeEnd, targetSec));
                if (range.end - clamped <= 4.0) {
                    this.returnToLiveEdge();
                    return;
                }
                this.setLiveSyncActive(false);
                try {
                    this.videoEl.currentTime = clamped;
                } catch (e) {}
                this.videoEl.play().catch(() => {});
                this.updateTimeMachineUI();
            }

            returnToLiveEdge() {
                this.clearFineSync(true);
                this.setLiveSyncActive(true);
                const range = this.getSeekRange();
                try {
                    if (this.hls && typeof this.hls.liveSyncPosition === "number" && this.hls.liveSyncPosition > 0) {
                        this.videoEl.currentTime = this.hls.liveSyncPosition;
                    } else if (range) {
                        this.videoEl.currentTime = Math.max(range.safeStart, range.end - 1.8);
                    }
                } catch (e) {}
                this.videoEl.play().catch(() => {});
                this.updateTimeMachineUI();
            }

            updateTimeMachineUI() {
                if (!this.tmSlider || !this.tmTimeEl || !this.tmLiveBtn) return;
                const range = this.getSeekRange();
                if (!this.isTimeMachine || !range || range.safeSpan <= 0.5) {
                    this.tmSlider.value = "1000";
                    this.tmTimeEl.textContent = "실시간";
                    this.tmLiveBtn.classList.add("is-live");
                    return;
                }
                const cur = Number.isFinite(this.videoEl.currentTime) ? this.videoEl.currentTime : range.end;
                const behind = Math.max(0, range.end - cur);
                const atLive = this.isLivePinned || behind <= 4.0;
                if (!this.isScrubbingTm) {
                    const ratio = Math.max(0, Math.min(1, (cur - range.safeStart) / range.safeSpan));
                    this.tmSlider.value = String(Math.round(ratio * 1000));
                }
                this.tmTimeEl.textContent = atLive ? "실시간" : this.formatOffsetLabel(behind);
                this.tmLiveBtn.classList.toggle("is-live", atLive);
            }

            snapToLiveEdge() {
                if (!this.isLivePinned || this.hasManualSyncHold) return;
                try {
                    if (this.hls && typeof this.hls.liveSyncPosition === "number" && this.hls.liveSyncPosition > 0) {
                        if (this.hls.liveSyncPosition - this.videoEl.currentTime > 3.5) {
                            this.videoEl.currentTime = this.hls.liveSyncPosition;
                        }
                    }
                } catch (e) {}
            }

            setupSwipeTimeMachine() {
                let pointerDown = false;
                let holdActive = false;
                let didMove = false;
                let startX = 0;
                let startY = 0;
                let holdTimer = null;
                let rangeCache = null;
                let baseTime = 0;
                let swipeTargetSec = null;

                const resetSwipeState = () => {
                    pointerDown = false;
                    holdActive = false;
                    didMove = false;
                    rangeCache = null;
                    swipeTargetSec = null;
                    if (holdTimer) {
                        clearTimeout(holdTimer);
                        holdTimer = null;
                    }
                    if (this.videoSwipeHudEl) {
                        this.videoSwipeHudEl.textContent = "";
                        this.videoSwipeHudEl.classList.add("hidden");
                    }
                    if (document.body.style.cursor === "ew-resize") {
                        document.body.style.cursor = "";
                    }
                };
                this._cancelSwipeScrub = resetSwipeState;

                this.videoPanelEl.addEventListener("mousedown", (e) => {
                    if (e.button !== 0 || !this.isVideoOpen) return;
                    if (e.target.closest(".video-overlay, .video-info-badge")) return;
                    resetSwipeState();
                    pointerDown = true;
                    startX = e.clientX;
                    startY = e.clientY;
                    holdTimer = setTimeout(() => {
                        holdTimer = null;
                        if (!pointerDown || this.isDestroyed || !this.isVideoOpen) return;
                        holdActive = true;
                        this.suppressDblClickUntil = Date.now() + 700;
                        if (!this.isTimeMachine) {
                            if (this.videoSwipeHudEl) {
                                this.videoSwipeHudEl.textContent = "타임머신 미지원 방송";
                                this.videoSwipeHudEl.classList.remove("hidden");
                            }
                            return;
                        }
                        rangeCache = this.getSeekRange();
                        if (!rangeCache || rangeCache.safeSpan <= 0.5) {
                            if (this.videoSwipeHudEl) {
                                this.videoSwipeHudEl.textContent = "이동 가능한 구간이 부족합니다";
                                this.videoSwipeHudEl.classList.remove("hidden");
                            }
                            rangeCache = null;
                            return;
                        }
                        baseTime = Number.isFinite(this.videoEl.currentTime) ? this.videoEl.currentTime : rangeCache.end;
                        swipeTargetSec = baseTime;
                        if (this.videoSwipeHudEl) {
                            this.videoSwipeHudEl.textContent = "↔ 좌우 스와이프 (Shift: 미세)";
                            this.videoSwipeHudEl.classList.remove("hidden");
                        }
                        document.body.style.cursor = "ew-resize";
                    }, 220);
                });

                this._onSwipeMove = (e) => {
                    if (!pointerDown) return;
                    if (!holdActive) {
                        if (Math.hypot(e.clientX - startX, e.clientY - startY) > 8) {
                            resetSwipeState();
                        }
                        return;
                    }
                    if (!this.isTimeMachine || !rangeCache) return;
                    const dx = e.clientX - startX;
                    if (Math.abs(dx) >= 6) didMove = true;
                    const ratePerPx = e.shiftKey ? 0.1 : 0.5;
                    swipeTargetSec = Math.max(rangeCache.safeStart, Math.min(rangeCache.safeEnd, baseTime + dx * ratePerPx));
                    const behind = Math.max(0, rangeCache.end - swipeTargetSec);
                    const delta = Math.round(swipeTargetSec - baseTime);
                    const arrow = delta < 0 ? "⏪" : (delta > 0 ? "⏩" : "⏱");
                    const clock = formatClockTime(Date.now() - behind * 1000);
                    const offsetTxt = behind <= 4 ? "실시간" : this.formatOffsetLabel(behind);
                    if (this.videoSwipeHudEl) {
                        this.videoSwipeHudEl.textContent = `${arrow} ${offsetTxt} · ${clock}`;
                        this.videoSwipeHudEl.classList.remove("hidden");
                    }
                };

                this._onSwipeUp = () => {
                    if (!pointerDown && !holdActive) return;
                    const commitTarget = (holdActive && didMove && this.isTimeMachine && swipeTargetSec !== null)
                        ? swipeTargetSec
                        : null;
                    if (holdActive) {
                        this.suppressDblClickUntil = Date.now() + 450;
                    }
                    resetSwipeState();
                    if (commitTarget !== null) {
                        this.seekToTime(commitTarget);
                    }
                };

                window.addEventListener("mousemove", this._onSwipeMove);
                window.addEventListener("mouseup", this._onSwipeUp);
                window.addEventListener("blur", resetSwipeState);
                this._onSwipeBlur = resetSwipeState;
            }

            setupBadgeDragSwap() {
                if (!this.videoBadgeEl || !this.videoPanelEl) return;
                this.videoBadgeEl.addEventListener("click", (e) => {
                    if (!focusedClient) return;
                    e.stopPropagation();
                    window.open(`https://chzzk.naver.com/live/${this.channel.id}`, "_blank", "noopener");
                });
                this.videoBadgeEl.addEventListener("dblclick", (e) => {
                    e.stopPropagation();
                });
                this.videoBadgeEl.addEventListener("dragstart", (e) => {
                    if (!focusedClient || focusActiveVideoIds.length < 2) {
                        e.preventDefault();
                        return;
                    }
                    dragSourceFocusId = this.channel.id;
                    if (e.dataTransfer) {
                        e.dataTransfer.effectAllowed = "move";
                        e.dataTransfer.setData("text/plain", this.channel.id);
                    }
                });
                this.videoPanelEl.addEventListener("dragover", (e) => {
                    if (!focusedClient || !dragSourceFocusId || dragSourceFocusId === this.channel.id) return;
                    if (!focusActiveVideoIds.includes(this.channel.id)) return;
                    e.preventDefault();
                    this.videoPanelEl.classList.add("drag-over");
                });
                this.videoPanelEl.addEventListener("dragleave", (e) => {
                    if (e.relatedTarget && this.videoPanelEl.contains(e.relatedTarget)) return;
                    this.videoPanelEl.classList.remove("drag-over");
                });
                this.videoPanelEl.addEventListener("drop", (e) => {
                    if (!focusedClient || !focusActiveVideoIds.includes(this.channel.id)) return;
                    const srcId = dragSourceFocusId || e.dataTransfer?.getData("text/plain");
                    dragSourceFocusId = null;
                    this.videoPanelEl.classList.remove("drag-over");
                    if (srcId && srcId !== this.channel.id) {
                        e.preventDefault();
                        swapFocusStreams(srcId, this.channel.id);
                    }
                });
                this.videoBadgeEl.addEventListener("dragend", () => {
                    dragSourceFocusId = null;
                    clientMap.forEach(c => c.videoPanelEl.classList.remove("drag-over"));
                });
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
                } else if (focusedClient && focusActiveVideoIds.includes(this.channel.id)) {
                    removeStreamFromFocus(this);
                } else if (focusedClient && focusAudioOnlyIds.includes(this.channel.id)) {
                    removeAudioOnlyFromFocus(this);
                }
                focusStreamSavedStates.delete(this.channel.id);
                this.isDestroyed = true;
                this.isConnecting = false;
                dirtyClients.delete(this);
                this.pendingMainQueue.length = 0;
                this.pendingPinQueue.length = 0;
                this.recentChats.length = 0;
                this.recentPins.length = 0;
                this.clearFineSync(true);
                if (this.livePollTimer) {
                    clearInterval(this.livePollTimer);
                    this.livePollTimer = null;
                }
                this.stopVideo();
                this.disconnectSocket();
                if (this.ro) this.ro.disconnect();
                window.removeEventListener("mousemove", this.onMouseMove);
                window.removeEventListener("mouseup", this.onMouseUp);
                if (this._onSwipeMove) window.removeEventListener("mousemove", this._onSwipeMove);
                if (this._onSwipeUp) window.removeEventListener("mouseup", this._onSwipeUp);
                if (this._onSwipeBlur) window.removeEventListener("blur", this._onSwipeBlur);
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
                syncModeDockUI();
                return;
            }

            const emptyScreen = wrapper.querySelector(".empty-screen");
            if (emptyScreen) emptyScreen.remove();

            wrapper.style.gridTemplateColumns = isHScrollMode
                ? `repeat(${activeChannels.length}, minmax(260px, 1fr))`
                : `repeat(${activeChannels.length}, 1fr)`;

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
            syncModeDockUI();
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
            activeChannels.push({ id: ch.id, name: ch.name });
            syncLayout();
            scheduleSaveHistoryToServer();
            return true;
        }

        const closedChannelStack = [];
        const redoChannelStack = [];
        let undoToastTimer = null;
        let undoToastMode = null;

        function showUndoToast(message, actionMode = null) {
            const toast = document.getElementById("undo-toast");
            const msgEl = document.getElementById("undo-toast-msg");
            const btnEl = document.getElementById("undo-toast-btn");
            if (!toast || !msgEl || !btnEl) return;
            msgEl.textContent = message;
            undoToastMode = actionMode;
            if (actionMode === "undo") {
                btnEl.textContent = "Ctrl+Z 되돌리기";
                btnEl.style.display = "inline-block";
            } else if (actionMode === "redo") {
                btnEl.textContent = "Ctrl+Y 다시 닫기";
                btnEl.style.display = "inline-block";
            } else {
                btnEl.style.display = "none";
            }
            toast.classList.add("visible");
            if (undoToastTimer) clearTimeout(undoToastTimer);
            undoToastTimer = setTimeout(() => {
                toast.classList.remove("visible");
                undoToastTimer = null;
                undoToastMode = null;
            }, 3000);
        }

        const undoToastBtnEl = document.getElementById("undo-toast-btn");
        if (undoToastBtnEl) {
            undoToastBtnEl.onclick = () => {
                if (undoToastMode === "redo") {
                    redoCloseChannel();
                } else if (undoToastMode === "undo") {
                    undoCloseChannel();
                }
            };
        }

        function removeChannel(targetId, fromRedo = false) {
            if (activeChannels.length <= 1) return false;
            const idx = activeChannels.findIndex(c => c.id === targetId);
            if (idx === -1) return false;
            if (!fromRedo) {
                redoChannelStack.length = 0;
            }
            const removed = activeChannels[idx];
            const client = clientMap.get(targetId);
            closedChannelStack.push({
                id: removed.id,
                name: removed.name,
                index: idx,
                isVideoOpen: !!(client && client.isVideoOpen),
                preferredQuality: client ? client.preferredQuality : 360,
                isChannelMuted: client ? client.isChannelMuted : true,
            });
            if (closedChannelStack.length > 20) {
                closedChannelStack.shift();
            }
            activeChannels = activeChannels.filter(c => c.id !== targetId);
            syncLayout();
            scheduleSaveHistoryToServer();
            showUndoToast(fromRedo ? `↪ ${removed.name} 다시 닫힘` : `${removed.name} 닫힘`, "undo");
            return true;
        }

        function undoCloseChannel() {
            while (closedChannelStack.length > 0) {
                const candidate = closedChannelStack[closedChannelStack.length - 1];
                if (activeChannels.some(c => c.id === candidate.id)) {
                    closedChannelStack.pop();
                    continue;
                }
                const entry = closedChannelStack.pop();
                const insertIdx = Math.max(0, Math.min(entry.index, activeChannels.length));
                activeChannels.splice(insertIdx, 0, { id: entry.id, name: entry.name });
                syncLayout();
                scheduleSaveHistoryToServer();

                const restored = clientMap.get(entry.id);
                if (restored) {
                    restored.preferredQuality = entry.preferredQuality ?? 360;
                    restored.videoQualBtn.textContent = restored.preferredQuality === 0 ? "원본" : `${restored.preferredQuality}p`;
                    restored.setVideoMuted(entry.isChannelMuted);
                    if (entry.isVideoOpen) {
                        restored.setVideoOpen(true);
                    }
                }
                redoChannelStack.push(entry);
                if (redoChannelStack.length > 20) {
                    redoChannelStack.shift();
                }
                showUndoToast(`↩ ${entry.name} 복구됨`, "redo");
                return true;
            }
            return false;
        }

        function redoCloseChannel() {
            while (redoChannelStack.length > 0) {
                const candidate = redoChannelStack[redoChannelStack.length - 1];
                if (!activeChannels.some(c => c.id === candidate.id)) {
                    redoChannelStack.pop();
                    continue;
                }
                if (activeChannels.length <= 1) {
                    showUndoToast("최소 1개 채널은 남겨야 합니다.", null);
                    return false;
                }
                redoChannelStack.pop();
                return removeChannel(candidate.id, true);
            }
            return false;
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
            const entry = modalHistory[comboIdx];
            const combo = Array.isArray(entry) ? entry : entry?.channels;
            if (!Array.isArray(combo) || combo.length === 0) return;
            activeChannels = combo.map(c => ({ id: c.id, name: c.name }));
            syncLayout();
            scheduleSaveHistoryToServer();
            closeModal();
        }

        async function toggleHistoryPin(comboIdx) {
            try {
                const res = await fetch("/api/history/pin", {
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

            titleEl.textContent = "📜 스크립트 최근 기록 (📌 고정 · 칩 클릭: 1명 추가)";
            countEl.textContent = `${modalHistory.length} / 9`;
            listEl.innerHTML = "";

            if (modalHistory.length === 0) {
                const empty = document.createElement("div");
                empty.className = "history-empty";
                empty.textContent = "스크립트에서 입력한 최근 기록이 없습니다.";
                listEl.appendChild(empty);
                return;
            }

            modalHistory.forEach((rawEntry, comboIdx) => {
                const isPinned = !Array.isArray(rawEntry) && !!rawEntry?.pinned;
                const combo = Array.isArray(rawEntry) ? rawEntry : (rawEntry?.channels || []);
                if (!Array.isArray(combo) || combo.length === 0) return;

                const row = document.createElement("div");
                row.className = `history-row${isPinned ? " pinned" : ""}`;
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

                const pinBtn = document.createElement("button");
                pinBtn.type = "button";
                pinBtn.className = `btn-mini btn-history-pin${isPinned ? " pinned" : ""}`;
                pinBtn.textContent = "📌";
                pinBtn.title = isPinned ? "고정 해제" : "상단에 고정 (새 기록이 쌓여도 유지)";
                pinBtn.onclick = () => toggleHistoryPin(comboIdx);
                actionsEl.appendChild(pinBtn);

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

            countLabel.textContent = `${activeChannels.length}개`;
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
            const masterMuteBtn = document.getElementById("btn-master-mute");
            const masterSlider = document.getElementById("master-vol-slider");
            if (!tools) return;

            if (masterMuteBtn) {
                masterMuteBtn.addEventListener("click", () => {
                    const currentlyMuted = masterMuted || masterVolume <= 0;
                    if (currentlyMuted) {
                        masterMuted = false;
                        if (masterVolume <= 0) {
                            masterVolume = masterLastNonZero > 0 ? masterLastNonZero : 50;
                            localStorage.setItem(MASTER_VOL_KEY, String(masterVolume));
                        }
                    } else {
                        masterMuted = true;
                    }
                    localStorage.setItem(MASTER_MUTE_KEY, masterMuted ? "1" : "0");
                    syncAllClientAudio();
                });
            }
            if (masterSlider) {
                masterSlider.addEventListener("input", (e) => {
                    const next = Math.max(0, Math.min(100, Math.round(Number(e.target.value) || 0)));
                    masterVolume = next;
                    if (next > 0) {
                        masterLastNonZero = next;
                        masterMuted = false;
                    } else {
                        masterMuted = true;
                    }
                    localStorage.setItem(MASTER_VOL_KEY, String(masterVolume));
                    localStorage.setItem(MASTER_MUTE_KEY, masterMuted ? "1" : "0");
                    syncAllClientAudio();
                });
            }
            syncMasterVolumeUI();

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
            # 웹 화면 편집은 현재 세션 새로고침용 상태만 갱신하고, 스크립트 최근 기록 파일에는 쓰지 않음
            if channels:
                with history_lock:
                    INITIAL_CHANNELS = channels
            self.send_bytes(200, "application/json; charset=utf-8", b'{"ok":true}')

        elif parsed.path == "/api/history/pin":
            body = self.read_json_body()
            if not isinstance(body, dict) or not isinstance(body.get("index"), int):
                self.send_bytes(400, "application/json; charset=utf-8", b'{"ok":false}')
                return
            updated = toggle_history_pin(body["index"])
            self.send_bytes(
                200,
                "application/json; charset=utf-8",
                json.dumps({"ok": True, "history": updated}, ensure_ascii=False).encode("utf-8"),
            )

        elif parsed.path == "/api/history/delete":
            body = self.read_json_body()
            if not isinstance(body, dict) or not isinstance(body.get("index"), int):
                self.send_bytes(400, "application/json; charset=utf-8", b'{"ok":false}')
                return
            idx = body["index"]
            history = load_history()
            if 0 <= idx < len(history):
                history.pop(idx)
                history = save_history(history)
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
    print(f" 📜 스크립트 최근 기록 (최대 {MAX_HISTORY}개 · 📌 고정):")
    for idx, entry in enumerate(history, 1):
        pin_mark = "📌 " if entry.get("pinned") else ""
        names = ", ".join(ch["name"] for ch in entry["channels"])
        print(f"   [{idx}] {pin_mark}{names}")
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
        ("fg:#a1a1aa bold", f" 📜 스크립트 최근 기록 (클릭해서 입력 · 최대 {MAX_HISTORY}개 · 📌 고정)\n"),
    ]
    for idx, entry in enumerate(history, 1):
        frags.append(("", "   "))
        if entry.get("pinned"):
            frags.append(("fg:#00ffa3 bold", f"[{idx}]📌", make_click_handler(str(idx))))
        else:
            frags.append(("fg:#00ffa3 bold", f"[{idx}]", make_click_handler(str(idx))))
        frags.append(("", " "))
        for sub_idx, ch in enumerate(entry["channels"]):
            if sub_idx > 0:
                frags.append(("fg:#71717a", ", "))
            frags.append(("fg:#e4e4e7", ch["name"], make_click_handler(ch["name"])))
        frags.append(("", "\n"))
    return FormattedText(frags)

def read_cli_input(pt, history):
    """회색 placeholder와 하단 클릭형 최근 기록을 포함한 CLI 입력을 받습니다."""
    if history:
        recent_names = ", ".join(ch["name"] for ch in history[0]["channels"])
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
        for ch in entry["channels"]:
            if "".join(ch["name"].split()).lower() == norm:
                return ch
    return None

def resolve_cli_inputs(raw_input, history):
    """콤마로 구분된 번호(1, 12, 1-2) 및 이름/ID/URL 입력을 채널 목록으로 변환합니다."""
    tokens = [tok.strip() for tok in raw_input.split(",") if tok.strip()]
    resolved = []
    seen_ids = set()

    def append_unique(ch):
        if ch["id"] not in seen_ids:
            seen_ids.add(ch["id"])
            resolved.append({"id": ch["id"], "name": ch["name"]})

    for tok in tokens:

        if history and len(tok) == 1 and tok in "123456789":
            c_idx = int(tok) - 1
            if 0 <= c_idx < len(history):
                chs = history[c_idx]["channels"]
                for ch in chs:
                    append_unique(ch)
                names = ", ".join(ch["name"] for ch in chs)
                print(f" ⚡ [{tok}] 불러오기 -> {names}")
                continue
            print(f" ❌ [{tok}]번 기록이 없습니다.")
            return None

        is_pair = len(tok) == 2 and tok.isdigit() and tok[0] != "0" and tok[1] != "0"
        dash_m = re.match(r"^([1-9])-(\d+)$", tok)
        if history and (is_pair or dash_m):
            c_idx = int(tok[0]) - 1
            s_idx = (int(dash_m.group(2)) if dash_m else int(tok[-1])) - 1
            if 0 <= c_idx < len(history) and 0 <= s_idx < len(history[c_idx]["channels"]):
                ch = history[c_idx]["channels"][s_idx]
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

def switch_to_version(entry, pin_mode=None):
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

    if pin_mode == "auto":
        save_update_config(True, "")
        print(" 🔄 최신 버전 선택으로 자동 업데이트가 켜졌습니다.")
    elif pin_mode == "pin":
        save_update_config(False, tag)
        print(f" 📌 {tag} 버전으로 고정되어 자동 업데이트를 껐습니다. (다시 켜려면 'v' → a)")

    print(f" ✅ {tag} 버전으로 변경 완료! (이전 파일 백업: {os.path.basename(backup_path)})")
    print(" 🔄 새 버전으로 다시 실행합니다...\n")

    next_args = [arg for arg in sys.argv[1:] if arg != "--skip-auto-update"]
    if os.name == "nt":
        rc = subprocess.call([sys.executable, script_path] + next_args)
        sys.exit(rc)
    else:
        os.execv(sys.executable, [sys.executable, script_path] + next_args)

def auto_update_if_needed():
    """시작 시 자동 업데이트가 켜져 있고 새 GitHub 릴리스가 있으면 즉시 업데이트합니다."""
    if "--skip-auto-update" in sys.argv[1:]:
        return False

    cfg = load_update_config()
    if not cfg["auto_update"]:
        print(f" 📌 자동 업데이트 꺼짐 ({APP_VERSION} 고정 중 · 'v' 메뉴에서 변경 가능)")
        print("--------------------------------------------------")
        return False

    newer_tag = check_latest_version(timeout=2.0)
    if not newer_tag:
        return False

    print(f" ✨ 새 버전({newer_tag})을 발견했습니다! 자동으로 업데이트합니다...")
    direct_url = f"https://github.com/{GITHUB_REPO}/releases/download/{newer_tag}/{GITHUB_ASSET_NAME}"
    if switch_to_version({"tag": newer_tag, "url": direct_url}):
        return True

    versions = fetch_available_versions()
    target_entry = next((v for v in versions if v["tag"] == newer_tag), None)
    if not target_entry and versions:
        cand = versions[0]
        if parse_version_tuple(cand["tag"]) > parse_version_tuple(APP_VERSION):
            target_entry = cand
    if target_entry and target_entry["url"] != direct_url:
        if switch_to_version(target_entry):
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

    cfg = load_update_config()
    auto_txt = "켜짐" if cfg["auto_update"] else f"꺼짐 ({cfg['pinned_version'] or APP_VERSION} 고정)"

    print("--------------------------------------------------")
    print(f" 📦 GitHub 버전 목록 (현재: {APP_VERSION} · 자동 업데이트: {auto_txt})")
    for idx, v in enumerate(versions, 1):
        cur_mark = " (현재)" if v["tag"] == APP_VERSION else ""
        date_part = f" - {v['date']}" if v["date"] else ""
        note_part = f" | {v['note']}" if v["note"] else ""
        print(f"   [{idx}] {v['tag']}{cur_mark}{date_part}{note_part}")
    print("--------------------------------------------------")

    choice = input("👉 변경할 버전 번호 (a = 자동 업데이트 켜기/끄기, 엔터 = 취소): ").strip()
    if not choice:
        print()
        return
    if choice.lower() == "a":
        next_auto = not cfg["auto_update"]
        save_update_config(next_auto, "" if next_auto else APP_VERSION)
        if next_auto:
            print("\n ✅ 자동 업데이트를 켰습니다.")
            auto_update_if_needed()
            print()
        else:
            print(f"\n 📌 자동 업데이트를 껐습니다. ({APP_VERSION} 고정)\n")
        return
    if not choice.isdigit():
        print("\n ❌ 숫자를 입력해 주세요.\n")
        return

    idx = int(choice) - 1
    if not (0 <= idx < len(versions)):
        print(f"\n ❌ 1 ~ {len(versions)} 사이의 번호를 입력하세요.\n")
        return

    chosen = versions[idx]
    max_tuple = max(parse_version_tuple(v["tag"]) for v in versions)
    is_latest_pick = parse_version_tuple(chosen["tag"]) >= max_tuple
    pin_mode = "auto" if is_latest_pick else "pin"

    if chosen["tag"] == APP_VERSION:
        ans = input(f" 이미 {APP_VERSION} 버전입니다. 다시 내려받아 덮어쓸까요? (y/N): ").strip().lower()
        if ans != "y":
            print()
            return

    switch_to_version(chosen, pin_mode=pin_mode)

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
    print(f" 🎬 치지직 실시간 채팅 멀티뷰 ({APP_VERSION})")
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
            print("              p1 = 1번 고정  d1 = 1번 삭제  n = 빈 화면  c = 쿠키  v = 버전")
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

        if history and user_input.lower().startswith("p") and user_input[1:].strip().isdigit():
            pin_idx = int(user_input[1:].strip()) - 1
            if 0 <= pin_idx < len(history):
                target_names = ", ".join(ch["name"] for ch in history[pin_idx]["channels"])
                was_pinned = history[pin_idx]["pinned"]
                toggle_history_pin(pin_idx)
                state_txt = "고정 해제" if was_pinned else "📌 고정"
                print(f"\n {state_txt} 완료: {target_names}\n")
            else:
                print(f"\n ❌ 1 ~ {len(history)} 사이의 번호만 고정할 수 있습니다.\n")
            continue

        if history and user_input.lower().startswith("d") and user_input[1:].strip().isdigit():
            del_idx = int(user_input[1:].strip()) - 1
            if 0 <= del_idx < len(history):
                removed = history.pop(del_idx)
                save_history(history)
                removed_names = ", ".join(ch["name"] for ch in removed["channels"])
                print(f"\n 🗑 [{del_idx + 1}] 기록을 삭제했습니다: {removed_names}\n")
            else:
                print(f"\n ❌ 1 ~ {len(history)} 사이의 번호만 삭제할 수 있습니다.\n")
            continue

        if user_input == "":
            if history:
                INITIAL_CHANNELS = list(history[0]["channels"])
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