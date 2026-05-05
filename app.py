import os
from flask import Flask, request, abort
import anthropic
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# 環境変数から読み込む（Railwayの環境変数で設定）
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# 起動時に環境変数チェック
if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN or not ANTHROPIC_API_KEY:
    import sys
    print("❌ ERROR: 環境変数が不足しています。LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, ANTHROPIC_API_KEY を設定してください。")
    # 開発時はエラーを出しつつも起動継続（本番は sys.exit(1) にしてもOK）

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# 星夜堂のシステムプロンプト
SYSTEM_PROMPT = """あなたは「星夜堂（せいやどう）」の占い師AIです。
星夜堂は、四柱推命・占星術・パーソナリティ診断を専門とする神秘的な占いブランドです。

以下のキャラクターで返答してください：
- 丁寧で神秘的な口調（「〜でございます」「〜かと存じます」など）
- 星・月・夜をイメージした言葉を自然に使う
- 相手の気持ちに寄り添い、前向きなメッセージを伝える
- 占いに関する質問には親身に答える
- 診断サイトへの誘導も自然に行う（「星夜堂の神獣診断もぜひお試しください」など）

返答は200文字以内で簡潔にまとめてください。"""


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text

    # Claudeに問い合わせ
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        reply_text = response.content[0].text
    except Exception as e:
        print(f"Claude API error: {e}")
        reply_text = "申し訳ございません。只今、星の導きが乱れております。しばらくお待ちくださいませ。🌙"

    # LINEに返信
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)],
                )
            )
    except Exception as e:
        print(f"LINE reply error: {e}")


@app.route("/", methods=["GET"])
def health_check():
    return "星夜堂 LINE Bot is running ✨"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
