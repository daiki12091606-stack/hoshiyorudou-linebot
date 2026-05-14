import os
import redis as _redis_lib
from collections import deque
import json
import re
import threading
import uuid
from datetime import datetime
from io import BytesIO
from flask import Flask, request, abort, make_response, jsonify
import anthropic

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
Configuration, ApiClient, MessagingApi,
ReplyMessageRequest, PushMessageRequest,
TextMessage, ImageMessage,
QuickReply, QuickReplyItem, MessageAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

user_data = {}

# ââ Redis helper (persistent storage) ââââââââââââââââââââââââââââââ
_redis_client = None
_user_data_fallback = {}
_conv_history_fallback = {}

def _get_redis():
    global _redis_client
    if _redis_client is None:
        url = os.environ.get("REDIS_URL")
        if url:
            try:
                _redis_client = _redis_lib.from_url(url, decode_responses=True)
                _redis_client.ping()
            except Exception:
                _redis_client = None
    return _redis_client

def get_user(uid):
    r = _get_redis()
    if r:
        try:
            raw = r.get(f"user:{uid}")
            return json.loads(raw) if raw else {}
        except Exception:
            pass
    return _user_data_fallback.get(uid, {})

def set_user(uid, data):
    r = _get_redis()
    if r:
        try:
            r.setex(f"user:{uid}", 180 * 86400, json.dumps(data, ensure_ascii=False))
            return
        except Exception:
            pass
    _user_data_fallback[uid] = data

def get_conv(uid):
    r = _get_redis()
    if r:
        try:
            raw = r.get(f"conv:{uid}")
            return json.loads(raw) if raw else []
        except Exception:
            pass
    return _conv_history_fallback.get(uid, [])

def set_conv(uid, history):
    r = _get_redis()
    if r:
        try:
            r.setex(f"conv:{uid}", 7 * 86400, json.dumps(history, ensure_ascii=False))
            return
        except Exception:
            pass
    _conv_history_fallback[uid] = history
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
graph_cache = {}
image_cache = {}
image_cache_order = deque(maxlen=60)
MAX_IMAGES = 60

SYSTEMS = ["åæ±æ¨å½", "ç®å½å­¦", "è¥¿æ´å æè¡", "æ°ç§è¡", "ç´«å¾®ææ°"]
COLORS = {
    "åæ±æ¨å½": "#4FC3F7",
    "ç®å½å­¦": "#FFD54F",
    "è¥¿æ´å æè¡": "#FF7043",
    "æ°ç§è¡": "#66BB6A",
    "ç´«å¾®ææ°": "#AB47BC",
}
SYSTEM_EN = {
    "åæ±æ¨å½": "4Pillars",
    "ç®å½å­¦": "9-Star",
    "è¥¿æ´å æè¡": "Western",
    "æ°ç§è¡": "Numerol.",
    "ç´«å¾®ææ°": "ZWDS",
}
LEGEND_TEXT = (
    "â" * 14 + "\n"
    "\U0001F7E6 4Pillars = åæ±æ¨å½\n"
    "\U0001F7E1 9-Star = ç®å½å­¦\n"
    "\U0001F534 Western = è¥¿æ´å æè¡\n"
    "\U0001F7E2 Numerol. = æ°ç§è¡\n"
    "\U0001F7E3 ZWDS = ç´«å¾®ææ°"
)
CAT_EMOJI = {
    "å¨ä½é": "ð",
    "éé": "ð°",
    "ææé": "ð",
    "ä»äºé": "ð¼",
    "å¥åº·é": "ðª",
    "å¯¾äººé": "ð¤",
}

def parse_birthday(text):
    import re as _re
    patterns = [
        r'(\d{4})[å¹´/\-.]*(\d{1,2})[æ/\-.]*(\d{1,2})',
        r'(\d{2})[å¹´/\-.]*(\d{1,2})[æ/\-.]*(\d{1,2})',
    ]
    for p in patterns:
        m = _re.search(p, text)
        if m:
            year = int(m.group(1))
            if year < 100:
                year += 1900
            try:
                return datetime(year, int(m.group(2)), int(m.group(3))).strftime("%Yå¹´%mæ%dæ¥")
            except Exception:
                pass
    return None

def parse_birth_time(text):
    import re as _re
    m = _re.search(r'åå\s*(\d{1,2})æ(?:\s*(\d{1,2})å)?', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2) or 0)
        return "åå" + str(h) + "æ" + (str(mn) + "å" if mn else "")
    m = _re.search(r'åå¾\s*(\d{1,2})æ(?:\s*(\d{1,2})å)?', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2) or 0)
        return "åå¾" + str(h) + "æ" + (str(mn) + "å" if mn else "")
    m = _re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if m:
        return str(int(m.group(1))) + "æ" + str(int(m.group(2))) + "å"
    return None

def parse_extra_info(text):
    import re as _re
    result = {}
    cleaned = _re.sub(r'\d{2,4}[å¹´/\-.]+\d{1,2}[æ/\-.]+\d{1,2}æ¥?', '', text)
    cleaned = _re.sub(r'åå|åå¾|\d{1,2}æ\d*å?|\d{1,2}:\d{2}', '', cleaned)
    cleaned = _re.sub(r'[\sã]+', ' ', cleaned).strip()
    kana_paren = _re.search(r'[ï¼(]([ã-ãã¼]{2,})[ï¼)]', cleaned)
    if kana_paren:
        result["name_kana"] = kana_paren.group(1)
        cleaned = cleaned.replace(kana_paren.group(0), '').strip()
    bp = _re.search(r'[ã-é¿¿ã -ã¿]+[é½éåºçå¸åºçºæ]', cleaned)
    if bp:
        result["birthplace"] = bp.group(0)
        cleaned = cleaned.replace(bp.group(0), '').strip()
    nm = _re.search(r'[ä¸-é¿¿ã -ã¿][ã-é¿¿ã -ã¿]{1,7}', cleaned)
    if nm:
        result["name"] = nm.group(0)
    if "name_kana" not in result:
        kana_only = _re.search(r'^[ã-ãã¼]{2,}$', cleaned.strip())
        if kana_only:
            result["name_kana"] = kana_only.group(0)
    return result

def build_user_context(user):
    bd = user.get("birthday", "")
    bt = user.get("birth_time")
    nm = user.get("name")
    nk = user.get("name_kana")
    bp = user.get("birthplace")
    lines = ["çå¹´ææ¥: " + bd + (" " + bt if bt else "")]
    if nm:
        lines.append("åå: " + nm + ("ï¼" + nk + "ï¼" if nk else ""))
    if bp:
        lines.append("åºçå°: " + bp)
    return "\n".join(lines)

def birthday_to_iso(bday):
    try:
        return datetime.strptime(bday, "%Yå¹´%mæ%dæ¥").strftime("%Y-%m-%d")
    except Exception:
        return bday

def iso_to_birthday(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%Y-%m-%d").strftime("%Yå¹´%mæ%dæ¥")
    except Exception:
        return iso

def bot_base_url():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    return f"https://{domain}" if domain else ""

def score_bar(score):
    filled = max(0, min(5, round(score / 10 * 5)))
    return "â­" * filled + "â" * (5 - filled)

def block_bar(score):
    filled = max(0, min(5, round(score / 2)))
    return "â" * filled + "â" * (5 - filled)

def main_menu_qr():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="ð ä»æ¥ã®éå¢", text="ä»æ¥ã®éå¢")),
        QuickReplyItem(action=MessageAction(label="ð ä»æã®éå¢", text="ä»æã®éå¢")),
        QuickReplyItem(action=MessageAction(label="ð® å è¡å¥è¨ºæ­", text="å è¡å¥è¨ºæ­")),
        QuickReplyItem(action=MessageAction(label="ð ä»å¹´/12å¹´æ¨ç§»ã°ã©ã", text="ä»å¹´/12å¹´æ¨ç§»ã°ã©ã")),
        QuickReplyItem(action=MessageAction(label="ð éå»12å¹´ã®éå¢", text="éå»12å¹´")),
        QuickReplyItem(action=MessageAction(label="âï¸ èªçæ¥å¤æ´", text="èªçæ¥å¤æ´")),
    ])

def push(user_id, text, with_menu=True):
    with ApiClient(configuration) as api_client:
        msg = TextMessage(text=text, quick_reply=main_menu_qr() if with_menu else None)
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[msg])
        )

def push_image(user_id, img_url):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=user_id,
                messages=[ImageMessage(
                    original_content_url=img_url,
                    preview_image_url=img_url,
                )]
            )
        )

def reply_msg(reply_token, text, with_menu=False):
    with ApiClient(configuration) as api_client:
        msg = TextMessage(text=text, quick_reply=main_menu_qr() if with_menu else None)
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[msg])
        )

def store_image(img_id, img_bytes):
    image_cache[img_id] = img_bytes
    image_cache_order.append(img_id)
    while len(image_cache_order) > MAX_IMAGES:
        old_id = image_cache_order.popleft()
        image_cache.pop(old_id, None)

def ask_claude(prompt, max_tokens=2000):
    resp = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system="ããªãã¯å ãå¸«AIã§ããæå®ãããJSONå½¢å¼ã®ã¿ãè¿ãã¦ãã ãããèª¬ææã»ãã¼ã¯ãã¦ã³ä¸è¦ã",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        return json.loads(m.group())
    return None

# ââ å è¡è¨ç®ãã«ãã¼ âââââââââââââââââââââââââââââââââââââââââââââ

def _digit_reduce(n):
    while n > 9 and n not in (11, 22, 33):
        n = sum(int(c) for c in str(n))
    return n

def _five_elem(kan):
    return [0, 0, 1, 1, 2, 2, 3, 3, 4, 4][kan % 10]

def _stem_harmony(bkan, tkan):
    be, te = _five_elem(bkan), _five_elem(tkan)
    gen = {(0,1),(1,2),(2,3),(3,4),(4,0)}
    ctrl = {(0,2),(2,4),(4,1),(1,3),(3,0)}
    if be == te: return 7
    if (be, te) in gen: return 9
    if (te, be) in gen: return 7
    if (be, te) in ctrl: return 3
    if (te, be) in ctrl: return 2
    return 5

def _date_day_kan(d):
    from datetime import date as _dc
    return ((_dc(d.year, d.month, d.day) - _dc(2000, 1, 1)).days % 10 + 10) % 10

def _kyusei_daily(d):
    from datetime import date as _dc
    delta = (_dc(d.year, d.month, d.day) - _dc(2000, 1, 6)).days
    s = 6 - (delta % 9)
    while s <= 0: s += 9
    return s

def _kyusei_harmony(personal, daily):
    diff = (personal - daily) % 9
    return {0:8, 1:6, 2:7, 3:5, 4:3, 5:4, 6:6, 7:7, 8:5}.get(diff, 5)

def _western_daily(sun_sign, d):
    from datetime import date as _dc
    days = (_dc(d.year, d.month, d.day) - _dc(2000, 1, 1)).days
    moon_sign = days % 12
    diff = (moon_sign - sun_sign) % 12
    return {0:9,1:5,2:7,3:5,4:8,5:6,6:3,7:5,8:8,9:6,10:7,11:5}.get(diff, 5)

def _numerology_daily(life_path, name_num, d):
    pd = _digit_reduce(d.year + d.month + d.day)
    lp_m = life_path % 9 or 9
    pd_m = pd % 9 or 9
    diff = abs(lp_m - pd_m)
    base = {0:9,1:7,2:6,3:8,4:3,5:4,6:8,7:6,8:7}.get(diff % 9, 5)
    nd = abs((name_num % 9 or 9) - pd_m)
    return min(10, base + (1 if nd in (0, 3, 6) else 0))

def _zwds_daily(zwds_base, d):
    combined = ((d.month + zwds_base - 2) % 12 + d.day % 12) % 12
    return {0:5,1:8,2:4,3:6,4:8,5:4,6:6,7:8,8:4,9:6,10:8,11:5}.get(combined, 5)

def _parse_bdata(user):
    import re as _re
    birthday = user.get("birthday", "")
    name_kana = user.get("name_kana") or ""
    birth_time = user.get("birth_time") or ""
    bday_iso = birthday_to_iso(birthday) or "1990-01-01"
    try:
        p = bday_iso.split('-')
        by, bm, bd = int(p[0]), int(p[1]), int(p[2])
    except Exception:
        by, bm, bd = 1990, 1, 1
    birth_hour = 12
    if birth_time:
        h = _re.search(r'åå(\d+)', birth_time)
        if h: birth_hour = int(h.group(1)) % 12
        h = _re.search(r'åå¾(\d+)', birth_time)
        if h: birth_hour = int(h.group(1)) % 12 + 12
        h = _re.search(r'(\d{1,2}):(\d{2})', birth_time)
        if h: birth_hour = int(h.group(1))
        h2 = _re.search(r'(\d{1,2})æ', birth_time)
        if h2 and birth_hour == 12: birth_hour = int(h2.group(1))
    from datetime import date as _dc
    try: bdo = _dc(by, bm, bd)
    except: bdo = _dc(1990, 1, 1)
    adj_year = by - 1 if (bm == 1 or (bm == 2 and bd < 4)) else by
    personal_star = ((11 - adj_year) % 9) or 9
    life_path = _digit_reduce(by + bm + bd)
    KANA_VAL = {'ã':1,'ã':2,'ã':3,'ã':4,'ã':5,'ã':1,'ã':2,'ã':3,'ã':4,'ã':5,'ã':1,'ã':2,'ã':3,'ã':4,'ã':5,'ã':1,'ã¡':2,'ã¤':3,'ã¦':4,'ã¨':5,'ãª':1,'ã«':2,'ã¬':3,'ã­':4,'ã®':5,'ã¯':1,'ã²':2,'ãµ':3,'ã¸':4,'ã»':5,'ã¾':1,'ã¿':2,'ã':3,'ã':4,'ã':5,'ã':1,'ã':3,'ã':5,'ã':1,'ã':2,'ã':3,'ã':4,'ã':5,'ã':1,'ã':5,'ã':5}
    rn = sum(KANA_VAL.get(c, 0) for c in name_kana)
    name_num = _digit_reduce(rn) if rn else life_path
    sign_starts = [(3,21),(4,20),(5,21),(6,21),(7,23),(8,23),(9,23),(10,23),(11,22),(12,22),(1,20),(2,19)]
    sun_sign = 11
    for i, (sm, sd) in enumerate(sign_starts):
        if bm == sm and bd >= sd: sun_sign = i; break
        nxt = sign_starts[(i+1)%12]
        if bm == nxt[0] and bd < nxt[1]: sun_sign = i; break
    hb = (birth_hour + 1) // 2 % 12
    zwds_base = (by * 12 + bm * 30 + bd + hb) % 9 + 1
    return {"bdo": bdo, "bday_kan": _date_day_kan(bdo),
            "personal_star": personal_star, "life_path": life_path,
            "name_num": name_num, "sun_sign": sun_sign, "zwds_base": zwds_base}

def _calc_scores(bdata, d):
    return {
        "åæ±æ¨å½": _stem_harmony(bdata["bday_kan"], _date_day_kan(d)),
        "ç®å½å­¦": _kyusei_harmony(bdata["personal_star"], _kyusei_daily(d)),
        "è¥¿æ´å æè¡": _western_daily(bdata["sun_sign"], d),
        "æ°ç§è¡": _numerology_daily(bdata["life_path"], bdata["name_num"], d),
        "ç´«å¾®ææ°": _zwds_daily(bdata["zwds_base"], d),
    }

_MSG = {
    "å¨ä½é": [["éãã«éããã®ãå","ç¡çãããä¼é¤ã"],["æéãªè¡åãâ","ç¦ãããã£ããã¨"],["ç©ãããªéæ°ã§ã","å¹³ç©ãªä¸æ¥ã«"],["å¥½èª¿ãªéæ°ï¼ç©æ¥µçã«","è¯ãæµãã«ä¹ã£ã¦"],["çµ¶å¥½èª¿ï¼ãã£ã³ã¹ã","æé«ã®éæ°ã§ã"]],
    "éé": [["æ¯åºã«æ³¨æã","ç¯ç´ãå¿ããã¦"],["è¡åè²·ãã¯æ§ãã¦","æéãªéé­ç®¡çã"],["å®å®ããééã§ã","æ®éã®ä¸æ¥"],["è¨æåå¥ã®åã","ééä¸æä¸­"],["çµ¶å¥½ã®ééï¼å¤§ããª","ãã£ã³ã¹ãæ´»ããã¦"]],
    "ææé": [["ä¸äººã®æéãå¤§åã«","èªåç£¨ãã®æ¥"],["ç´ ç´ãªæ°æã¡ãå¤§åã«","ç¦ãããã£ãã"],["ç©ãããªææé","è¯ãé¢ä¿ãç¶­æ"],["åºä¼ãã®ãã£ã³ã¹ï¼","æ°æã¡ãä¼ããã®ã«â"],["æææé«æ½®ï¼ç©æ¥µçã«","éå½çãªåºä¼ãã"]],
    "ä»äºé": [["å®ãã«å¾¹ãã¦","éè¦ãªæ±ºæ­ã¯é¿ãã¦"],["æéã«é²ãããã¨","ä¸å¯§ãªä»äºã¶ãã"],["ã³ãã³ãç©ã¿ä¸ããæ¥","çå®ãªä»äºãâ"],["ä»äºéå¥½èª¿ï¼ãªã¼ãã¼ã","ææãåºãããæ¥"],["å¤§ããªææãæå¾â","çµ¶å¥½ã®ãã¸ãã¹ãã£ã³ã¹"]],
    "å¥åº·é": [["ç¡çã¯ç¦ç©","ä½ã®ãµã¤ã³ã«ææã«"],["ç¡ç ãååã«","ç²ãããããªããã"],["ä½èª¿ã¯å®å®","ãã©ã³ã¹ãä¿ã¦ãã"],["ã¨ãã«ã®ãã·ã¥ãªæ¥","æ´»åçã«éãããã"],["æé«ã®ã³ã³ãã£ã·ã§ã³ï¼","ä½ãå¿ãçµ¶å¥½èª¿"]],
    "å¯¾äººé": [["éãã«éããã¦","äººæ··ã¿ã¯é¿ãã¦"],["èãå½¹ã«åãã®ãâ","ç¸æã®æ°æã¡ãåªå"],["åæ»ãªã³ãã¥ãã±ã¼ã·ã§ã³","äººéé¢ä¿ã¯å®å®"],["äººèãåºãããã","ç©æ¥µçã«äº¤æµã"],["æé«ã®å¯¾äººéï¼","ç´ æ´ãããåºä¼ãã"]],
}
_LUCKY = {
    "å¨ä½é": [["ä¼æ¯","çæ³"],["æè»ãªçºæ³","éè¦³"],["æ£æ­©","æ¸©ããé£²ã¿ç©"],["ç©æ¥µçãªè¡å","æã®è¨ç»"],["å¤§ããªæ±ºæ­","ç´æãä¿¡ãã¦"]],
    "éé": [["è²¡å¸ãæ´ç","ç¯ç´"],["å®¶è¨ç®¡ç","è²¯è"],["é»è²ãã¢ã¤ãã ","è²¡å¸ã®æ´ç"],["æè³ã»å¯æ¥­","è¨æåå¥ãæ´»ç¨"],["å¤§ããªå¥ç´","ãã¸ãã¹å±é"]],
    "ææé": [["èªå·±çè§£","åé¢ãç£¨ã"],["ãã³ã¯","å¿æ¸©ã¾ãè¨è"],["é","è½ã¡çããå ´æ"],["èµ¤ãã¢ã¤ãã ","ç©æ¥µçãªã¢ãã­ã¼ã"],["èµ¤ã»ãã³ã¯","åç½ã»ãã­ãã¼ãº"]],
    "ä»äºé": [["æ¥­åã®è¦ç´ã","æºå"],["ã¡ã¢ã»ãã¼ã","éä¸­"],["ã³ã¼ãã¼","æ´çæ´é "],["æ°ãã­ã¸ã§ã¯ã","ãã¬ã¼ã³"],["éè¦ãªä¼è­°","å¤§åæ¡ä»¶"]],
    "å¥åº·é": [["ä¼æ¯","æ©å¯"],["ã¹ãã¬ãã","æ°´åè£çµ¦"],["ã¦ã©ã¼ã­ã³ã°","ãã©ã³ã¹é£"],["éå","ã¢ã¦ããã¢"],["ã¹ãã¼ã","ææ¦"]],
    "å¯¾äººé": [["èª­æ¸","åç"],["å¾è´","ç©ãããªè¨è"],["ãç¤¼ã¡ãã»ã¼ã¸","ç¬é¡"],["æ°ããåºä¼ã","äº¤æµä¼"],["ãã¼ãã£ã¼","ç©æ¥µçãªäº¤æµ"]],
}

def gen_daily(user):
    import hashlib as _hs
    from datetime import datetime, date as _dc
    now = datetime.now()
    today = _dc(now.year, now.month, now.day)
    bdata = _parse_bdata(user)
    s = _calc_scores(bdata, today)

    def wt(a, b, c, d, e): return max(1, min(10, round(s["åæ±æ¨å½"]*a + s["ç®å½å­¦"]*b + s["è¥¿æ´å æè¡"]*c + s["æ°ç§è¡"]*d + s["ç´«å¾®ææ°"]*e)))
    cat_sc = {
        "å¨ä½é": wt(0.2, 0.2, 0.2, 0.2, 0.2),
        "éé": wt(0.4, 0.2, 0.1, 0.2, 0.1),
        "ææé": wt(0.1, 0.1, 0.4, 0.2, 0.2),
        "ä»äºé": wt(0.4, 0.3, 0.1, 0.1, 0.1),
        "å¥åº·é": wt(0.2, 0.3, 0.1, 0.1, 0.3),
        "å¯¾äººé": wt(0.1, 0.2, 0.4, 0.2, 0.1),
    }

    def lv(sc): return min(4, max(0, (sc - 1) * 4 // 9))
    def pick(lst, key):
        h = int(_hs.sha256(f"{user.get('birthday','')}|{now.strftime('%Y%m%d')}|{key}".encode()).hexdigest(), 16)
        return lst[h % len(lst)]

    date_str = now.strftime("%Yå¹´%mæ%dæ¥")
    ov = cat_sc["å¨ä½é"]
    om_list = ["ä»æ¥ã¯ãã£ããä¼ãã§ä½ãæ´ãã¾ããã","æéã«ä¸æ­©ãã¤é²ãæ¥ã§ã","ç©ããã§å®å®ããä¸æ¥ã«ãªããã","éæ°ãä¸æä¸­ï¼ç©æ¥µçã«åãã¦","æé«ã®éæ°ãå¤§ããªä¸æ­©ãè¸ã¿åºãã¦"]
    overall_msg = om_list[lv(ov)]
    categories = {}
    for cat in ["å¨ä½é","éé","ææé","ä»äºé","å¥åº·é","å¯¾äººé"]:
        sc = cat_sc[cat]
        v = lv(sc)
        msg = pick(_MSG[cat][v], cat + "_msg")
        lucky_list = _LUCKY.get(cat, [["",""],["" ,""],["" ,""],["" ,""],["" ,""]])[v]
        lucky = pick(lucky_list, cat + "_lucky") if cat not in ("å¥åº·é","å¯¾äººé") else ""
        categories[cat] = {"score": sc, "message": msg, "lucky": lucky}
    return {"date": date_str, "overall_message": overall_msg, "categories": categories}

def gen_monthly(user):
    import hashlib as _hs, calendar as _cal
    from datetime import datetime, date as _dc
    now = datetime.now()
    year, month = now.year, now.month
    bdata = _parse_bdata(user)
    _, last_day = _cal.monthrange(year, month)

    day_avgs = []
    for day in range(1, last_day + 1):
        try:
            ds = _calc_scores(bdata, _dc(year, month, day))
            avg = sum(ds.values()) / len(ds)
            day_avgs.append((day, avg, ds))
        except Exception:
            pass

    def wt(a,b,c,d,e):
        vals = [sum(ds["åæ±æ¨å½"]*a + ds["ç®å½å­¦"]*b + ds["è¥¿æ´å æè¡"]*c + ds["æ°ç§è¡"]*d + ds["ç´«å¾®ææ°"]*e for _, _, ds in day_avgs) / len(day_avgs)]
        return max(1, min(10, round(vals[0])))
    cat_sc = {
        "å¨ä½é": round(sum(v for _,v,_ in day_avgs)/len(day_avgs)),
        "éé": wt(0.4,0.2,0.1,0.2,0.1),
        "ææé": wt(0.1,0.1,0.4,0.2,0.2),
        "ä»äºé": wt(0.4,0.3,0.1,0.1,0.1),
        "å¥åº·é": wt(0.2,0.3,0.1,0.1,0.3),
        "å¯¾äººé": wt(0.1,0.2,0.4,0.2,0.1),
    }
    cat_sc = {k: max(1, min(10, v)) for k, v in cat_sc.items()}

    mid = last_day // 2
    first_half = sum(v for d,v,_ in day_avgs if d <= mid) / max(1, mid)
    second_half = sum(v for d,v,_ in day_avgs if d > mid) / max(1, last_day - mid)
    diff = second_half - first_half
    trend_map = {cat: ("ä¸æ" if diff > 0.3 else "ä¸é" if diff < -0.3 else "å®å®") for cat in cat_sc}
    for cat in ["éé","ææé","ä»äºé","å¥åº·é","å¯¾äººé"]:
        sc = cat_sc[cat]
        if sc >= 7: trend_map[cat] = "ä¸æ" if trend_map["å¨ä½é"] != "ä¸é" else "å®å®"
        elif sc <= 4: trend_map[cat] = "ä¸é" if trend_map["å¨ä½é"] != "ä¸æ" else "å®å®"

    sorted_days = sorted(day_avgs, key=lambda x: -x[1])
    best_days = "ã»".join(str(d) + "æ¥" for d,_,_ in sorted_days[:3])
    caution_days = "ã»".join(str(d) + "æ¥" for d,_,_ in sorted_days[-3:])

    def lv(sc): return min(4, max(0, (sc-1)*4//9))
    def pick(lst, key):
        h = int(_hs.sha256(f"{user.get('birthday','')}|{year}{month:02d}|{key}".encode()).hexdigest(),16)
        return lst[h % len(lst)]

    month_str = now.strftime("%Yå¹´%[æ")
    ov = cat_sc["å¨ä½é"]
    om_list = ["æéã«éããæã§ã","ä¸æ­©ä¸æ­©çå®ã«","ç©ãããªéæ°ã®æ","å¥½èª¿ãªæï¼ç©æ¥µçã«","çµ¶å¥½èª¿ã®æãå¤§ããªææ¦ã"]
    categories = {}
    for cat in ["å¨ä½é","éé","ææé","ä»äºé","å¥åº·é","å¯¾äººé"]:
        sc = cat_sc[cat]
        v = lv(sc)
        msg = pich(_MSG[cat][v], cat + "_monthly")
        categories[cat] = {"score": sc, "trend": trend_map[cat], "message": msg}
    return {
        "month": month_str,
        "overall_message": om_list[lv(ov)],
        "categories": categories,
        "best_days": best_days,
        "caution_days": caution_days,
    }

def gen_divination(user):
    today = datetime.now().strftime("%Yå¹´%[æ%dæ¥")
    ctx = build_user_context(user)
    birthday = user.get("birthday", "")
    prompt = f"""{ctx}
ä»æ¥: {today}

5ã¤ã®å è¡ã§ãã®äººç©ãè¨ºæ­ãã¦JSONå½¢å¼ã§è¿ãã¦ãã ããã
{{
"åæ±æ¨å½": {{"score": 1, "element": "äºè¡å±æ§", "lucky_direction": "åæ¹ä½", "description": "ç¹å¾´50æå­ä»¥å", "current_luck": "ç¾å¨ã®éæ°30æå­ä»¥å"}},
"ç®å½å­¦": {{"score": 1, "star": "ä¸»æå", "description": "ç¹å¾´50æå­ä»¥å", "current_luck": "ç¾å¨ã®éæ°30æå­ä»¥å"}},
"è¥¿æ´å æè¡": {{"score": 1, "sign": "å¤ªé½æåº§å", "planet": "æ¯éæ", "description": "ç¹å¾´50æå­ä»¥å", "current_luck": "ç¾å¨ã®éæ°30æå­ä»¥å"}},
"æ°ç§è¡": {{"score": 1, "life_path": "ã©ã¤ããã¹ãã³ãã¼1-9", "destiny": "éå½æ°1-9", "description": "ç¹å¾´50æå­ä»¥å", "current_luck": "ç¾å¨ã®éæ°30æå­ä»¥å"}},
"ç´«å¾®ææ°": {{"score": 1, "main_star": "ä¸»æå", "description": "ç¹å¾´50æå­ä»¥å", "current_luck": "ç¾å¨ã®éæ°30æå­ä»¥å"}}
}}"""
    return ask_claude(prompt, max_tokens=2500)

def gen_yearly(user):
    current_year = datetime.now().year
    start = current_year - 2
    end = current_year + 10
    ctx = build_user_context(user)
    birthday = user.get("birthday", "")
    prompt = f"""{ctx}

{start}å¹´ãã{end}å¹´ã¾ã§ã®13å¹´éã®éå¢æ¨ç§»ãJSONå½¢å¼ã§è¿ãã¦ãã ããã
{{
"overall_trend": "å¨ä½çãªéæ°ã®æµãï¼50æå­ä»¥åï¼",
"peak_year": 2026,
"caution_year": 2028,
"years": [
{{"year": 2024, "score": 1, "trend": "ä¸æããã¼ã¯ãä¸éãå®å®", "theme": "ãã¼ã12æå­ä»¥å"}}
]
}}"""
    return ask_claude(prompt, max_tokens=2500)

def gen_graph_data(user):
    import hashlib, math
    from datetime import datetime, date as _date
    import re as _re

    birthday = user.get("birthday", "")
    name = user.get("name") or ""
    name_kana = user.get("name_kana") or ""
    birthplace = user.get("birthplace") or ""
    birth_time = user.get("birth_time") or ""

    bday_iso = birthday_to_iso(birthday) or ""
    by, bm, bd_num = 1990, 1, 1
    if bday_iso:
        try:
            parts = bday_iso.split('-')
            by, bm, bd_num = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            pass

    birth_hour = 12
    if birth_time:
        h = _re.search(r'åå(\d+)', birth_time)
        if h: birth_hour = int(h.group(1)) % 12
        h = _re.search(r'åå¾(\d+)', birth_time)
        if h: birth_hour = int(h.group(1)) % 12 + 12
        h = _re.search(r'(\d{1,2}):(\d{2})', birth_time)
        if h: birth_hour = int(h.group(1))
        h2 = _re.search(r'(\d{1,2})æ', birth_time)
        if h2 and birth_hour == 12: birth_hour = int(h2.group(1))

    def digit_reduce(n):
        while n > 9 and n not in (11, 22, 33):
            n = sum(int(c) for c in str(n))
        return n
    life_path = digit_reduce(by + bm + bd_num)
    KANA_VAL = {
        'ã':1,'ã':2,'ã':3,'ã':4,'ã':5,
        'ã':1,'ã':2,'ã':3,'ã':4,'ã':5,
        'ã':1,'ã':2,'ã':3,'ã':4,'ã':5,
        'ã':1,'ã¡':2,'ã¤':3,'ã¦':4,'ã¨':5,
        'ãª':1,'ã«':2,'ã¬':3,'ã­':4,'ã®':5,
        'ã¯':1,'ã²':2,'ãµ':3,'ã¸':4,'ã»':5,
        'ã¾':1,'ã¿':2,'ã':3,'ã':4,'ã':5,
        'ã':1,'ã':3,'ã':5,
        'ã':1,'ã':2,'ã':3,'ã':4,'ã':5,
        'ã':1,'ã':5,'ã':5,
    }
    raw_name_num = sum(KANA_VAL.get(c, 0) for c in name_kana)
    name_num = digit_reduce(raw_name_num) if raw_name_num else life_path

    adj_year = by - 1 if (bm == 1 or (bm == 2 and bd_num < 4)) else by
    kyusei = ((11 - adj_year) % 9) or 9

    try:
        delta = (_date(by, bm, bd_num) - _date(2000, 1, 1)).days
    except Exception:
        delta = 0
    day_kan = ((delta % 10) + 10) % 10
    hour_branch = (birth_hour + 1) // 2 % 12

    sign_starts = [(3,21),(4,20),(5,21),(6,21),(7,23),(8,23),
                   (9,23),(10,23),(11,22),(12,22),(1,20),(2,19)]
    sun_sign = 11
    for i, (sm, sd) in enumerate(sign_starts):
        if bm == sm and bd_num >= sd:
            sun_sign = i; break
        nxt = sign_starts[(i + 1) % 12]
        if bm == nxt[0] and bd_num < nxt[1]:
            sun_sign = i; break

    zwds_base = (by * 12 + bm * 30 + bd_num + hour_branch) % 9 + 1

    base_scores = {
        "åæ±æ¨å½": 5.0 + (day_kan - 4.5) * 0.45,
        "ç®å½å­¦": 5.0 + (kyusei - 5.0) * 0.50,
        "è¥¿æ´å æè¡": 5.0 + math.sin(sun_sign * math.pi / 6.0) * 2.0,
        "æ°ç§è¡": 5.0 + (name_num - 5.0) * 0.35 + (life_path - 5.0) * 0.20,
        "ç´«å¾®ææ°": 5.0 + (zwds_base - 5.0) * 0.50,
    }

    def art_hash(art, tag):
        seed = f"{birthday}|{name}|{name_kana}|{birthplace}|{birth_time}|{art}|{tag}"
        return int(hashlib.sha256(seed.encode()).hexdigest(), 16)

    def wave_score(art, t, tag):
        hv = art_hash(art, tag)
        b = base_scores[art]
        f1 = 1.0 + (hv % 100) / 200.0
        f2 = 2.0 + (hv % 50) / 100.0
        p1 = (hv % 628) / 100.0
        p2 = ((hv >> 8) % 628) / 100.0
        a1 = 1.6 + (hv % 30) / 20.0
        a2 = 0.9 + (hv % 20) / 25.0
        s = b + a1 * math.sin(f1 * t + p1) + a2 * math.sin(f2 * t + p2)
        return max(1.0, min(10.0, s))

    current_year = datetime.now().year
    arts = list(base_scores.keys())
    result = {}
    for art in arts:
        monthly = [round(wave_score(art, (m / 12.0) * 2 * math.pi, "monthly"), 1)
                   for m in range(12)]
        yearly = [round(wave_score(art, (y / 13.0) * 2 * math.pi, "yearly"), 1)
                  for y in range(13)]
        past_yearly = [round(wave_score(art, ((i - 10) / 13.0) * 2 * math.pi, "yearly"), 1)
                       for i in range(13)]
        result[art] = {"monthly": monthly, "yearly": yearly, "past_yearly": past_yearly}
    return result

def get_graph_data_cached(user):
    birthday = user.get("birthday", "")
    name = user.get("name") or ""
    birthplace = user.get("birthplace") or ""
    birth_time = user.get("birth_time") or ""
    cache_key = birthday_to_iso(birthday) + "|" + name + "|" + birthplace + "|" + birth_time
    now = datetime.now()
    if cache_key in graph_cache:
        age_h = (now - graph_cache[cache_key]["cached_at"]).total_seconds() / 3600
        if age_h < 24:
            return graph_cache[cache_key]["data"]
    data = gen_graph_data(user)
    if data:
        graph_cache[cache_key] = {"data": data, "cached_at": now}
    return data

def generate_fortune_image(graph_data, user):
    current_year = datetime.now().year
    current_month = datetime.now().month
    start_year = current_year - 2

    birthday = user.get("birthday", "")
    birthday_iso = birthday_to_iso(birthday) or ""
    bday_disp = iso_to_birthday(birthday_iso) if birthday_iso else birthday

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 11), facecolor='#0c0c22')
    fig.suptitle(f'Hoshiyorudou Fortune ({bday_disp})',
                 color='#c8a8ff', fontsize=11, y=0.99)

    month_labels = ['Jan','Feb','Mar','Apr','May','Jun',
                    'Jul','Aug','Sep','Oct','Nov','Dec']
    charts = [
        (ax1, 'monthly',
         month_labels,
         f'{current_year} Monthly Fortune',
         current_month - 1),
        (ax2, 'yearly',
         [str(start_year + i) for i in range(13)],
         '12-Year Fortune Trend',
         2),
    ]

    for ax, key, labels, title, curr_idx in charts:
        ax.set_facecolor('#10102c')
        ax.set_title(title, color='#a0c8ff', fontsize=10, pad=7)
        ax.set_ylim(1, 10)
        ax.set_yticks(range(1, 11))
        ax.set_yticklabels([str(i) for i in range(1, 11)], fontsize=9)
        ax.tick_params(colors='#8888bb', labelsize=9)
        ax.grid(color='#1e1e44', linewidth=0.7, alpha=0.8)
        for spine in ax.spines.values():
            spine.set_color('#2a2a54')

        for system in SYSTEMS:
            scores = graph_data.get(system, {}).get(key, [])
            if scores:
                ax.plot(scores,
                        color=COLORS[system],
                        linewidth=2.2,
                        marker='o',
                        markersize=3.5,
                        label=SYSTEM_EN[system],
                        alpha=0.92)

        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(
            labels, fontsize=8, color='#9999cc',
            rotation=40 if key == 'yearly' else 0,
            ha='right' if key == 'yearly' else 'center',
        )
        ax.axvline(x=curr_idx, color='#ffffff', alpha=0.2,
                   linestyle='--', linewidth=1)
        ax.legend(loc='upper right', fontsize=7.5,
                  facecolor='#1c1c3c', labelcolor='#ddddff',
                  framealpha=0.9, edgecolor='#3a3a64',
                  handlelength=1.5, handletextpad=0.5)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.subplots_adjust(hspace=0.4)
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=150,
                bbox_inches='tight', facecolor='#0c0c22')
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def generate_past_fortune_image(graph_data, user):
    current_year = datetime.now().year
    start_year = current_year - 12

    birthday = user.get("birthday", "")
    birthday_iso = birthday_to_iso(birthday) or ""
    bday_disp = iso_to_birthday(birthday_iso) if birthday_iso else birthday

    fig, ax = plt.subplots(1, 1, figsize=(10, 5.5), facecolor='#0c0c22')
    fig.suptitle(f'Hoshiyorudou Past 12-Year Trend ({bday_disp})',
                 color='#c8a8ff', fontsize=11, y=0.99)

    ax.set_facecolor('#10102c')
    ax.set_title('Past 12-Year Fortune Trend', color='#a0c8ff', fontsize=10, pad=7)
    ax.set_ylim(1, 10)
    ax.set_yticks(range(1, 11))
    ax.set_yticklabels([str(i) for i in range(1, 11)], fontsize=9)
    ax.tick_params(colors='#8888bb', labelsize=9)
    ax.grid(color='#1e1e44', linewidth=0.7, alpha=0.8)
    for spine in ax.spines.values():
        spine.set_color('#2a2a54')

    target_systems = ["åæ±æ¨å½", "ç®å½å­¦", "è¥¿æ´å æè¡", "æ°ç§è¡"]
    for system in target_systems:
        scores = graph_data.get(system, {}).get("past_yearly", [])
        if scores:
            ax.plot(scores,
                    color=COLORS[system],
                    linewidth=2.2,
                    marker='o',
                    markersize=3.5,
                    label=SYSTEM_EN[system],
                    alpha=0.92)

    labels = [str(start_year + i) for i in range(13)]
    ax.set_xticks(range(13))
    ax.set_xticklabels(labels, fontsize=8, color='#9999cc', rotation=40, ha='right')
    ax.axvline(x=12, color='#ffffff', alpha=0.4, linestyle='--', linewidth=1.5)
    ax.legend(loc='upper right', fontsize=7.5,
              facecolor='#1c1c3c', labelcolor='#ddddff',
              framealpha=0.9, edgecolor='#3a3a64',
              handlelength=1.5, handletextpad=0.5)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=150,
                bbox_inches='tight', facecolor='#0c0c22')
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def fmt_daily(data):
    if not data:
        return "â ï¸ éå¢ã®è¨ç®ã«å¤±æãã¾ãããããä¸åº¦ãè©¦ããã ããã"
    lines = [f"ð {data.get('date','ä»æ¥')}ã®éå¢",
             f"ð {data.get('overall_message','')}",
             "ââââââââââââââââââ"]
    for cat, emoji in CAT_EMOJI.items():
        d = data.get("categories", {}).get(cat, {})
        score = d.get("score", 5)
        lines.append(f"  {cat} {score}/10")
        lines.append(f"  {d.get('message','')}")
        if d.get("lucky"):
            lines.append(f"  â {d['lucky']}")
    return "\n".join(lines)

def fmt_monthly(data):
    if not data:
        return "â ï¸ ä»æã®éå¢ã®è¨ç®ã«å¤±æãã¾ããã"
    trend_icon = {"ä¸æ": "â", "å®å®": "â", "ä¸é": "â"}
    lines = [f"ð {data.get('month','ä»æ')}ã®éå¢",
             f"ð {data.get('overall_message','')}",
             "ââââââââââââââââââ"]
    for cat, emoji in CAT_EMOJI.items():
        d = data.get("categories", {}).get(cat, {})
        score = d.get("score", 5)
        trend = d.get("trend", "å®å®")
        lines.append(f"  {cat} {score}/10 {trend_icon.get(trend,'â')}")
        lines.append(f"  {d.get('message','')}")
    lines += ["ââââââââââââââââââ",
              f"åæ¥ï¼{data.get('best_days','-')}",
              f"â ï¸ æ³¨ææ¥ï¼{data.get('caution_days','-')}"]
    return "\n".join(lines)

def fmt_divination(data):
    if not data:
        return "â ï¸ å è¡è¨ºæ­ã®è¨ç®ã«å¤±æãã¾ããã"
    sys_emoji = {"åæ±æ¨å½": "â¯ï¸", "ç®å½å­¦": "ð",
                 "è¥¿æ´å æè¡": "â", "æ°ç§è¡": "ð¢", "ç´«å¾®ææ°": "ð"}
    lines = ["ð® å è¡å¥ ç·åè¨ºæ­ ð®", "ââââââââââââââââââ"]
    for sys_name, emoji in sys_emoji.items():
        d = data.get(sys_name, {})
        score = d.get("score", 5)
        lines.append(f"{emoji} ã{sys_name}ã {score_bar(score)} {score}/10")
        if sys_name == "åæ±æ¨å½":
            lines.append(f"  äºè¡: {d.get('element','-')} åæ¹ä½: {d.get('lucky_direction','-')}")
        elif sys_name == "ç®å½å­¦":
            lines.append(f"  ä¸»æ: {d.get('star','-')}")
        elif sys_name == "è¥¿æ´å æè¡":
            lines.append(f"  {d.get('sign','-')} æ¯éæ: {d.get('planet','-')}")
        elif sys_name == "æ°ç§è¡":
            lines.append(f"  ã©ã¤ããã¹: {d.get('life_path','-')} éå½æ°: {d.get('destiny','-')}")
        elif sys_name == "ç´«å¾®ææ°":
            lines.append(f"  ä¸»æ: {d.get('main_star','-')}")
        lines.append(f"  {d.get('description','')}")
        lines.append(f"  â¶ {d.get('current_luck','')}")
        lines.append("")
    return "\n".join(lines).rstrip()

def fmt_yearly(data):
    if not data:
        return "â ï¸ å¹´éæ¨ç§»ã®è¨ç®ã«å¤±æãã¾ããã"
    current_year = datetime.now().year
    trend_sym = {"ä¸æ": "â", "ãã¼ã¯": "ð", "ä¸é": "â", "å®å®": "â"}
    lines = ["ð 12å¹´éã®éå¢æ¨ç§» ð",
             f"â¨ {data.get('overall_trend','')}",
             "ââââââââââââââââââ",
             "å¹´  ãã¼      ç¹ å¾å ãã¼ã",
             "ââââââââââââââââââ"]
    for yd in data.get("years", []):
        year = yd.get("year", "")
        score = yd.get("score", 5)
        trend = yd.get("trend", "å®å®")
        theme = yd.get("theme", "")
        now_mark = "âä»" if year == current_year else "   "
        lines.append(
            f"{year} [{block_bar(score)}] {score:2d} {trend_sym.get(trend,'â')} {theme} {now_mark}")
    lines += ["ââââââââââââââââââ",
              f"ð æé«ã®å¹´ï¼{data.get('peak_year','-')}å¹´",
              f"â ï¸ æ³¨æã®å¹´ï¼{data.get('caution_year','-')}å¹´"]
    return "\n".join(lines)

def fortune_thread(user_id, user, fortune_type):
    try:
        birthday = user.get("birthday", "")
        if fortune_type == "daily":
            push(user_id, fmt_daily(gen_daily(user)))
        elif fortune_type == "monthly":
            push(user_id, fmt_monthly(gen_monthly(user)))
        elif fortune_type == "divination":
            push(user_id, fmt_divination(gen_divination(user)))
        elif fortune_type == "yearly":
            push(user_id, fmt_yearly(gen_yearly(user)))
    except Exception as e:
        push(user_id, f"â ï¸ ã¨ã©ã¼ãçºçãã¾ãããããä¸åº¦ãè©¦ããã ããã\n({e})")

def graph_image_thread(user_id, user):
    try:
        birthday_iso = birthday_to_iso(user.get("birthday", ""))
        data = get_graph_data_cached(user)
        if not data:
            push(user_id, "â ï¸ ã°ã©ããã¼ã¿ã®çæã«å¤±æãã¾ãããããä¸åº¦ãè©¦ããã ããã")
            return

        img_bytes = generate_fortune_image(data, user)
        img_id = uuid.uuid4().hex
        store_image(img_id, img_bytes)

        base = bot_base_url()
        if not base:
            push(user_id, "â ï¸ ãµã¼ãã¼URLãåå¾ã§ãã¾ããã§ããã")
            return

        img_url = f"{base}/img/{img_id}"
        push_image(user_id, img_url)

        legend = (
            "ð ã°ã©ãã®è²ã®å¡ä¾\n"
            + LEGEND_TEXT + "\n\n"
            "ð¸ ã¹ã¯ãªã¼ã³ã·ã§ããã§ä¿å­ã§ãã¾ãã\n"
            "â»ãã¼ã¿ã¯24æéã­ã£ãã·ã¥ããã¾ãã"
        )
        push(user_id, legend, with_menu=True)

    except Exception as e:
        push(user_id, f"â ï¸ ã°ã©ãã®çæã«å¤±æãã¾ããã\n({e})")


def past_graph_image_thread(user_id, user):
    try:
        data = get_graph_data_cached(user)
        if not data:
            push(user_id, "â ï¸ ã°ã©ããã¼ã¿ã®çæã«å¤±æãã¾ãããããä¸åº¦ãè©¦ããã ããã")
            return

        img_bytes = generate_past_fortune_image(data, user)
        img_id = uuid.uuid4().hex
        store_image(img_id, img_bytes)

        base = bot_base_url()
        if not base:
            push(user_id, "â ï¸ ãµã¼ãã¼URLãåå¾ã§ãã¾ããã§ããã")
            return

        img_url = f"{base}/img/{img_id}"
        push_image(user_id, img_url)
        push(user_id,
             "ð éå»12å¹´ã®éå¢æ¨ç§»ã§ãã\n\nå®éã«è¯ãã£ãå¹´ã»å¤§å¤ã ã£ãå¹´ã¨ãã©ã®å è¡ã®å±±è°·ãä¸è´ãã¦ãããç¢ºèªãã¦ã¿ã¦ãã ããã\nä¸çªä¸è´ãã¦ããå è¡ãããªãã¨ã®ç¸æ§ãé«ãå è¡ã§ãâ¨",
             with_menu=True)

    except Exception as e:
        push(user_id, f"â ï¸ ã°ã©ãã®çæã«å¤±æãã¾ããã\n({e})")


WELCOME_TEXT = """ð æå¤å ã¸ãããã â¨

æå¤å ã¯ãè¤æ°ã®å è¡ãçµã¿åããã
æ¬æ ¼çãªå ããµã¼ãã¹ã§ãã

ãã§ãããã¨ã
ð $ï¿½ï¿½æ¥ã®éå¢
å¨ä½éã»ééã»ææéã»ä»äºéã»
å¥åº·éã»å¯¾äººéã®6ã«ãã´ãªã
ã¹ã³ã¢ä»ãä¸è¦§è¡¨ç¤º

ð ä»æã®éå¢
ã«ãã´ãªå¥ã¹ã³ã¢ï¼ä¸æ/å®å®/ä¸éã®
ãã«ã³ãã¨åæ¥ã»æ³¨ææ¥ããç¥ãã

ð® å è¡å¥è¨ºæ­
åæ±æ¨å½ã»ç®å½å­¦ã»è¥¿æ´å æè¡ã»
æ°ç§è¡ã»ç´«å¾®ææ°ã®5å è¡ã®çµæã
ã¹ã³ã¢ä»ãã§ä¸è¦§ã§ãã¾ã

ð ä»å¹´/12å¹´æ¨ç§»ã°ã©ã
5å è¡ã®å¨ä½éãæãç·ã°ã©ãç»åã§
ãã£ããã«ç´æ¥éä¿¡ãã¾ã"""

REGISTRATION_PROMPT = """ð ã¾ããä»¥ä¸ãæãã¦ãã ããã

ð 'ï¿½ï¿½å¹´ææ¥ï¼åããã°æå»ãï¼
ð¤ ååã¨èª­ã¿æ¹ï¼å¹³ä»®åï¼ â»æ°ç§è¡ã®ç²¾åº¦åä¸
ð åºçå° â»ç²¾åº¦åä¸

å¥åä¾ï¼
1990å¹´3æ15æ¥ åå10æ
ç°ä¸­å¤ªéï¼ããªããããï¼ æ±äº¬é½"""

@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    set_user(user_id, {"state": "waiting_diagnosis", "birthday": None, "name": None, "birthplace": None, "birth_time": None, "diagnosis_done": False})
    LIFF_URL = "https://liff.line.me/2010080648-3clhj7zs"
    combined = (
        WELCOME_TEXT +
        "\n\n━━━━━━━━━━━━━━━━━━\n\n"
        "📝 まず、あなたのことを教えてください！\n"
        "以下のリンクから簡単な診断（約5〜7分）を受けると、"
        "あなただけにカスタマイズされた占いが届くようになります✨\n\n"
        f"🔮 診断はこちら\n{LIFF_URL}"
    )
    reply_msg(event.reply_token, combined)
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user = get_user(user_id)
    text = event.message.text.strip()

    if text == "èªçæ¥å¤æ´":
        user["state"] = "waiting_birthday"
        set_user(user_id, user)
        reply_msg(event.reply_token, "æ°ããçå¹´ææ¥ãå¥åãã¦ãã ããã\nï¼ä¾: 1990å¹´3æ15æ¥ï¼")
        return

    if user.get("state") == "waiting_birthday" or not user.get("birthday"):
        birthday = parse_birthday(text)
        if birthday:
            user["birthday"] = birthday
            user["state"] = "menu"
            bt = parse_birth_time(text)
            if bt:
                user["birth_time"] = bt
            extra = parse_extra_info(text)
            if extra.get("name"):
                user["name"] = extra["name"]
            if extra.get("birthplace"):
                user["birthplace"] = extra["birthplace"]
            set_user(user_id, user) # Redisã«æ°¸ç¶å
            detail = ""
            if user.get("birth_time"): detail += f" {user['birth_time']}"
            if user.get("name"): detail += f"\nð¤ {user['name']}"
            if user.get("birthplace"): detail += f"\nð {user['birthplace']}"
            reply_msg(event.reply_token,
                      f"â¨ {birthday}{detail}\n\nã§ç»é²ãã¾ããï¼\nã¡ãã¥ã¼ãããé¸ã³ãã ããã",
                      with_menu=True)
        else:
            reply_msg(event.reply_token,
                      "çå¹´ææ¥ã®å½¢å¼ãèªè­ã§ãã¾ããã§ããã\n\nä»¥ä¸ã®å½¢å¼ã§ãå¥åãã ããï¼\nã»1990å¹´3æ15æ¥\nã»1990/3/15\nã»1990-3-15")
        return

    birthday = user["birthday"]

    loading_msgs = {
        "ä»æ¥ã®éå¢": "ð ä»æ¥ã®éå¢ãå ãä¸­ã§ã...\nãã°ãããå¾ã¡ãã ãã ð",
        "ä»æã®éå¢": "ð ä»æã®éå¢ãè¨ç®ä¸­ã§ã...\nãã°ãããå¾ã¡ãã ãã ð",
        "å è¡å¥è¨ºæ­": "ð® 5ã¤ã®å è¡ã§è¨ºæ­ä¸­ã§ã...\nãã°ãããå¾ã¡ãã ãã â¨",
        "ä»å¹´/12å¹´æ¨ç§»ã°ã©ã": "ð 12å¹´éã®éå¢æ¨ç§»ãè¨ç®ä¸­ã§ã...\nãã°ãããå¾ã¡ãã ãã ð",
    }
    fortune_map = {
        "ä»æ¥ã®éå¢": "daily",
        "ä»æã®éå¢": "monthly",
        "å è¡å¥è¨ºæ­": "divination",
    }

    if text in ("éå»12å¹´", "éå»ã®éå¢", "ç¸æ§è¨ºæ­"):
        reply_msg(event.reply_token,
                  "ð éå»12å¹´ã®æãç·ã°ã©ããçæä¸­ã§ã...\nãã°ãããå¾ã¡ãã ãã ð")
        threading.Thread(
            target=past_graph_image_thread,
            args=(user_id, user),
            daemon=True,
        ).start()
        return

    if text == "ä»å¹´/12å¹´æ¨ç§»ã°ã©ã":
        reply_msg(event.reply_token,
                  "ð æãç·ã°ã©ããçæä¸­ã§ã...\nãã°ãããå¾ã¡ãã ãã ð\nï¼ååã¯20ã30ç§ãããã¾ãï¼")
        threading.Thread(
            target=graph_image_thread,
            args=(user_id, user),
            daemon=True,
        ).start()
        return

    if text in fortune_map:
        reply_msg(event.reply_token, loading_msgs[text])
        threading.Thread(
            target=fortune_thread,
            args=(user_id, user, fortune_map[text]),
            daemon=True,
        ).start()
    else:
        try:
            resp = claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system="""ããªãã¯ãæå¤å ï¼ãããã©ãï¼ãã®å ãå¸«AIã§ãã
åæ±æ¨å½ã»ç®å½å­¦ã»å æè¡ã»æ°ç§è¡ã»ç´«å¾®ææ°ãå°éã¨ããç¥ç§çãªå ããã©ã³ãã§ãã
ã»ä¸å¯§ã§ç¥ç§çãªå£èª¿ï¼ããã§ãããã¾ãããããã¨å­ãã¾ããï¼
ã»æã»æã»å¤ãã¤ã¡ã¼ã¸ããè¨èãèªç¶ã«ä½¿ã
ã»ç¸æã®æ°æã¡ã«å¯ãæ·»ãååããªã¡ãã»ã¼ã¸ãä¼ãã
è¿ç­ã¯200æå­ä»¥åã§ã""",
                messages=[{"role": "user", "content": text}],
            )
            reply_text = resp.content[0].text
        except Exception:
            reply_text = "ç³ãè¨³ãããã¾ãããåªä»ãæã®å°ããä¹±ãã¦ããã¾ãããã°ãããå¾ã¡ãã ããã¾ããð"
        reply_msg(event.reply_token, reply_text, with_menu=True)

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@app.route("/img/<img_id>")
def serve_image(img_id):
    if img_id in image_cache:
        resp = make_response(image_cache[img_id])
        resp.headers["Content-Type"] = "image/png"
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    abort(404)

@app.route("/", methods=["GET"])
def health_check():
    return "æå¤å  LINE Bot is running â¨"


@app.route('/liff')
def serve_liff():
    liff_path = os.path.join(os.path.dirname(__file__), 'liff_onboarding.html')
    try:
        with open(liff_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return content, 200, {'Content-Type': 'text/html; charset=utf-8'}
    except FileNotFoundError:
        return "LIFF page not found", 404

@app.route('/api/liff-result', methods=['POST'])
def liff_result():
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400
    line_user_id = data.get('line_user_id')
    if not line_user_id:
        return jsonify({"error": "no user id"}), 400
    profile = data.get('profile', {})
    tags = data.get('tags', {})
    user = get_user(line_user_id) or {}
    user.update({
        'state': 'registered',
        'name': profile.get('name'),
        'birthday': profile.get('birthday'),
        'birth_time': profile.get('birthtime'),
        'birthplace': profile.get('birthplace'),
        'diagnosis_tags': tags,
        'diagnosis_done': True
    })
    set_user(line_user_id, user)
    try:
        name = profile.get('name', '')
        msg = f"✨ {name}さん、診断が完了しました！\n\n今日から、あなただけにカスタマイズされた占いをお届けします🌙\n\n「今日の運勢」を送ってみてください📅"
        line_bot_api.push_message(line_user_id, TextSendMessage(text=msg))
    except Exception as e:
        print(f"Push error: {e}")
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
