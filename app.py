import os
import json
import re
import threading
from datetime import datetime
from flask import Flask, request, abort
import anthropic
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, QuickReply, QuickReplyItem, MessageAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# In-memory user state  {user_id: {"state": str, "birthday": str}}
user_data = {}


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def get_user(user_id):
    if user_id not in user_data:
        user_data[user_id] = {"state": "new", "birthday": None}
    return user_data[user_id]


def parse_birthday(text):
    patterns = [
        r'(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})',
        r'(\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            year = int(m.group(1))
            if year < 100:
                year += 1900
            try:
                return datetime(year, int(m.group(2)), int(m.group(3))).strftime("%Y年%m月%d日")
            except Exception:
                pass
    return None


def score_bar(score):
    filled = max(0, min(5, round(score / 10 * 5)))
    return "⭐" * filled + "☆" * (5 - filled)


def block_bar(score):
    filled = max(0, min(5, round(score / 2)))
    return "█" * filled + "░" * (5 - filled)


def main_menu_qr():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📅 今日の運勢",  text="今日の運勢")),
        QuickReplyItem(action=MessageAction(label="📆 今月の運勢",  text="今月の運勢")),
        QuickReplyItem(action=MessageAction(label="🔮 占術別診断",  text="占術別診断")),
        QuickReplyItem(action=MessageAction(label="📊 12年の推移",  text="12年の推移")),
        QuickReplyItem(action=MessageAction(label="✏️ 誕生日変更",  text="誕生日変更")),
    ])


def push(user_id, text, with_menu=True):
    with ApiClient(configuration) as api_client:
        msg = TextMessage(text=text, quick_reply=main_menu_qr() if with_menu else None)
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[msg])
        )


def reply_msg(reply_token, text, with_menu=False):
    with ApiClient(configuration) as api_client:
        msg = TextMessage(text=text, quick_reply=main_menu_qr() if with_menu else None)
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[msg])
        )


# ──────────────────────────────────────────────────────────────
# Fortune generation (Claude API)
# ──────────────────────────────────────────────────────────────

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


def gen_daily(birthday):
    today = datetime.now().strftime("%Y年%m月%d日")
    prompt = f"""生年月日: {birthday}
今日: {today}

今日の運勢を以下のJSON形式で返してください。
{{
  "date": "{today}",
  "overall_message": "今日全体のひとことメッセージ（50文字以内）",
  "categories": {{
    "全体運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": "ラッキーアイテム"}},
    "金運":   {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": "ラッキーカラー"}},
    "恋愛運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": "ラッキーアクション"}},
    "仕事運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": "ラッキーワード"}},
    "健康運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": ""}},
    "対人運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": ""}}
  }}
}}"""
    return ask_claude(prompt)


def gen_monthly(birthday):
    month = datetime.now().strftime("%Y年%m月")
    prompt = f"""生年月日: {birthday}
対象月: {month}

今月の運勢を以下のJSON形式で返してください。
{{
  "month": "{month}",
  "overall_message": "今月全体のメッセージ（80文字以内）",
  "categories": {{
    "全体運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},
    "金運":   {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},
    "恋愛運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},
    "仕事運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},
    "健康運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},
    "対人運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}}
  }},
  "best_days": "吉日（例: 3日・15日・22日）",
  "caution_days": "注意日（例: 8日・19日）"
}}"""
    return ask_claude(prompt)


def gen_divination(birthday):
    today = datetime.now().strftime("%Y年%m月%d日")
    prompt = f"""生年月日: {birthday}
今日: {today}

5つの占術でこの人物を診断してJSON形式で返してください。
{{
  "四柱推命": {{
    "score": 数値1-10,
    "element": "五行属性（木か火か土か金か水）",
    "lucky_direction": "吉方位（例: 北東）",
    "description": "性格・運命の特徴（50文字以内）",
    "current_luck": "現在の運気（30文字以内）"
  }},
  "算命学": {{
    "score": 数値1-10,
    "star": "主星名（例: 貫索星）",
    "description": "算命学による特徴（50文字以内）",
    "current_luck": "現在の運気（30文字以内）"
  }},
  "西洋占星術": {{
    "score": 数値1-10,
    "sign": "太陽星座名",
    "planet": "支配星",
    "description": "星座の特徴（50文字以内）",
    "current_luck": "現在の運気（30文字以内）"
  }},
  "数秘術": {{
    "score": 数値1-10,
    "life_path": "ライフパスナンバー（1-9）",
    "destiny": "運命数（1-9）",
    "description": "数秘術による特徴（50文字以内）",
    "current_luck": "現在の運気（30文字以内）"
  }},
  "紫微斗数": {{
    "score": 数値1-10,
    "main_star": "主星名（例: 紫微星）",
    "description": "紫微斗数による特徴（50文字以内）",
    "current_luck": "現在の運気（30文字以内）"
  }}
}}"""
    return ask_claude(prompt, max_tokens=2500)


def gen_yearly(birthday):
    current_year = datetime.now().year
    start = current_year - 2
    end = current_year + 10
    prompt = f"""生年月日: {birthday}

{start}年から{end}年までの13年間の運勢推移をJSON形式で返してください。
四柱推命の大運・流年と西洋占星術のトランジットを組み合わせて算出してください。

{{
  "overall_trend": "全体的な運気の流れ（50文字以内）",
  "peak_year": 最も運気が高い年（数値のみ）,
  "caution_year": 最も注意が必要な年（数値のみ）,
  "years": [
    {{"year": 年（数値）, "score": 数値1-10, "trend": "上昇かピークか下降か安定", "theme": "その年のテーマ（12文字以内）"}},
    ...13件分...
  ]
}}"""
    return ask_claude(prompt, max_tokens=2500)


# ──────────────────────────────────────────────────────────────
# Formatters
# ──────────────────────────────────────────────────────────────

CAT_EMOJI = {
    "全体運": "⭐", "金運": "💰", "恋愛運": "💕",
    "仕事運": "💼", "健康運": "💪", "対人運": "🤝",
}


def fmt_daily(data):
    if not data:
        return "⚠️ 運勢の計算に失敗しました。もう一度お試しください。"
    lines = [
        f"✨ {data.get('date', '今日')}の運勢 ✨",
        f"🌙 {data.get('overall_message', '')}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for cat, emoji in CAT_EMOJI.items():
        d = data.get("categories", {}).get(cat, {})
        score = d.get("score", 5)
        msg = d.get("message", "")
        lucky = d.get("lucky", "")
        lines.append(f"{emoji} {cat}  {score_bar(score)}  {score}/10")
        lines.append(f"   {msg}")
        if lucky:
            lines.append(f"   🍀 {lucky}")
    return "\n".join(lines)


def fmt_monthly(data):
    if not data:
        return "⚠️ 今月の運勢の計算に失敗しました。"
    trend_icon = {"上昇": "📈", "安定": "➡️", "下降": "📉"}
    lines = [
        f"🌕 {data.get('month', '今月')}の運勢 🌕",
        f"✨ {data.get('overall_message', '')}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for cat, emoji in CAT_EMOJI.items():
        d = data.get("categories", {}).get(cat, {})
        score = d.get("score", 5)
        trend = d.get("trend", "安定")
        msg = d.get("message", "")
        lines.append(f"{emoji} {cat}  {score_bar(score)}  {trend_icon.get(trend, '➡️')}")
        lines.append(f"   {msg}")
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"🌟 吉日：{data.get('best_days', '-')}",
        f"⚠️ 注意日：{data.get('caution_days', '-')}",
    ]
    return "\n".join(lines)


def fmt_divination(data):
    if not data:
        return "⚠️ 占術診断の計算に失敗しました。"
    system_emoji = {
        "四柱推命": "☯️", "算命学": "🌟",
        "西洋占星術": "♈", "数秘術": "🔢", "紫微斗数": "🌌",
    }
    lines = ["🔮 占術別 総合診断 🔮", "━━━━━━━━━━━━━━━━━━"]
    for sys_name, emoji in system_emoji.items():
        d = data.get(sys_name, {})
        score = d.get("score", 5)
        lines.append(f"{emoji} 【{sys_name}】 {score_bar(score)}  {score}/10")
        if sys_name == "四柱推命":
            lines.append(f"   五行: {d.get('element','-')}  吉方位: {d.get('lucky_direction','-')}")
        elif sys_name == "算命学":
            lines.append(f"   主星: {d.get('star','-')}")
        elif sys_name == "西洋占星術":
            lines.append(f"   {d.get('sign','-')}  支配星: {d.get('planet','-')}")
        elif sys_name == "数秘術":
            lines.append(f"   ライフパス: {d.get('life_path','-')}  運命数: {d.get('destiny','-')}")
        elif sys_name == "紫微斗数":
            lines.append(f"   主星: {d.get('main_star','-')}")
        lines.append(f"   {d.get('description','')}")
        lines.append(f"   ▶ {d.get('current_luck','')}")
        lines.append("")
    return "\n".join(lines).rstrip()


def fmt_yearly(data):
    if not data:
        return "⚠️ 年間推移の計算に失敗しました。"
    current_year = datetime.now().year
    trend_sym = {"上昇": "↗", "ピーク": "🔝", "下降": "↘", "安定": "→"}
    lines = [
        "📊 12年間の運勢推移 📊",
        f"✨ {data.get('overall_trend', '')}",
        "━━━━━━━━━━━━━━━━━━",
        "年     バー      点  傾向  テーマ",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for yd in data.get("years", []):
        year = yd.get("year", "")
        score = yd.get("score", 5)
        trend = yd.get("trend", "安定")
        theme = yd.get("theme", "")
        sym = trend_sym.get(trend, "→")
        bar = block_bar(score)
        now_mark = "◀今" if year == current_year else "   "
        lines.append(f"{year} [{bar}] {score:2d} {sym} {theme} {now_mark}")
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"🏆 最高の年：{data.get('peak_year', '-')}年",
        f"⚠️ 注意の年：{data.get('caution_year', '-')}年",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Background fortune thread
# ──────────────────────────────────────────────────────────────

def fortune_thread(user_id, birthday, fortune_type):
    try:
        if fortune_type == "daily":
            push(user_id, fmt_daily(gen_daily(birthday)))
        elif fortune_type == "monthly":
            push(user_id, fmt_monthly(gen_monthly(birthday)))
        elif fortune_type == "divination":
            push(user_id, fmt_divination(gen_divination(birthday)))
        elif fortune_type == "yearly":
            push(user_id, fmt_yearly(gen_yearly(birthday)))
    except Exception as e:
        push(user_id, f"⚠️ エラーが発生しました。もう一度お試しください。\n({e})")


# ──────────────────────────────────────────────────────────────
# LINE Event Handlers
# ──────────────────────────────────────────────────────────────

WELCOME_TEXT = """🌙 星夜堂へようこそ ✨

星夜堂は、複数の占術を組み合わせた
本格的な占いサービスです。

【できること】
📅 今日の運勢
  金運・恋愛運・仕事運・健康運など
  6カテゴリをスコア付き一覧表示

📆 今月の運勢
  カテゴリ別スコア＋上昇/安定/下降の
  トレンドと吉日・注意日をお知らせ

🔮 占術別診断
  四柱推命・算命学・西洋占星術・
  数秘術・紫微斗数の5占術を比較

📊 12年間の運勢推移
  過去2年〜未来10年の運気を
  グラフ形式で可視化

━━━━━━━━━━━━━━━━━━
まず、あなたの生年月日を
教えてください。

入力例：
・1990年3月15日
・1990/3/15"""


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    user_data[user_id] = {"state": "waiting_birthday", "birthday": None}
    reply_msg(event.reply_token, WELCOME_TEXT)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user = get_user(user_id)
    text = event.message.text.strip()

    # ── 誕生日変更 ──
    if text == "誕生日変更":
        user["state"] = "waiting_birthday"
        reply_msg(event.reply_token, "新しい生年月日を入力してください。\n（例: 1990年3月15日）")
        return

    # ── 生年月日待ち ──
    if user["state"] == "waiting_birthday" or not user.get("birthday"):
        birthday = parse_birthday(text)
        if birthday:
            user["birthday"] = birthday
            user["state"] = "menu"
            reply_msg(
                event.reply_token,
                f"✨ {birthday} で登録しました！\n\nメニューからお選びください。",
                with_menu=True,
            )
        else:
            reply_msg(
                event.reply_token,
                "生年月日の形式を認識できませんでした。\n\n以下の形式でご入力ください：\n・1990年3月15日\n・1990/3/15\n・1990-3-15",
            )
        return

    birthday = user["birthday"]

    loading_msgs = {
        "今日の運勢": "📅 今日の運勢を占い中です...\nしばらくお待ちください 🌙",
        "今月の運勢": "📆 今月の運勢を計算中です...\nしばらくお待ちください 🌕",
        "占術別診断": "🔮 5つの占術で診断中です...\nしばらくお待ちください ✨",
        "12年の推移": "📊 12年間の運勢推移を計算中です...\nしばらくお待ちください 🌌",
    }
    fortune_map = {
        "今日の運勢": "daily",
        "今月の運勢": "monthly",
        "占術別診断": "divination",
        "12年の推移": "yearly",
    }

    if text in fortune_map:
        reply_msg(event.reply_token, loading_msgs[text])
        threading.Thread(
            target=fortune_thread,
            args=(user_id, birthday, fortune_map[text]),
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
            reply_text = "申し訳ございません。只今、星の導きが乱れております。しばらくお待ちくださいませ。🌙"

        reply_msg(event.reply_token, reply_text, with_menu=True)


# ──────────────────────────────────────────────────────────────
# Flask routes
# ──────────────────────────────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/", methods=["GET"])
def health_check():
    return "星夜堂 LINE Bot is running ✨"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
