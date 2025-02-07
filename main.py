import telegram
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes, CallbackContext
import httpx
import json
import os
import asyncio
import uuid
import re
import time
import random
import base64
import pickle
from telegram.error import NetworkError, TimedOut, TelegramError

# --- 配置部分 ---
TELEGRAM_BOT_TOKEN = "7598188:AAuNGPI"  # 替换为你的机器人 Token
DIFY_API_URL = "http://192"  # 替换为你的 Dify API 地址
HTTP_PROXY = "http://127.0.0.1:10808"  # 如果需要，设置 HTTP 代理

API_KEYS = {
    "dave": "app-YhesWAl62s7miX",  # 替换为你的 Dify API 密钥
    "dean": "app-o7m7R3g587g",  # 替换为你的 Dify API 密钥
}

DEFAULT_API_KEY_ALIAS = "dave"

# --- 代码部分 ---

message_queue = asyncio.Queue()
rate_limit = 30  # 速率限制，单位：秒
user_last_processed_time = {}
segment_regex = r".*?[。？！~…]+|.+$"  # 分段正则表达式

SUPPORTED_DOCUMENT_MIME_TYPES = [
    "text/plain", "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]

DATA_FILE = "bot_data.pickle"

# 新增：连接状态标记
is_connected = True

def load_data():
    """加载保存的会话数据和 API 密钥。"""
    try:
        with open(DATA_FILE, "rb") as f:
            data = pickle.load(f)
            conversation_ids_by_key = data.get('conversation_ids_by_key', {})
            api_keys = data.get('api_keys', API_KEYS)
            user_api_keys = data.get('user_api_keys', {})
            return conversation_ids_by_key, api_keys, user_api_keys
    except (FileNotFoundError, EOFError, pickle.UnpicklingError) as e:
        print(f"Error loading data from {DATA_FILE}: {e}, using default values.")
        return {}, API_KEYS, {}
    except Exception as e:
        print(f"Unexpected error loading from pickle: {e}")
        return {}, API_KEYS, {}


def save_data(conversation_ids_by_key, api_keys, user_api_keys):
    """保存会话数据和 API 密钥。"""
    data = {
        'conversation_ids_by_key': conversation_ids_by_key,
        'api_keys': api_keys,
        'user_api_keys': user_api_keys
    }
    with open(DATA_FILE, "wb") as f:
        pickle.dump(data, f)

conversation_ids_by_key, api_keys, user_api_keys = load_data()


def get_user_api_key(user_id: str):
    """获取用户当前使用的 API Key 和别名。"""
    alias = user_api_keys.get(user_id, DEFAULT_API_KEY_ALIAS)
    return api_keys.get(alias, api_keys[DEFAULT_API_KEY_ALIAS]), alias


async def set_api_key(update: telegram.Update, context: CallbackContext):
    """设置用户使用的 Dify API Key。"""
    user_id = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("请提供 API Key 的别名，例如：/set dave")
        return
    alias = context.args[0].lower()
    if alias in api_keys:
        user_api_keys[user_id] = alias
        save_data(conversation_ids_by_key, api_keys, user_api_keys)
        await update.message.reply_text(f"你的 Dify API Key 已切换为：{alias}")
    else:
        await update.message.reply_text(f"未找到名为 '{alias}' 的 API Key。")

def segment_text(text, segment_regex):
    """将文本分段，以便逐段发送。"""
    segments = re.findall(segment_regex, text, re.S)
    return [segment.strip() for segment in segments if segment.strip()]

async def upload_file_to_dify(file_bytes, file_name, mime_type, user_id):
    """上传文件到 Dify。"""
    current_api_key, _ = get_user_api_key(user_id)
    headers = {"Authorization": f"Bearer {current_api_key}"}
    files = {'file': (file_name, file_bytes, mime_type), 'user': (None, str(user_id))}
    upload_url = DIFY_API_URL + "/files/upload"
    print(f"文件上传 URL: {upload_url}")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
                response = await client.post(upload_url, headers=headers, files=files)
                if response.status_code == 201:
                    return response.json()
                else:
                    print(f"Error uploading file: {response.status_code}, {response.text}")
                    return None
        except (httpx.RequestError, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            print(f"文件上传失败 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                print("达到最大重试次数。")
                return None
            await asyncio.sleep(5)

async def dify_stream_response(user_message: str, chat_id: int, bot: telegram.Bot, files=None) -> None:
    """向 Dify 发送消息并处理流式响应。"""
    user_id = str(chat_id)
    current_api_key, current_api_key_alias = get_user_api_key(user_id)
    current_conversation_ids = conversation_ids_by_key.get(current_api_key_alias, {})
    conversation_id = current_conversation_ids.get(str(chat_id))

    headers = {"Authorization": f"Bearer {current_api_key}"}
    data = {"inputs": {}, "query": user_message, "user": str(chat_id), "response_mode": "streaming", "files": files if files else []}
    if conversation_id:
        data["conversation_id"] = conversation_id
        print(f"Continuing conversation: {chat_id=}, {conversation_id=}")
    else:
        print(f"Starting new conversation: {chat_id=}")
    full_text_response = ""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
                response = await client.post(DIFY_API_URL + "/chat-messages", headers=headers, json=data)
                if response.status_code == 200:
                    print(f"Dify API status code: 200 OK")
                    first_chunk_received = False
                    async for chunk in response.aiter_lines():
                        if chunk.strip() == "":
                            continue
                        if chunk.startswith("data:"):
                            try:
                                response_data = json.loads(chunk[5:])
                                event = response_data.get("event")
                                if not first_chunk_received:
                                    first_chunk_received = True
                                    response_conversation_id = response_data.get("conversation_id")
                                    if response_conversation_id:
                                        current_conversation_ids[str(chat_id)] = response_conversation_id
                                        conversation_ids_by_key[current_api_key_alias] = current_conversation_ids
                                        save_data(conversation_ids_by_key, api_keys, user_api_keys)
                                        print(f"Stored conversation_id: {response_conversation_id}")
                                    else:
                                        print("Warning: conversation_id not found in the first chunk!")
                                if event == "message_file":
                                    file_url, file_type = response_data.get("url"), response_data.get("type")
                                    if file_url and file_type == "image":
                                        await bot.send_photo(chat_id=chat_id, photo=file_url)
                                        print(f"Sent image: {file_url}")
                                elif event == "tts_message":
                                    audio_base64 = response_data.get("audio")
                                    if audio_base64:
                                        audio_bytes = base64.b64decode(audio_base64)
                                        try:
                                            await bot.send_voice(chat_id=chat_id, voice=bytes(audio_bytes))
                                            print("Sent voice.")
                                        except Exception as voice_err:
                                            print(f"Error sending voice message: {voice_err}")
                                            print("Trying as document...")
                                            try:
                                                await bot.send_document(chat_id=chat_id, document=bytes(audio_bytes), filename="dify_voice.mp3", caption="Dify voice (download to play)")
                                                print("Sent as document.")
                                            except Exception as doc_err:
                                                print(f"Error sending document as voice: {doc_err}")
                                elif event == "message":
                                    text_chunk = response_data.get("answer", "")
                                    if text_chunk:
                                        full_text_response += text_chunk
                            except json.JSONDecodeError as e:
                                print(f"JSONDecodeError: {e}")
                                print(f"Invalid chunk: {chunk}")
                        else:
                            print(f"Non-data chunk received: {chunk}")
                    segments = segment_text(full_text_response, segment_regex)
                    for i, segment in enumerate(segments):
                        print(f"Segment to Send: {segment}")
                        await bot.send_message(chat_id=chat_id, text=segment)
                        if i < len(segments) - 1:
                            delay = random.uniform(1, 3)
                            print(f"Waiting for {delay:.2f}s")
                            await asyncio.sleep(delay)
                    return
                else:
                    print(f"Dify API status code: {response.status_code} Error")
                    error_message = f"Dify API request failed with status code: {response.status_code}"
                    try:
                        error_details = response.json()
                        error_message += f", Details: {error_details}"
                        print(f"Error details: {error_details}")
                    except (httpx.HTTPError, json.JSONDecodeError):
                        error_message += ", Could not decode error response."
                        print("Could not decode error response.")
                    await bot.send_message(chat_id=chat_id, text=error_message)
                    break
        except (httpx.RequestError, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            print(f"Dify API 请求失败 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                await bot.send_message(chat_id=chat_id, text=f"与 Dify API 通信失败。")
                print("达到最大重试次数。")
                return
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Unexpected error: {e}")
            await bot.send_message(chat_id=chat_id, text=f"发生意外错误: {e}")
            return

async def handle_message(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理传入的 Telegram 消息。"""
    message, chat_id, bot = update.message, update.effective_chat.id, context.bot
    if message.document:
        if message.document.mime_type not in SUPPORTED_DOCUMENT_MIME_TYPES:
            await bot.send_message(chat_id=chat_id, text="这个文件类型我打不开哦，抱歉。")
            return
    message_type, message_content, file_info = "unknown", None, None
    if message.text:
        message_type, message_content = "text", message.text
    elif message.photo:
        message_type, message_content = "photo", message.caption if message.caption else "看看这张图片"
        file_info = {"file_id": message.photo[-1].file_id, "file_type": "image", "file_name": f"photo_{uuid.uuid4()}.jpg", "mime_type": "image/jpeg"}
    elif message.voice:
        message_type, message_content = "voice", message.caption if message.caption else "语音消息"
        file_info = {"file_id": message.voice.file_id, "file_type": "audio", "file_name": f"voice_{uuid.uuid4()}.ogg", "mime_type": "audio/ogg"}
    elif message.document:
        message_type, message_content = "document", message.caption if message.caption else "看看这个文件"
        file_info = {"file_id": message.document.file_id, "file_type": "document", "file_name": message.document.file_name or f"document_{uuid.uuid4()}", "mime_type": message.document.mime_type}
    elif message.sticker:
        message_type, message_content = "sticker", "用户发送了一个表情"
    await message_queue.put((update, context, message_type, message_content, file_info))
    print(f"消息已加入队列: 类型: {message_type}, 来自用户: {update.effective_user.id}，chat_id: {update.effective_chat.id}")

async def process_message_queue(application: Application):
    """处理消息队列中的消息。"""
    print("process_message_queue started")
    while True:
        update, context, message_type, message_content, file_info = await message_queue.get()
        user_id, chat_id, bot = str(update.effective_user.id), update.effective_chat.id, context.bot

        # 检查连接状态
        if not is_connected:
            print("Telegram 连接断开，消息处理暂停。")
            await message_queue.put((update, context, message_type, message_content, file_info))
            message_queue.task_done()
            await asyncio.sleep(1)
            continue

        # 记录消息到达时间 (用于基本速率限制)
        message_arrival_time = time.time()
        last_processed_time = user_last_processed_time.get(user_id, 0)

        if message_arrival_time - last_processed_time < rate_limit:
            remaining_time = rate_limit - (message_arrival_time - last_processed_time)
            print(f"用户 {user_id} 触发基本速率限制, 剩余等待时间: {remaining_time:.2f} 秒")
            await asyncio.sleep(remaining_time)

        # --- 核心逻辑：合并队列中该用户的所有消息 ---
        collected_text = ""
        collected_files = []

        # 首先处理当前消息
        if message_type == "sticker":
            await bot.send_message(chat_id=chat_id, text="看不懂你发的啥意思耶")
        elif message_type == "text":
            collected_text += (message_content if message_content else "") + "\n"
        elif message_type in ("photo", "voice", "document"):
            if message_content:
                collected_text += message_content + "\n"  # 添加 caption
            try:
                if message_type == "photo":
                    file = await bot.get_file(file_info['file_id'])
                    file_bytes = await file.download_as_bytearray()
                    file_info['file_name'] = f"photo_{uuid.uuid4()}.jpg"
                elif message_type == "voice":
                    file = await bot.get_file(file_info['file_id'])
                    file_bytes = await file.download_as_bytearray()
                elif message_type == "document":
                    file = await bot.get_file(file_info['file_id'])
                    file_bytes = await file.download_as_bytearray()
                upload_result = await upload_file_to_dify(bytes(file_bytes), file_info['file_name'], file_info['mime_type'], user_id)
                if upload_result and upload_result.get("id"):
                    collected_files.append({"type": file_info['file_type'], "transfer_method": "local_file", "upload_file_id": upload_result["id"]})
            except Exception as e:
                print(f"文件上传/处理错误: {e}")
                await bot.send_message(chat_id=chat_id, text="处理文件时出错。")
                #  continue  <--  这里不能 continue，因为后面还要检查队列

        # 然后，循环检查并合并后续消息
        while not message_queue.empty():
            try:
                next_update, next_context, next_message_type, next_message_content, next_file_info = message_queue.get_nowait()
                if str(next_update.effective_user.id) == user_id:
                    if next_message_type == "sticker":
                        await bot.send_message(chat_id=chat_id, text="看不懂你发的啥意思耶")
                    elif next_message_type == "text":
                        collected_text += (next_message_content if next_message_content else "") + "\n"
                    elif next_message_type in ("photo", "voice", "document"):
                        if next_message_content:
                            collected_text += next_message_content + "\n"
                        try:
                            if next_message_type == "photo":
                                file = await bot.get_file(next_file_info['file_id'])
                                file_bytes = await file.download_as_bytearray()
                                next_file_info['file_name'] = f"photo_{uuid.uuid4()}.jpg"
                            elif next_message_type == "voice":
                                file = await bot.get_file(next_file_info['file_id'])
                                file_bytes = await file.download_as_bytearray()
                            elif next_message_type == "document":
                                file = await bot.get_file(next_file_info['file_id'])
                                file_bytes = await file.download_as_bytearray()
                            upload_result = await upload_file_to_dify(bytes(file_bytes), next_file_info['file_name'], next_file_info['mime_type'], user_id)
                            if upload_result and upload_result.get("id"):
                                collected_files.append({"type": next_file_info['file_type'], "transfer_method": "local_file", "upload_file_id": upload_result["id"]})
                        except Exception as e:
                            print(f"文件上传/处理错误: {e}")
                            await bot.send_message(chat_id=chat_id, text="处理文件时出错。")
                            # continue  <-- 这里也不能 continue

                    message_queue.task_done()
                else:
                    await message_queue.put((next_update, next_context, next_message_type, next_message_content, next_file_info))
                    break
            except asyncio.QueueEmpty:
                break

        if collected_text.strip() or collected_files:
            print(f"合并消息: {collected_text}, 文件: {collected_files}")
            await dify_stream_response(collected_text.strip(), chat_id, bot, files=collected_files)

        user_last_processed_time[user_id] = time.time()  # 更新时间戳
        message_queue.task_done()

async def start(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 /start 命令。"""
    welcome_message = """
你好呀！

你可以向我发送文本、图片、语音或文档，我会尽力理解你的意思。

如果你想切换聊天对象，可以使用 /set 命令，例如：/set dave

准备好开始聊天了吗？😊
    """
    await update.message.reply_text(welcome_message)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """错误处理程序。"""
    print(f"Exception while handling an update: {context.error}")
    if update and update.effective_chat:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="处理消息时发生了一些错误。")


async def check_queue_size(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """检查消息队列大小。"""
    print(f"当前队列大小 (手动检查): {message_queue.qsize()}")
    await update.message.reply_text(f"当前队列大小: {message_queue.qsize()}")

async def check_connection(application: Application) -> bool:
    """心跳检测：尝试获取机器人的信息。"""
    global is_connected
    try:
        # 使用 get_me() 方法，这是一个轻量级的检查连接的方法
        await application.bot.get_me()
        if not is_connected:
            print("Telegram 连接恢复!")
        is_connected = True  # 连接正常
        return True
    except TelegramError as e:
        print(f"心跳检测失败: {e}")
        is_connected = False  # 连接可能断开
        return False
    except Exception as e:
        print(f"心跳检测期间发生意外错误: {e}")
        is_connected = False
        return False

async def connect_telegram():
    """连接 Telegram 机器人并处理断线重连。"""
    global is_connected
    while True:
        try:
            application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("set", set_api_key))
            application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
            application.add_error_handler(error_handler)
            application.add_handler(CommandHandler("check_queue", check_queue_size))

            async with application:
                await application.start()
                await application.updater.start_polling()

                # 启动消息处理队列
                asyncio.create_task(process_message_queue(application))

                print("Bot started. Press Ctrl+C to stop.")

                # 主循环：定期进行心跳检测
                while True:
                    if not await check_connection(application):
                        print("检测到 Telegram 连接断开，尝试重新连接...")
                        await application.updater.stop()
                        await application.stop()

                        break  # 退出内层循环，重新连接
                    await asyncio.sleep(30)  # 每 30 秒进行一次心跳检测

        except (NetworkError, TimedOut) as e:
            print(f"Telegram 连接错误: {e}")
            print("尝试重新连接...")
            is_connected = False  # 设置连接状态为断开
            await asyncio.sleep(10)

        except asyncio.CancelledError:
            print("Stopping the bot...")
            if 'application' in locals():
                save_data(conversation_ids_by_key, api_keys, user_api_keys)
                await application.updater.stop()
                await application.stop()
            break

        except Exception as e:
            print(f"Unexpected error: {e}")
            is_connected = False
            if 'application' in locals():
                save_data(conversation_ids_by_key, api_keys, user_api_keys)
                await application.updater.stop()
                await application.stop()
            break

async def main() -> None:
    """主函数。"""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("请设置 TELEGRAM_BOT_TOKEN")
        return
    if not DIFY_API_URL or DIFY_API_URL == "YOUR_DIFY_API_URL":
        print("请设置 DIFY_API_URL")
        return

    await connect_telegram()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        save_data(conversation_ids_by_key, api_keys, user_api_keys)
