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

# ── Redis helper (persistent storage) ──────────────────────────────
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
# ────────────────────────────────────────────────────────────────────
graph_cache = {}
image_cache = {}
image_cache_order = deque(maxlen=60)
MAX_IMAGES = 60

SYSTEMS = ["四柱推命", "算命学", "西洋占星術", "数秘術", "紫微斗数"]
COLORS = {
    "四柱推命": "#4FC3F7",
    "算命学": "#FFD54F",
    "西洋占星術": "#FF7043",
    "数秘術": "#66BB6A",
    "紫微斗数": "#AB47BC",
}
SYSTEM_EN = {
    "四柱推命": "4Pillars",
    "算命学": "9-Star",
    "西洋占星術": "Western",
    "数秘術": "Numerol.",
    "紫微斗数": "ZWDS",
}
LEGEND_TEXT = (
    "━" * 14 + "\n"
    "\U0001F7E6 4Pillars = 四柱推命\n"
    "\U0001F7E1 9-Star = 算命学\n"
    "\U0001F534 Western = 西洋占星術\n"
    "\U0001F7E2 Numerol. = 数秘術\n"
    "\U0001F7E3 ZWDS = 紫微斗数"
)
CAT_EMOJI = {
    "全体運": "🌟",
    "金運": "💰",
    "恋愛運": "💕",
    "仕事運": "💼",
    "健康運": "💪",
    "対人運": "🤝",
}

def parse_birthday(text):
    import re as _re
    patterns = [
        r'(\d{4})[年/\-.]*(\d{1,2})[月/\-.]*(\d{1,2})',
        r'(\d{2})[年/\-.]*(\d{1,2})[月/\-.]*(\d{1,2})',
    ]
    for p in patterns:
        m = _re.search(p, text)
        if m:
            year = int(m.group(1))
            if year < 100:
                year += 1900
            try:
                return datetime(year, int(m.group(2)), int(m.group(3))).strftime("%Y年%m月%d日")
            except Exception:
                pass
    return None

def parse_birth_time(text):
    import re as _re
    m = _re.search(r'午前\s*(\d{1,2})時(?:\s*(\d{1,2})分)?', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2) or 0)
        return "午前" + str(h) + "時" + (str(mn) + "分" if mn else "")
    m = _re.search(r'午後\s*(\d{1,2})時(?:\s*(\d{1,2})分)?', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2) or 0)
        return "午後" + str(h) + "時" + (str(mn) + "分" if mn else "")
    m = _re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if m:
        return str(int(m.group(1))) + "時" + str(int(m.group(2))) + "分"
    return None

def parse_extra_info(text):
    import re as _re
    result = {}
    cleaned = _re.sub(r'\d{2,4}[年/\-.]+\d{1,2}[月/\-.]+\d{1,2}日?', '', text)
    cleaned = _re.sub(r'午前|午後|\d{1,2}時\d*分?|\d{1,2}:\d{2}', '', cleaned)
    cleaned = _re.sub(r'[\s　]+', ' ', cleaned).strip()
    kana_paren = _re.search(r'[（(]([぀-ゟー]{2,})[）)]', cleaned)
    if kana_paren:
        result["name_kana"] = kana_paren.group(1)
        cleaned = cleaned.replace(kana_paren.group(0), '').strip()
    bp = _re.search(r'[぀-鿿゠-ヿ]+[都道府県市区町村]', cleaned)
    if bp:
        result["birthplace"] = bp.group(0)
        cleaned = cleaned.replace(bp.group(0), '').strip()
    nm = _re.search(r'[一-鿿゠-ヿ][぀-鿿゠-ヿ]{1,7}', cleaned)
    if nm:
        result["name"] = nm.group(0)
    if "name_kana" not in result:
        kana_only = _re.search(r'^[぀-ゟー]{2,}$', cleaned.strip())
        if kana_only:
            result["name_kana"] = kana_only.group(0)
    return result

def build_user_context(user):
    bd = user.get("birthday", "")
    bt = user.get("birth_time")
    nm = user.get("name")
    nk = user.get("name_kana")
    bp = user.get("birthplace")
    lines = ["生年月日: " + bd + (" " + bt if bt else "")]
    if nm:
        lines.append("名前: " + nm + ("（" + nk + "）" if nk else ""))
    if bp:
        lines.append("出生地: " + bp)
    return "\n".join(lines)

def birthday_to_iso(bday):
    try:
        return datetime.strptime(bday, "%Y年%m月%d日").strftime("%Y-%m-%d")
    except Exception:
        return bday

def iso_to_birthday(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%Y-%m-%d").strftime("%Y年%m月%d日")
    except Exception:
        return iso

def bot_base_url():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    return f"https://{domain}" if domain else ""

def score_bar(score):
    filled = max(0, min(5, round(score / 10 * 5)))
    return "⭐" * filled + "☆" * (5 - filled)

def block_bar(score):
    filled = max(0, min(5, round(score / 2)))
    return "█" * filled + "░" * (5 - filled)

def main_menu_qr():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📅 今日の運勢", text="今日の運勢")),
        QuickReplyItem(action=MessageAction(label="📆 今月の運勢", text="今月の運勢")),
        QuickReplyItem(action=MessageAction(label="🔮 占術別診断", text="占術別診断")),
        QuickReplyItem(action=MessageAction(label="📊 今年/12年推移グラフ", text="今年/12年推移グラフ")),
        QuickReplyItem(action=MessageAction(label="📈 過去12年の運勢", text="過去12年")),
        QuickReplyItem(action=MessageAction(label="✏️ 誕生日変更", text="誕生日変更")),
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
        system="あなたは占い師AIです。指定されたJSON形式のみを返してください。説明文・マークダウン不要。",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        return json.loads(m.group())
    return None

# ── 占術計算ヘルパー ─────────────────────────────────────────────

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
    if be == te: return 6
    if (be, te) in gen: return 9
    if (te, be) in gen: return 7
    if (be, te) in ctrl: return 3
    if (te, be) in ctrl: return 1
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
    return {0:8, 1:5, 2:7, 3:4, 4:2, 5:3, 6:5, 7:7, 8:4}.get(diff, 5)

def _western_daily(sun_sign, d):
    from datetime import date as _dc
    days = (_dc(d.year, d.month, d.day) - _dc(2000, 1, 1)).days
    moon_sign = days % 12
    diff = (moon_sign - sun_sign) % 12
    return {0:9,1:5,2:7,3:3,4:8,5:5,6:2,7:3,8:8,9:5,10:6,11:4}.get(diff, 5)

def _numerology_daily(life_path, name_num, d):
    pd = _digit_reduce(d.year + d.month + d.day)
    lp_m = life_path % 9 or 9
    pd_m = pd % 9 or 9
    diff = abs(lp_m - pd_m)
    base = {0:9,1:6,2:5,3:8,4:2,5:3,6:8,7:5,8:6}.get(diff % 9, 5)
    nd = abs((name_num % 9 or 9) - pd_m)
    return min(10, base + (1 if nd in (0, 3, 6) else 0))

def _zwds_daily(zwds_base, d):
    combined = ((d.month + zwds_base - 2) % 12 + d.day % 12) % 12
    return {0:5,1:8,2:2,3:6,4:9,5:2,6:5,7:8,8:1,9:6,10:8,11:3}.get(combined, 5)

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
        h = _re.search(r'午前(\d+)', birth_time)
        if h: birth_hour = int(h.group(1)) % 12
        h = _re.search(r'午後(\d+)', birth_time)
        if h: birth_hour = int(h.group(1)) % 12 + 12
        h = _re.search(r'(\d{1,2}):(\d{2})', birth_time)
        if h: birth_hour = int(h.group(1))
        h2 = _re.search(r'(\d{1,2})時', birth_time)
        if h2 and birth_hour == 12: birth_hour = int(h2.group(1))
    from datetime import date as _dc
    try: bdo = _dc(by, bm, bd)
    except: bdo = _dc(1990, 1, 1)
    adj_year = by - 1 if (bm == 1 or (bm == 2 and bd < 4)) else by
    personal_star = ((11 - adj_year) % 9) or 9
    life_path = _digit_reduce(by + bm + bd)
    KANA_VAL = {'あ':1,'い':2,'う':3,'え':4,'お':5,'か':1,'き':2,'く':3,'け':4,'こ':5,'さ':1,'し':2,'す':3,'せ':4,'そ':5,'た':1,'ち':2,'つ':3,'て':4,'と':5,'な':1,'に':2,'ぬ':3,'ね':4,'の':5,'は':1,'ひ':2,'ふ':3,'へ':4,'ほ':5,'ま':1,'み':2,'む':3,'め':4,'も':5,'や':1,'ゆ':3,'よ':5,'ら':1,'り':2,'る':3,'れ':4,'ろ':5,'わ':1,'を':5,'ん':5}
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
        "四柱推命": _stem_harmony(bdata["bday_kan"], _date_day_kan(d)),
        "算命学": _kyusei_harmony(bdata["personal_star"], _kyusei_daily(d)),
        "西洋占星術": _western_daily(bdata["sun_sign"], d),
        "数秘術": _numerology_daily(bdata["life_path"], bdata["name_num"], d),
        "紫微斗数": _zwds_daily(bdata["zwds_base"], d),
    }

_MSG = {
    "全体運": [["静かに過ごすのが吉","無理をせず休養を"],["慎重な行動が◎","焦らずゆっくりと"],["穏やかな運気です","平穏な一日に"],["好調な運気！積極的に","良い流れに乗って"],["絶好調！チャンスを","最高の運気です"]],
    "金運": [["支出に注意を","節約を心がけて"],["衝動買いは控えて","慎重な金銭管理を"],["安定した金運です","普通の一日"],["臨時収入の兆し","金運上昇中"],["絶好の金運！大きな","チャンスを活かして"]],
    "恋愛運": [["一人の時間を大切に","自分磨きの日"],["素直な気持ちを大切に","焦らずゆっくり"],["穏やかな恋愛運","良い関係を維持"],["出会いのチャンス！","気持ちを伝えるのに◎"],["恋愛最高潮！積極的に","運命的な出会いも"]],
    "仕事運": [["守りに徹して","重要な決断は避けて"],["慎重に進めること","丁寧な仕事ぶりを"],["コツコツ積み上げる日","着実な仕事が◎"],["仕事運好調！リーダーを","成果が出やすい日"],["大きな成果が期待◎","絶好のビジネスチャンス"]],
    "健康運": [["無理は禁物","体のサインに敏感に"],["睡眠を十分に","疲れをためないよう"],["体調は安定","バランスを保てそう"],["エネルギッシュな日","活動的に過ごせそう"],["最高のコンディション！","体も心も絶好調"]],
    "対人運": [["静かに過ごして","人混みは避けて"],["聞き役に回るのが◎","相手の気持ちを優先"],["円滑なコミュニケーション","人間関係は安定"],["人脈が広がりそう","積極的に交流を"],["最高の対人運！","素晴らしい出会いも"]],
}
_LUCKY = {
    "全体運": [["休息","瞑想"],["柔軟な発想","静観"],["散歩","温かい飲み物"],["積極的な行動","旅の計画"],["大きな決断","直感を信じて"]],
    "金運": [["財布を整理","節約"],["家計管理","貯蓄"],["黄色いアイテム","財布の整理"],["投資・副業","臨時収入を活用"],["大きな契約","ビジネス展開"]],
    "恋愛運": [["自己理解","内面を磨く"],["ピンク","心温まる言葉"],["青","落ち着いた場所"],["赤いアイテム","積極的なアプローチ"],["赤・ピンク","告白・プロポーズ"]],
    "仕事運": [["業務の見直し","準備"],["メモ・ノート","集中"],["コーヒー","整理整頓"],["新プロジェクト","プレゼン"],["重要な会議","大型案件"]],
    "健康運": [["休息","早寝"],["ストレッチ","水分補給"],["ウォーキング","バランス食"],["運動","アウトドア"],["スポーツ","挑戦"]],
    "対人運": [["読書","内省"],["傾聴","穏やかな言葉"],["お礼メッセージ","笑顔"],["新しい出会い","交流会"],["パーティー","積極的な交流"]],
}

_PRIORITY_MAP = {
    "career":"キャリア・仕事","love":"恋愛・パートナー","wealth":"お金・経済的自由",
    "health":"健康","family":"家族・家庭","creative":"創作・自己表現","spiritual":"精神的成長",
}
_ADVICE_MAP = {
    "action":"具体的な行動指針を求めている","caution":"リスク・注意点を知りたい",
    "confirmation":"自分の選択の後押しが欲しい","self_insight":"自己理解を深めたい",
}
_YEAR_THEME_MAP = {
    "leap":"大きな変化・飛躍の年","consolidation":"安定・基盤固めの年",
    "growth":"自己成長の年","healing":"癒し・回復の年",
}
_MOOD_MAP = {
    "high":"充実・エネルギー高め","neutral":"普通","low":"疲れ気味・停滞感",
    "expansive":"拡張期","stable":"安定期","developmental":"成長期","restorative":"回復期",
}

def _build_persona_summary(tags):
    if not tags:
        return ""
    parts = []
    if tags.get("priority") in _PRIORITY_MAP:
        parts.append("最優先事項: " + _PRIORITY_MAP[tags["priority"]])
    if tags.get("priority2") in _PRIORITY_MAP:
        parts.append("次の優先: " + _PRIORITY_MAP[tags["priority2"]])
    if tags.get("advice_style") in _ADVICE_MAP:
        parts.append("占いへの期待: " + _ADVICE_MAP[tags["advice_style"]])
    if tags.get("year_theme") in _YEAR_THEME_MAP:
        parts.append("今年のテーマ: " + _YEAR_THEME_MAP[tags["year_theme"]])
    m = tags.get("mood") or tags.get("vitality")
    if m in _MOOD_MAP:
        parts.append("現在の状態: " + _MOOD_MAP[m])
    _cm = {"money":"収入アップ・副業中","career":"転職・キャリアチェンジ中","love":"新しい恋愛・結婚を求めている","health":"健康改善中"}
    if tags.get("challenge") in _cm:
        parts.append("現在の挑戦: " + _cm[tags["challenge"]])
    _lm = {"stable_partner":"パートナーがいて安定","challenging_partner":"パートナーとの課題あり","seeking":"恋愛を積極的に求めている","single_focused":"今は恋愛以外を優先"}
    if tags.get("love_status") in _lm:
        parts.append("恋愛状況: " + _lm[tags["love_status"]])
    _lcm = {"red_orange":"赤・オレンジ","blue":"青・紺","yellow_gold":"黄・金","green":"緑"}
    if tags.get("lucky_color") in _lcm:
        parts.append("好きな色: " + _lcm[tags["lucky_color"]])
    _lam = {"physical":"体を動かす","creative":"創作活動","social":"人と交流","intellectual":"読書・学習"}
    if tags.get("lucky_action") in _lam:
        parts.append("気分が上がる行動: " + _lam[tags["lucky_action"]])
    return "\n".join(parts)

_CAT_PRIMARY_SYS = {
    "金運": "四柱推命", "恋愛運": "西洋占星術",
    "仕事運": "算命学", "健康運": "紫微斗数", "対人運": "西洋占星術",
}

def _gen_personalized_text(user, cat_sc, sys_scores, date_label, mode):
    tags = user.get("diagnosis_tags") or {}
    name = user.get("name") or "あなた"
    persona = _build_persona_summary(tags)
    birthday = user.get("birthday", "")
    today_key = datetime.now().strftime("%Y%m%d") if mode == "daily" else datetime.now().strftime("%Y%m")
    cache_key = f"fortune_text_{mode}:{abs(hash(birthday + name)) % 10**12}:{today_key}"
    r = _get_redis()
    if r:
        try:
            cached = r.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass
    score_lines = []
    for cat in ["全体運", "金運", "恋愛運", "仕事運", "健康運", "対人運"]:
        sc = cat_sc.get(cat, 5)
        if cat in _CAT_PRIMARY_SYS:
            sn = _CAT_PRIMARY_SYS[cat]
            score_lines.append(f"{cat}: {sc}/10（{sn}ベース {sys_scores.get(sn, 5)}/10）")
        else:
            score_lines.append(f"{cat}: {sc}/10（5占術平均）")
    period = f"今日（{date_label}）" if mode == "daily" else f"今月（{date_label}）"
    guidance = "今日1日の具体的なアドバイスを含めてください" if mode == "daily" else "今月の前半・後半の流れを意識したアドバイスを含めてください"
    prompt = f"""あなたは20年以上のキャリアを持つ伝説の占い師です。龍神占術・月占星術・数秘術を組み合わせ、"当たりすぎる"と話題の存在です。

以下のユーザー情報と運勢スコアをもとに、{name}さんの{period}を占ってください。

【ユーザー情報】
名前: {name}さん
{persona if persona else "（診断情報なし）"}

【{period}の運勢スコア（10点満点）】
{chr(10).join(score_lines)}

【占いスタイルの絶対ルール】
・断言せず、傾向・流れとして表現する：「〜な流れがある」「〜を感じる一日（ひと月）」「〜が動きやすい」「〜に意識が向きやすい」などの表現を使う
・読んだ人が「もしかしてそういうことかも？」と思えるような、星占い的な抽象度で書く
・具体的な時間帯・場所・特定の人物は書かない
・ユーザーの状況（優先事項・恋愛状況・挑戦中のこと）の雰囲気をやわらかく織り込む
・{guidance}
・overall_messageは詩的かつ本質をついた一文。読んだ人の心に静かに響く言葉で
・スコアは1〜10の全範囲を正直に反映する。統計的に平均5〜6になるよう、低運（1〜4）・中運（5〜6）・好運（7〜10）をバランスよく使う
・全カテゴリが高い日も低い日もある。ユーザーの生まれ情報と今日の干支・星回り・数の流れを正直に反映し、特定スコア帯に偏らない
・スコア8以上：可能性や好機の「流れ」を感じさせる前向きな表現
・スコア4以下：無理をしないことや内省の「流れ」を感じさせる、穏やかな注意の表現
・スコア5〜7：静かなエネルギーの中にある気づきや変化の兆しを伝える
・各カテゴリのmessageは「占術的背景を感じさせる表現」（「星の配置が示す」「数のエネルギーが」「天干地支の流れで」「九星の気の流れが」など）を自然に用い、前半に現在の状態・エネルギー、後半に意識すべき具体的テーマを含める

以下のJSON形式のみで返してください：
{{
  "overall_message": "{period}を詩的に表現した一文（50文字以内・傾向・本質重視）",
  "categories": {{
    "全体運": {{"message": "占術的根拠を感じさせる【現在の状態・エネルギー】＋【意識すべき具体的テーマ】（50〜80文字）", "reason": "なぜそうなるか・占術的根拠（25文字以内）"}},
    "金運": {{"message": "占術的根拠を感じさせる【現在の状態・エネルギー】＋【意識すべき具体的テーマ】（50〜80文字）", "reason": "なぜそうなるか・占術的根拠（25文字以内）"}},
    "恋愛運": {{"message": "占術的根拠を感じさせる【現在の状態・エネルギー】＋【意識すべき具体的テーマ】（50〜80文字）", "reason": "なぜそうなるか・占術的根拠（25文字以内）"}},
    "仕事運": {{"message": "占術的根拠を感じさせる【現在の状態・エネルギー】＋【意識すべき具体的テーマ】（50〜80文字）", "reason": "なぜそうなるか・占術的根拠（25文字以内）"}},
    "健康運": {{"message": "占術的根拠を感じさせる【現在の状態・エネルギー】＋【意識すべき具体的テーマ】（50〜80文字）", "reason": "なぜそうなるか・占術的根拠（25文字以内）"}},
    "対人運": {{"message": "占術的根拠を感じさせる【現在の状態・エネルギー】＋【意識すべき具体的テーマ】（50〜80文字）", "reason": "なぜそうなるか・占術的根拠（25文字以内）"}}
  }},
  "energy_message": "この時期のエネルギーと起こりそうなこと（40文字以内・「〜のエネルギーが働き、〜の可能性がある」スタイル）",
  "lucky": {{
    "color": "ラッキーカラー（複数可：「赤または青」形式・12文字以内）",
    "color_reason": "なぜその色か・占術的根拠（20文字以内）",
    "action": "ラッキー行動（複数可：「〜か〜」形式・25文字以内）",
    "action_reason": "なぜその行動か（20文字以内）",
    "item": "ラッキーアイテム（複数可：「〜または〜」形式・15文字以内）",
    "item_reason": "なぜそのアイテムか（20文字以内）",
    "word": "今日の魔法の言葉（8文字以内）"
  }}
}}"""
    result = ask_claude(prompt, max_tokens=1800)
    if result and r:
        try:
            ttl = 86400 if mode == "daily" else 86400 * 7
            r.setex(cache_key, ttl, json.dumps(result, ensure_ascii=False))
        except Exception:
            pass
    return result

def gen_daily(user):
    import hashlib as _hs
    from datetime import datetime, date as _dc
    now = datetime.now()
    today = _dc(now.year, now.month, now.day)
    bdata = _parse_bdata(user)
    s = _calc_scores(bdata, today)

    def wt(a, b, c, d, e): return max(1, min(10, round(s["四柱推命"]*a + s["算命学"]*b + s["西洋占星術"]*c + s["数秘術"]*d + s["紫微斗数"]*e)))
    cat_sc = {
        "全体運": wt(0.2, 0.2, 0.2, 0.2, 0.2),
        "金運":   wt(0.75, 0.1, 0.05, 0.05, 0.05),
        "恋愛運": wt(0.05, 0.05, 0.75, 0.1, 0.05),
        "仕事運": wt(0.1, 0.75, 0.05, 0.05, 0.05),
        "健康運": wt(0.05, 0.1, 0.05, 0.05, 0.75),
        "対人運": wt(0.05, 0.1, 0.75, 0.05, 0.05),
    }

    date_str = now.strftime("%Y年%m月%d日")
    if user.get("diagnosis_done"):
        personalized = _gen_personalized_text(user, cat_sc, s, date_str, "daily")
        if personalized:
            categories = {
                cat: {"score": cat_sc[cat],
                      "message": personalized.get("categories", {}).get(cat, {}).get("message", ""),
                      "lucky": ""}
                for cat in ["全体運", "金運", "恋愛運", "仕事運", "健康運", "対人運"]
            }
            return {"date": date_str, "overall_message": personalized.get("overall_message", ""),
                    "categories": categories, "lucky_summary": personalized.get("lucky", {})}

    def lv(sc): return min(4, max(0, (sc - 1) * 4 // 9))
    def pick(lst, key):
        h = int(_hs.sha256(f"{user.get('birthday','')}|{now.strftime('%Y%m%d')}|{key}".encode()).hexdigest(), 16)
        return lst[h % len(lst)]

    ov = cat_sc["全体運"]
    om_list = ["今日はゆっくり休んで体を整えましょう","慎重に一歩ずつ進む日です","穏やかで安定した一日になりそう","運気が上昇中！積極的に動いて","最高の運気。大きな一歩を踏み出して"]
    overall_msg = om_list[lv(ov)]
    categories = {}
    for cat in ["全体運","金運","恋愛運","仕事運","健康運","対人運"]:
        sc = cat_sc[cat]
        v = lv(sc)
        msg = pick(_MSG[cat][v], cat + "_msg")
        lucky_list = _LUCKY.get(cat, [["",""],["" ,""],["" ,""],["" ,""],["" ,""]])[v]
        lucky = pick(lucky_list, cat + "_lucky") if cat not in ("健康運","対人運") else ""
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
        vals = [sum(ds["四柱推命"]*a + ds["算命学"]*b + ds["西洋占星術"]*c + ds["数秘術"]*d + ds["紫微斗数"]*e for _, _, ds in day_avgs) / len(day_avgs)]
        return max(1, min(10, round(vals[0])))
    cat_sc = {
        "全体運": round(sum(v for _,v,_ in day_avgs)/len(day_avgs)),
        "金運":   wt(0.75,0.1,0.05,0.05,0.05),
        "恋愛運": wt(0.05,0.05,0.75,0.1,0.05),
        "仕事運": wt(0.1,0.75,0.05,0.05,0.05),
        "健康運": wt(0.05,0.1,0.05,0.05,0.75),
        "対人運": wt(0.05,0.1,0.75,0.05,0.05),
    }
    cat_sc = {k: max(1, min(10, v)) for k, v in cat_sc.items()}

    month_str = now.strftime("%Y年%m月")
    if user.get("diagnosis_done"):
        sys_avg = {sys: round(sum(ds[sys] for _,_,ds in day_avgs) / len(day_avgs), 1)
                   for sys in ["四柱推命","算命学","西洋占星術","数秘術","紫微斗数"]}
        personalized = _gen_personalized_text(user, cat_sc, sys_avg, month_str, "monthly")
        if personalized:
            categories = {
                cat: {"score": cat_sc[cat],
                      "message": personalized.get("categories", {}).get(cat, {}).get("message", ""),
                      "trend": "安定"}
                for cat in ["全体運", "金運", "恋愛運", "仕事運", "健康運", "対人運"]
            }
            return {"month": month_str, "overall_message": personalized.get("overall_message", ""),
                    "categories": categories, "best_days": "", "caution_days": "",
                    "lucky_summary": personalized.get("lucky", {})}

    mid = last_day // 2
    first_half = sum(v for d,v,_ in day_avgs if d <= mid) / max(1, mid)
    second_half = sum(v for d,v,_ in day_avgs if d > mid) / max(1, last_day - mid)
    diff = second_half - first_half
    trend_map = {cat: ("上昇" if diff > 0.3 else "下降" if diff < -0.3 else "安定") for cat in cat_sc}
    for cat in ["金運","恋愛運","仕事運","健康運","対人運"]:
        sc = cat_sc[cat]
        if sc >= 7: trend_map[cat] = "上昇" if trend_map["全体運"] != "下降" else "安定"
        elif sc <= 4: trend_map[cat] = "下降" if trend_map["全体運"] != "上昇" else "安定"

    sorted_days = sorted(day_avgs, key=lambda x: -x[1])
    best_days = "・".join(str(d) + "日" for d,_,_ in sorted_days[:3])
    caution_days = "・".join(str(d) + "日" for d,_,_ in sorted_days[-3:])

    def lv(sc): return min(4, max(0, (sc-1)*4//9))
    def pick(lst, key):
        h = int(_hs.sha256(f"{user.get('birthday','')}|{year}{month:02d}|{key}".encode()).hexdigest(),16)
        return lst[h % len(lst)]

    month_str = now.strftime("%Y年%m月")
    ov = cat_sc["全体運"]
    om_list = ["慎重に過ごす月です","一歩一歩着実に","穏やかな運気の月","好調な月！積極的に","絶好調の月。大きな挑戦を"]
    categories = {}
    for cat in ["全体運","金運","恋愛運","仕事運","健康運","対人運"]:
        sc = cat_sc[cat]
        v = lv(sc)
        msg = pick(_MSG[cat][v], cat + "_monthly")
        categories[cat] = {"score": sc, "trend": trend_map[cat], "message": msg}
    return {
        "month": month_str,
        "overall_message": om_list[lv(ov)],
        "categories": categories,
        "best_days": best_days,
        "caution_days": caution_days,
    }

def gen_divination(user):
    today = datetime.now().strftime("%Y年%m月%d日")
    ctx = build_user_context(user)
    birthday = user.get("birthday", "")
    prompt = f"""{ctx}
今日: {today}

5つの占術でこの人物を診断してJSON形式で返してください。
{{
"四柱推命": {{"score": 1, "element": "五行属性", "lucky_direction": "吉方位", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}},
"算命学": {{"score": 1, "star": "主星名", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}},
"西洋占星術": {{"score": 1, "sign": "太陽星座名", "planet": "支配星", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}},
"数秘術": {{"score": 1, "life_path": "ライフパスナンバー1-9", "destiny": "運命数1-9", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}},
"紫微斗数": {{"score": 1, "main_star": "主星名", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}}
}}"""
    return ask_claude(prompt, max_tokens=2500)

def gen_yearly(user):
    current_year = datetime.now().year
    start = current_year - 2
    end = current_year + 10
    ctx = build_user_context(user)
    birthday = user.get("birthday", "")
    prompt = f"""{ctx}

{start}年から{end}年までの13年間の運勢推移をJSON形式で返してください。
{{
"overall_trend": "全体的な運気の流れ（50文字以内）",
"peak_year": 2026,
"caution_year": 2028,
"years": [
{{"year": 2024, "score": 1, "trend": "上昇かピークか下降か安定", "theme": "テーマ12文字以内"}}
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
        h = _re.search(r'午前(\d+)', birth_time)
        if h: birth_hour = int(h.group(1)) % 12
        h = _re.search(r'午後(\d+)', birth_time)
        if h: birth_hour = int(h.group(1)) % 12 + 12
        h = _re.search(r'(\d{1,2}):(\d{2})', birth_time)
        if h: birth_hour = int(h.group(1))
        h2 = _re.search(r'(\d{1,2})時', birth_time)
        if h2 and birth_hour == 12: birth_hour = int(h2.group(1))

    def digit_reduce(n):
        while n > 9 and n not in (11, 22, 33):
            n = sum(int(c) for c in str(n))
        return n
    life_path = digit_reduce(by + bm + bd_num)
    KANA_VAL = {
        'あ':1,'い':2,'う':3,'え':4,'お':5,
        'か':1,'き':2,'く':3,'け':4,'こ':5,
        'さ':1,'し':2,'す':3,'せ':4,'そ':5,
        'た':1,'ち':2,'つ':3,'て':4,'と':5,
        'な':1,'に':2,'ぬ':3,'ね':4,'の':5,
        'は':1,'ひ':2,'ふ':3,'へ':4,'ほ':5,
        'ま':1,'み':2,'む':3,'め':4,'も':5,
        'や':1,'ゆ':3,'よ':5,
        'ら':1,'り':2,'る':3,'れ':4,'ろ':5,
        'わ':1,'を':5,'ん':5,
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
        "四柱推命": 5.0 + (day_kan - 4.5) * 0.45,
        "算命学": 5.0 + (kyusei - 5.0) * 0.50,
        "西洋占星術": 5.0 + math.sin(sun_sign * math.pi / 6.0) * 2.0,
        "数秘術": 5.0 + (name_num - 5.0) * 0.35 + (life_path - 5.0) * 0.20,
        "紫微斗数": 5.0 + (zwds_base - 5.0) * 0.50,
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

    target_systems = ["四柱推命", "算命学", "西洋占星術", "数秘術", "紫微斗数"]
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
        return "⚠️ 運勢の計算に失敗しました。もう一度お試しください。"
    lines = [f"📅 {data.get('date','今日')}の運勢",
             f"🌙 {data.get('overall_message','')}"]
    if data.get('energy_message'):
        lines.append(f"🔮 {data['energy_message']}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    for cat, emoji in CAT_EMOJI.items():
        d = data.get("categories", {}).get(cat, {})
        score = d.get("score", 5)
        lines.append(f"{emoji} {cat}  ◆ {score}/10 ◆")
        lines.append(f"{d.get('message','')}")
        if d.get("reason"):
            lines.append(f"  ✦ {d['reason']}")
        if d.get("lucky"):
            lines.append(f"  → {d['lucky']}")
    lucky = data.get("lucky_summary")
    if lucky:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("✨ 今日のラッキー")
        if lucky.get("color"):
            cr = f"　{lucky['color_reason']}" if lucky.get("color_reason") else ""
            lines.append(f"🎨 カラー：{lucky['color']}{cr}")
        if lucky.get("action"):
            ar = f"　{lucky['action_reason']}" if lucky.get("action_reason") else ""
            lines.append(f"🎯 行動：{lucky['action']}{ar}")
        if lucky.get("item"):
            ir = f"　{lucky['item_reason']}" if lucky.get("item_reason") else ""
            lines.append(f"💎 アイテム：{lucky['item']}{ir}")
        if lucky.get("word"):   lines.append(f"🔑 キーワード：{lucky['word']}")
    return "\n".join(lines)

def fmt_monthly(data):
    if not data:
        return "⚠️ 今月の運勢の計算に失敗しました。"
    trend_icon = {"上昇": "↑", "安定": "→", "下降": "↓"}
    lines = [f"📆 {data.get('month','今月')}の運勢",
             f"🌙 {data.get('overall_message','')}"]
    if data.get('energy_message'):
        lines.append(f"🔮 {data['energy_message']}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    for cat, emoji in CAT_EMOJI.items():
        d = data.get("categories", {}).get(cat, {})
        score = d.get("score", 5)
        trend = d.get("trend", "安定")
        lines.append(f"{emoji} {cat}  ◆ {score}/10 ◆")
        lines.append(f"{d.get('message','')}")
        if d.get("reason"):
            lines.append(f"  ✦ {d['reason']}")
    lines += ["━━━━━━━━━━━━━━━━━━",
              f"吉日：{data.get('best_days','-')}",
              f"⚠️ 注意日：{data.get('caution_days','-')}"]
    lucky = data.get("lucky_summary")
    if lucky:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("✨ 今月のラッキー")
        if lucky.get("color"):
            cr = f"　{lucky['color_reason']}" if lucky.get("color_reason") else ""
            lines.append(f"🎨 カラー：{lucky['color']}{cr}")
        if lucky.get("action"):
            ar = f"　{lucky['action_reason']}" if lucky.get("action_reason") else ""
            lines.append(f"🎯 行動：{lucky['action']}{ar}")
        if lucky.get("item"):
            ir = f"　{lucky['item_reason']}" if lucky.get("item_reason") else ""
            lines.append(f"💎 アイテム：{lucky['item']}{ir}")
        if lucky.get("word"):   lines.append(f"🔑 キーワード：{lucky['word']}")
    return "\n".join(lines)

def fmt_divination(data):
    if not data:
        return "⚠️ 占術診断の計算に失敗しました。"
    sys_emoji = {"四柱推命": "☯️", "算命学": "🌟",
                 "西洋占星術": "♈", "数秘術": "🔢", "紫微斗数": "🌌"}
    lines = ["🔮 占術別 総合診断 🔮", "━━━━━━━━━━━━━━━━━━"]
    for sys_name, emoji in sys_emoji.items():
        d = data.get(sys_name, {})
        score = d.get("score", 5)
        lines.append(f"{emoji} 【{sys_name}】 {score_bar(score)} {score}/10")
        if sys_name == "四柱推命":
            lines.append(f"  五行: {d.get('element','-')} 吉方位: {d.get('lucky_direction','-')}")
        elif sys_name == "算命学":
            lines.append(f"  主星: {d.get('star','-')}")
        elif sys_name == "西洋占星術":
            lines.append(f"  {d.get('sign','-')} 支配星: {d.get('planet','-')}")
        elif sys_name == "数秘術":
            lines.append(f"  ライフパス: {d.get('life_path','-')} 運命数: {d.get('destiny','-')}")
        elif sys_name == "紫微斗数":
            lines.append(f"  主星: {d.get('main_star','-')}")
        lines.append(f"  {d.get('description','')}")
        lines.append(f"  ▶ {d.get('current_luck','')}")
        lines.append("")
    return "\n".join(lines).rstrip()

def fmt_yearly(data):
    if not data:
        return "⚠️ 年間推移の計算に失敗しました。"
    current_year = datetime.now().year
    trend_sym = {"上昇": "↗", "ピーク": "🔝", "下降": "↘", "安定": "→"}
    lines = ["📊 12年間の運勢推移 📊",
             f"✨ {data.get('overall_trend','')}",
             "━━━━━━━━━━━━━━━━━━",
             "年  バー      点 傾向 テーマ",
             "━━━━━━━━━━━━━━━━━━"]
    for yd in data.get("years", []):
        year = yd.get("year", "")
        score = yd.get("score", 5)
        trend = yd.get("trend", "安定")
        theme = yd.get("theme", "")
        now_mark = "◀今" if year == current_year else "   "
        lines.append(
            f"{year} [{block_bar(score)}] {score:2d} {trend_sym.get(trend,'→')} {theme} {now_mark}")
    lines += ["━━━━━━━━━━━━━━━━━━",
              f"🏆 最高の年：{data.get('peak_year','-')}年",
              f"⚠️ 注意の年：{data.get('caution_year','-')}年"]
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
        push(user_id, f"⚠️ エラーが発生しました。もう一度お試しください。\n({e})")

def graph_image_thread(user_id, user):
    try:
        birthday_iso = birthday_to_iso(user.get("birthday", ""))
        data = get_graph_data_cached(user)
        if not data:
            push(user_id, "⚠️ グラフデータの生成に失敗しました。もう一度お試しください。")
            return

        img_bytes = generate_fortune_image(data, user)
        img_id = uuid.uuid4().hex
        store_image(img_id, img_bytes)

        base = bot_base_url()
        if not base:
            push(user_id, "⚠️ サーバーURLが取得できませんでした。")
            return

        img_url = f"{base}/img/{img_id}"
        push_image(user_id, img_url)

        legend = (
            "📊 グラフの色の凡例\n"
            + LEGEND_TEXT + "\n\n"
            "📸 スクリーンショットで保存できます。\n"
            "※データは24時間キャッシュされます。"
        )
        push(user_id, legend, with_menu=True)

    except Exception as e:
        push(user_id, f"⚠️ グラフの生成に失敗しました。\n({e})")


def past_graph_image_thread(user_id, user):
    try:
        data = get_graph_data_cached(user)
        if not data:
            push(user_id, "⚠️ グラフデータの生成に失敗しました。もう一度お試しください。")
            return

        img_bytes = generate_past_fortune_image(data, user)
        img_id = uuid.uuid4().hex
        store_image(img_id, img_bytes)

        base = bot_base_url()
        if not base:
            push(user_id, "⚠️ サーバーURLが取得できませんでした。")
            return

        img_url = f"{base}/img/{img_id}"
        push_image(user_id, img_url)
        push(user_id,
             "📊 過去12年の運勢推移です。\n\n実際に良かった年・大変だった年と、どの占術の好調・低調の波が一致しているか確認してみてください。\n一番一致している占術があなたとの相性が高い占術です✨")
        legend = (
            "📊 グラフの色の凡例\n"
            + LEGEND_TEXT + "\n\n"
            "📸 スクリーンショットで保存できます。\n"
            "※データは24時間キャッシュされます。"
        )
        push(user_id, legend, with_menu=True)

    except Exception as e:
        push(user_id, f"⚠️ グラフの生成に失敗しました。\n({e})")


WELCOME_TEXT = """🌙 星夜堂へようこそ ✨

星夜堂は、複数の占術を組み合わせた
本格的な占いサービスです。

【できること】
📅 今日の運勢
全体運・金運・恋愛運・仕事運・
健康運・対人運の6カテゴリを
スコア付き一覧表示

📆 今月の運勢
カテゴリ別スコア＋上昇/安定/下降の
トルンドと吉日・注意日をお知らせ

🔮 占術別診断
四柱推命・算命学・西洋占星術・
数秘術・紫微斗数の5占術の結果を
スコア付きで一覧できます

📊 今年/12年推移グラフ
5占術の全体運を折れ線グラフ画像で
チャットに直接送信します"""

REGISTRATION_PROMPT = """📝 まず、以下を教えてください。

📅 生年月日（分かれば時刻も）
👤 名前と読み方（平仮名） ※数秘術の精度向上
📍 出生地 ※精度向上

入力例：
1990年3月15日 午前10時
田中太郎（たなかたろう） 東京都"""

@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    set_user(user_id, {"state": "waiting_diagnosis", "birthday": None, "name": None, "birthplace": None, "birth_time": None, "diagnosis_done": False})
    LIFF_URL = "https://liff.line.me/2010080648-3cltj7zs"
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

    if text == "誕生日変更":
        user["state"] = "waiting_birthday"
        set_user(user_id, user)
        reply_msg(event.reply_token, "新しい生年月日を入力してください。\n（例: 1990年3月15日）")
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
            set_user(user_id, user) # Redisに永続化
            detail = ""
            if user.get("birth_time"): detail += f" {user['birth_time']}"
            if user.get("name"): detail += f"\n👤 {user['name']}"
            if user.get("birthplace"): detail += f"\n📍 {user['birthplace']}"
            reply_msg(event.reply_token,
                      f"✨ {birthday}{detail}\n\nで登録しました！\nメニューからお選びください。",
                      with_menu=True)
        else:
            reply_msg(event.reply_token,
                      "生年月日の形式を認識できませんでした。\n\n以下の形式でご入力ください：\n・1990年3月15日\n・1990/3/15\n・1990-3-15")
        return

    birthday = user["birthday"]

    loading_msgs = {
        "今日の運勢": "📅 今日の運勢を占い中です...\nしばらくお待ちください 🌙",
        "今月の運勢": "📆 今月の運勢を計算中です...\nしばらくお待ちください 🌕",
        "占術別診断": "🔮 5つの占術で診断中です...\nしばらくお待ちください ✨",
        "今年/12年推移グラフ": "📊 12年間の運勢推移を計算中です...\nしばらくお待ちください 🌌",
    }
    fortune_map = {
        "今日の運勢": "daily",
        "今月の運勢": "monthly",
        "占術別診断": "divination",
    }

    if text in ("過去12年", "過去の運勢", "相性診断"):
        reply_msg(event.reply_token,
                  "📈 過去12年の折れ線グラフを生成中です...\nしばらくお待ちください 🌌")
        threading.Thread(
            target=past_graph_image_thread,
            args=(user_id, user),
            daemon=True,
        ).start()
        return

    if text == "今年/12年推移グラフ":
        reply_msg(event.reply_token,
                  "📈 折れ線グラフを生成中です...\nしばらくお待ちください 🌌\n（初回は20〜30秒かかります）")
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
                system="""あなたは「星夜堂（せいやどう）」の占い師AIです。
四柱推命・算命学・占星術・数秘術・紫微斗数を専門とする神秘的な占いブランドです。
・丁寧で神秘的な口調（「〜でございます」「〜かと存じます」）
・星・月・夜をイメージした言葉を自然に使う
・相手の気持ちに寄り添い前向きなメッセージを伝える
返答は200文字以内で。""",
                messages=[{"role": "user", "content": text}],
            )
            reply_text = resp.content[0].text
        except Exception:
            reply_text = "申し訳ございません。只今、星の導きが乱れてえります。しばらくお待ちくださいませ。🌙"
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
    return "星夜堂 LINE Bot is running ✨"


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
        push(line_user_id, msg, with_menu=False)
    except Exception as e:
        print(f"Push error: {e}")
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
