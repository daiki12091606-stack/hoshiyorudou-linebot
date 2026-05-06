import os
import json
import re
import threading
from datetime import datetime
from flask import Flask, request, abort, render_template_string, jsonify
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

user_data = {}
graph_cache = {}


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


def birthday_to_iso(bday):
    try:
        return datetime.strptime(bday, "%Y年%m月%d日").strftime("%Y-%m-%d")
    except Exception:
        return bday


def iso_to_birthday(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%Y年%m月%d日")
    except Exception:
        return iso


def bot_base_url():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if domain:
        return f"https://{domain}"
    return ""


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
        QuickReplyItem(action=MessageAction(label="📈 グラフ表示",  text="グラフ表示")),
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
    prompt = f"""生年月日: {birthday}\n今日: {today}\n\n今日の運勢を以下のJSON形式で返してください。\n{{\n  "date": "{today}",\n  "overall_message": "今日全体のひとことメッセージ（50文字以内）",\n  "categories": {{\n    "全体運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": "ラッキーアイテム"}},\n    "金運":   {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": "ラッキーカラー"}},\n    "恋愛運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": "ラッキーアクション"}},\n    "仕事運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": "ラッキーワード"}},\n    "健康運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": ""}},\n    "対人運": {{"score": 数値1-10, "message": "コメント20文字以内", "lucky": ""}}\n  }}\n}}"""
    return ask_claude(prompt)


def gen_monthly(birthday):
    month = datetime.now().strftime("%Y年%m月")
    prompt = f"""生年月日: {birthday}\n対象月: {month}\n\n今月の運勢を以下のJSON形式で返してください。\n{{\n  "month": "{month}",\n  "overall_message": "今月全体のメッセージ（80文字以内）",\n  "categories": {{\n    "全体運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},\n    "金運":   {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},\n    "恋愛運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},\n    "仕事運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},\n    "健康運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}},\n    "対人運": {{"score": 数値1-10, "trend": "上昇か安定か下降", "message": "30文字以内"}}\n  }},\n  "best_days": "吉日（例: 3日・15日・22日）",\n  "caution_days": "注意日（例: 8日・19日）"\n}}"""
    return ask_claude(prompt)


def gen_divination(birthday):
    today = datetime.now().strftime("%Y年%m月%d日")
    prompt = f"""生年月日: {birthday}\n今日: {today}\n\n5つの占術でこの人物を診断してJSON形式で返してください。\n{{\n  "四柱推命": {{"score": 数値1-10, "element": "五行属性", "lucky_direction": "吉方位", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}},\n  "算命学": {{"score": 数値1-10, "star": "主星名", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}},\n  "西洋占星術": {{"score": 数値1-10, "sign": "太陽星座名", "planet": "支配星", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}},\n  "数秘術": {{"score": 数値1-10, "life_path": "ライフパスナンバー1-9", "destiny": "運命数1-9", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}},\n  "紫微斗数": {{"score": 数値1-10, "main_star": "主星名", "description": "特徴50文字以内", "current_luck": "現在の運気30文字以内"}}\n}}"""
    return ask_claude(prompt, max_tokens=2500)


def gen_yearly(birthday):
    current_year = datetime.now().year
    start = current_year - 2
    end = current_year + 10
    prompt = f"""生年月日: {birthday}\n\n{start}年から{end}年までの13年間の運勢推移をJSON形式で返してください。\n{{\n  "overall_trend": "全体的な運気の流れ（50文字以内）",\n  "peak_year": 最も運気が高い年（数値のみ）,\n  "caution_year": 最も注意が必要な年（数値のみ）,\n  "years": [\n    {{"year": 年（数値）, "score": 数値1-10, "trend": "上昇かピークか下降か安定", "theme": "その年のテーマ（12文字以内）"}},\n    ...13件分...\n  ]\n}}"""
    return ask_claude(prompt, max_tokens=2500)


def gen_graph_data(birthday):
    current_year = datetime.now().year
    start_year = current_year - 2
    prompt = f"""生年月日: {birthday}\n\n5つの占術それぞれについて、以下の期間の「全体運スコア（1-10の整数）」を各占術の理論に基づいて算出してください。\n1) 今年（{current_year}年）の1月〜12月の月別スコア（12個）\n2) {start_year}年〜{start_year + 12}年の年別スコア（13個）\n\nJSON形式（数値のみ）：\n{{\n  "monthly": {{\n    "四柱推命":   [1月,2月,3月,4月,5月,6月,7月,8月,9月,10月,11月,12月],\n    "算命学":     [12個],\n    "西洋占星術": [12個],\n    "数秘術":     [12個],\n    "紫微斗数":   [12個]\n  }},\n  "yearly": {{\n    "四柱推命":   [{start_year}〜{start_year+12}年の13個],\n    "算命学":     [13個],\n    "西洋占星術": [13個],\n    "数秘術":     [13個],\n    "紫微斗数":   [13個]\n  }}\n}}"""
    return ask_claude(prompt, max_tokens=1500)


def get_graph_data_cached(birthday_iso):
    now = datetime.now()
    if birthday_iso in graph_cache:
        cached = graph_cache[birthday_iso]
        age_hours = (now - cached["cached_at"]).total_seconds() / 3600
        if age_hours < 24:
            return cached["data"]
    birthday = iso_to_birthday(birthday_iso)
    data = gen_graph_data(birthday)
    if data:
        graph_cache[birthday_iso] = {"data": data, "cached_at": now}
    return data


CAT_EMOJI = {
    "全体運": "⭐", "金運": "💰", "恋愛運": "💕",
    "仕事運": "💼", "健康運": "💪", "対人運": "🤝",
}


def fmt_daily(data):
    if not data:
        return "⚠️ 運勢の計算に失敗しました。もう一度お試しください。"
    lines = [f"✨ {data.get('date', '今日')}の運勢 ✨", f"🌙 {data.get('overall_message', '')}", "━━━━━━━━━━━━━━━━━━"]
    for cat, emoji in CAT_EMOJI.items():
        d = data.get("categories", {}).get(cat, {})
        score = d.get("score", 5)
        lines.append(f"{emoji} {cat}  {score_bar(score)}  {score}/10")
        lines.append(f"   {d.get('message', '')}")
        if d.get("lucky"):
            lines.append(f"  #🍀 {d['lucky']}")
    return "\n".join(lines)


def fmt_monthly(data):
    if not data:
        return "⚠️ 今月の運勢の計算に失敗しました。"
    trend_icon = {"上昇": "📈", "安定": "➡️", "下降": "📉"}
    lines = [f"🌕 {data.get('month', '今月')}の運勢 🌕", f"✨ {data.get('overall_message', '')}", "━━━━━━━━━━━━━━━━━━"]
    for cat, emoji in CAT_EMOJI.items():
        d = data.get("categories", {}).get(cat, {})
        score = d.get("score", 5)
        trend = d.get("trend", "安定")
        lines.append(f"{emoji} {cat}  {score_bar(score)}  {trend_icon.get(trend, '➡️')}")
        lines.append(f"   {d.get('message', '')}")
    lines += ["━━━━━━━━━━━━━━━━━━", f"🌟 吉日：{data.get('best_days', '-')}", f"⚠️ 注意日：{data.get('caution_days', '-')}"]
    return "\n".join(lines)


def fmt_divination(data):
    if not data:
        return "⚠️ 占術診断の計算に失敗しました。"
    system_emoji = {"四柱推命": "☯️", "算命学": "🌟", "西洋占星術": "♈", "数秘術": "🔢", "紫微斗数": "🌌"}
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
    lines = ["📊 12年間の運勢推移 📊", f"✨ {data.get('overall_trend', '')}", "━━━━━━━━━━━━━━━━━━", "年     バー      点  傾向  テーマ", "━━━━━━━━━━━━━━━━━━"]
    for yd in data.get("years", []):
        year = yd.get("year", "")
        score = yd.get("score", 5)
        trend = yd.get("trend", "安定")
        theme = yd.get("theme", "")
        now_mark = "◀今" if year == current_year else "   "
        lines.append(f"{year} [{block_bar(score)}] {score:2d} {trend_sym.get(trend,'→')} {theme} {now_mark}")
    lines += ["━━━━━━━━━━━━━━━━━━", f"🏆 最高の年：{data.get('peak_year', '-')}年", f"⚠️ 注意の年：{data.get('caution_year', '-')}年"]
    return "\n".join(lines)


GRAPH_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>星夜堂 運勢グラフ</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: linear-gradient(160deg, #08081e 0%, #160828 50%, #0a0a1e 100%);
  color: #e8e8f8;
  font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif;
  min-height: 100vh;
  padding: 0 12px 32px;
}
header { text-align: center; padding: 24px 0 18px; }
header h1 { font-size: 21px; color: #c8a8ff; letter-spacing: 2px; margin-bottom: 5px; }
header p { font-size: 12px; color: #6868a0; }
.chart-card {
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(180,140,255,0.18);
  border-radius: 16px;
  padding: 16px 14px 14px;
  margin-bottom: 18px;
}
.chart-card h2 { font-size: 14px; color: #a0c0ff; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid rgba(160,192,255,0.15); }
.legend { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 12px; }
.legend-item { display: flex; align-items: center; gap: 5px; font-size: 11px; color: #aaaacc; }
.legend-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
#loadingDiv { text-align: center; padding: 50px 20px; color: #7878aa; font-size: 14px; }
.spinner { width: 34px; height: 34px; border: 3px solid rgba(180,140,255,0.2); border-top-color: #c8a8ff; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 14px; }
@keyframes spin { to { transform: rotate(360deg); } }
.error { color: #ff8888; text-align: center; padding: 24px; font-size: 14px; line-height: 1.6; }
footer { text-align: center; color: #44445a; font-size: 11px; padding-top: 8px; }
</style>
</head>
<body>
<header>
  <h1>🌙 星夜堂 運勢グラフ</h1>
  <p id="bdayLabel">全体運の推移（5占術）</p>
</header>
<div id="loadingDiv">
  <div class="spinner"></div>
  <p>占い中です... 🌙<br>初回は少しお待ちください</p>
</div>
<div id="chartsArea" style="display:none">
  <div class="chart-card">
    <h2>📆 今年の月別運勢推移（全体運）</h2>
    <canvas id="monthlyChart"></canvas>
    <div id="monthlyLegend" class="legend"></div>
  </div>
  <div class="chart-card">
    <h2>📊 12年間の運勢推移（全体運）</h2>
    <canvas id="yearlyChart"></canvas>
    <div id="yearlyLegend" class="legend"></div>
  </div>
</div>
<footer>星夜堂 ✨ ブックマークして毎日チェック</footer>
<script>
const COLORS = {
  "四柱推命": "#4FC3F7", "算命学": "#FFD54F",
  "西洋占星術": "#CE93D8", "数秘術": "#81C784", "紫微斗数": "#FF8A65"
};
const BASE_OPTS = {
  responsive: true,
  interaction: { mode: "index", intersect: false },
  scales: {
    y: { min: 1, max: 10, ticks: { color: "#8888bb", stepSize: 1, font: { size: 10 } }, grid: { color: "rgba(255,255,255,0.05)" } },
    x: { ticks: { color: "#8888bb", font: { size: 10 } }, grid: { color: "rgba(255,255,255,0.05)" } }
  },
  plugins: {
    legend: { display: false },
    tooltip: { backgroundColor: "rgba(16,16,36,0.95)", titleColor: "#ccccff", bodyColor: "#9999cc", borderColor: "rgba(160,120,255,0.4)", borderWidth: 1, padding: 10 }
  }
};
function makeDatasets(data) {
  return Object.entries(data).map(([name, scores]) => ({
    label: name, data: scores,
    borderColor: COLORS[name] || "#aaaaff",
    backgroundColor: (COLORS[name] || "#aaaaff") + "22",
    tension: 0.35, pointRadius: 3.5, pointHoverRadius: 6, borderWidth: 2.5, fill: false
  }));
}
function makeLegend(id, data) {
  document.getElementById(id).innerHTML = Object.entries(data).map(([name]) =>
    `<div class="legend-item"><div class="legend-dot" style="background:${COLORS[name]}"></div><span>${name}</span></div>`
  ).join("");
}
const params = new URLSearchParams(location.search);
const bParam = params.get("b") || "";
if (bParam) document.getElementById("bdayLabel").textContent = `生年月日: ${bParam}  ｜  全体運の推移（5占術）`;
const currentYear = new Date().getFullYear();
const MONTHS = ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"];
const startYear = currentYear - 2;
const YEARS = Array.from({length: 13}, (_, i) => { const y = startYear + i; return y === currentYear ? y+"年◀" : y+"年"; });
fetch(`/api/graph-data?b=${encodeURIComponent(bParam)}`)
  .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(d => {
    document.getElementById("loadingDiv").style.display = "none";
    document.getElementById("chartsArea").style.display = "block";
    new Chart(document.getElementById("monthlyChart").getContext("2d"), { type: "line", data: { labels: MONTHS, datasets: makeDatasets(d.monthly) }, options: BASE_OPTS });
    makeLegend("monthlyLegend", d.monthly);
    new Chart(document.getElementById("yearlyChart").getContext("2d"), { type: "line", data: { labels: YEARS, datasets: makeDatasets(d.yearly) }, options: BASE_OPTS });
    makeLegend("yearlyLegend", d.yearly);
  })
  .catch(e => {
    document.getElementById("loadingDiv").innerHTML = `<p class="error">⚠️ データの読み込みに失敗しました。<br>LINEで「グラフ表示」を再度タップしてお試しください。</p>`;
  });
</script>
</body>
</html>"""


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
  トレンドと吉日・注意日をお知らせ

🔮 占術別診断
  四柱推命・算命学・西洋占星術・
  数秘術・紫微斗数の5占術の結果を
  スコア付きで一覧できます

📊 12年間の運勢推移
  過去2年〜未来10年の運気を
  グラフ形式で可視化

📈 グラフ表示
  5占術の全体運を折れ線グラフで
  いつでもチェックできます

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

    if text == "誕生日変更":
        user["state"] = "waiting_birthday"
        reply_msg(event.reply_token, "新しい生年月日を入力してください。\n（例: 1990年3月15日）")
        return

    if user["state"] == "waiting_birthday" or not user.get("birthday"):
        birthday = parse_birthday(text)
        if birthday:
            user["birthday"] = birthday
            user["state"] = "menu"
            reply_msg(event.reply_token, f"✨ {birthday} で登録しました！\n\nメニューからお選びください。", with_menu=True)
        else:
            reply_msg(event.reply_token, "生年月日の形式を認識できませんでした。\n\n以下の形式でご入力ください：\n・1990年3月15日\n・1990/3/15\n・1990-3-15")
        return

    birthday = user["birthday"]

    loading_msgs = {
        "今日の運勢": "📅 今日の運勢を占い中です...\nしばらくお待ちください 🌙",
        "今月の運勢": "📆 今月の運勢を計算中です...\nしばらくお待ちください 🌕",
        "占術別診断": "🔮 5つの占術で診断中です...\nしばらくお待ちください ✨",
        "12年の推移": "📊 12年間の運勢推移を計算中です...\nしばらくお待ちください 🌌",
    }
    fortune_map = {
        "今日の運勢": "daily", "今月の運勢": "monthly",
        "占術別診断": "divination", "12年の推移": "yearly",
    }

    if text == "グラフ表示":
        base = bot_base_url()
        if base:
            b_iso = birthday_to_iso(birthday)
            url = f"{base}/graph?b={b_iso}"
            reply_msg(event.reply_token, f"📈 運勢グラフはこちら：\n{url}\n\nブックマークしておくといつでも確認できます。\n※初回は読み込みに10〜20秒かかります。", with_menu=True)
        else:
            reply_msg(event.reply_token, "⚠️ グラフURLを取得できませんでした。", with_menu=True)
        return

    if text in fortune_map:
        reply_msg(event.reply_token, loading_msgs[text])
        threading.Thread(target=fortune_thread, args=(user_id, birthday, fortune_map[text]), daemon=True).start()
    else:
        try:
            resp = claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system="""あなたは「星夜堂（せいやどう）」の占い師AIです。四柱推命・算命学・占星術・数秘術・紫微斗数を専門とする神秘的な占いブランドです。・丁寧で神秘的な口調（「〜でございます」「〜かと存じます」）・星・月・夜をイメージした言葉を自然に使う・相手の気持ちに寄り添い前向きなメッセージを伝える。返答は200文字以内で。""",
                messages=[{"role": "user", "content": text}],
            )
            reply_text = resp.content[0].text
        except Exception:
            reply_text = "申し訳ございません。只今、星の導きが乱れております。しばらくお待ちくださいませ。🌙"
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


@app.route("/graph")
def graph_page():
    return render_template_string(GRAPH_HTML)


@app.route("/api/graph-data")
def api_graph_data():
    b = request.args.get("b", "").strip()
    if not b:
        return jsonify({"error": "birthday param 'b' required (YYYY-MM-DD)"}), 400
    data = get_graph_data_cached(b)
    if data:
        return jsonify(data)
    return jsonify({"error": "data generation failed"}), 500


@app.route("/", methods=["GET"])
def health_check():
    return "星夜堂 LINE Bot is running ✨"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
