import telegram
import telegram
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    CommandHandler,
    ContextTypes,
    CallbackContext,
    ConversationHandler,
    CallbackQueryHandler  # 确保这一行存在
)
import httpx
import json
import os
import sys
import asyncio
import uuid
import re
import time
import random
import base64
import aiosqlite
import pickle
from telegram.error import NetworkError, TimedOut, TelegramError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# --- 配置部分 ---
TELEGRAM_BOT_TOKEN = "7522JHCMFjJY"  # 替换为你的 Telegram Bot Token
DIFY_API_URL = "http://192.1"  # 替换为你的 Dify API URL
HTTP_PROXY = "http://127.0.0.1:10808"  # 替换为你的代理（如果需要）  # 未使用，但保留
ADMIN_IDS = ["603"]  # 替换为你的管理员 ID，可以有多个
API_KEYS = {
    "dave": "a",
    "dean": "ap587g",
}


DEFAULT_API_KEY_ALIAS = "dave"


# --- 代码部分 ---
message_queue = asyncio.Queue()
rate_limit = 20  # 基础速率限制（秒）
user_last_processed_time = {}
segment_regex = r".*?[。？！~…]+|.+$"

SUPPORTED_DOCUMENT_MIME_TYPES = [
    "text/plain", "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]

DATA_FILE = "bot_data.pickle"

is_connected = True  # 全局变量，用于跟踪 Telegram 连接状态
telegram_application = None  # 全局变量，用于存储 Application 实例

# 添加数据库常量
DB_FILE = "chat_memory.db"

# 在文件开头的全局变量部分添加
last_user_message = ""
last_assistant_response = ""

# 在全局变量部分修改 conversation_history 的结构
# 从 user_id -> messages 改为 (user_id, api_key_alias) -> messages
conversation_history = {}

# 在全局变量部分添加 is_importing_memory
is_importing_memory = False

# 修改全局变量部分，将 conversation_ids_by_user 改为按角色存储
# 从 user_id -> conversation_id 改为 (user_id, api_key_alias) -> conversation_id
conversation_ids_by_user = {}

# 添加一个全局变量来跟踪每个用户的导入状态
user_importing_memory = {}


def load_data():
    """加载保存的会话数据和 API 密钥。"""
    global API_KEYS  # 声明使用全局变量
    try:
        with open(DATA_FILE, "rb") as f:
            data = pickle.load(f)
            conversation_ids_by_user = data.get('conversation_ids_by_user', {})
            loaded_api_keys = data.get('api_keys', {})
            # 合并已保存的和新的 API keys
            API_KEYS.update(loaded_api_keys)  # 保留新添加的 keys
            user_api_keys = data.get('user_api_keys', {})
            blocked_users = data.get('blocked_users', set())
            return conversation_ids_by_user, API_KEYS, user_api_keys, blocked_users
    except (FileNotFoundError, EOFError, pickle.UnpicklingError) as e:
        print(f"Error loading data from {DATA_FILE}: {e}, using default values.")
        return {}, API_KEYS, {}, set()
    except Exception as e:
        print(f"Unexpected error loading from pickle: {e}")
        return {}, API_KEYS, {}, set()


def save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users):
    """保存会话数据和 API 密钥。"""
    data = {
        'conversation_ids_by_user': conversation_ids_by_user,
        'api_keys': api_keys,
        'user_api_keys': user_api_keys,
        'blocked_users': blocked_users,  # 保存黑名单
    }
    try:
        with open(DATA_FILE, "wb") as f:
            pickle.dump(data, f)
    except Exception as e:
        print(f"Error saving data to {DATA_FILE}: {e}")


conversation_ids_by_user, api_keys, user_api_keys, blocked_users = load_data()


def get_user_api_key(user_id: str):
    """获取用户当前使用的 API Key 和别名。"""
    alias = user_api_keys.get(user_id, DEFAULT_API_KEY_ALIAS)
    return api_keys.get(alias, api_keys[DEFAULT_API_KEY_ALIAS]), alias


async def set_api_key(update: telegram.Update, context: CallbackContext):
    """设置用户使用的 Dify API Key。"""
    user_id = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("想换个人聊天？告诉我它的名字，比如：/set dave")
        return
    alias = context.args[0].lower()
    if alias in api_keys:
        old_alias = user_api_keys.get(user_id)
        user_api_keys[user_id] = alias  # 更新为新的 alias
        
        # 不清除对话ID，让每个角色保持自己的对话
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
        await update.message.reply_text(f"好嘞，让 {alias} 来跟你聊吧！")
    else:
        await update.message.reply_text(f"呃，我不认识叫 '{alias}' 的家伙。")


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
            async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
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
            await asyncio.sleep(5)  # 等待一段时间后重试


async def dify_stream_response(user_message: str, chat_id: int, bot: telegram.Bot, files=None) -> None:
    """向 Dify 发送消息并处理流式响应。"""
    global conversation_history, is_importing_memory
    user_id = str(chat_id)
    current_api_key, current_api_key_alias = get_user_api_key(user_id)
    history_key = (user_id, current_api_key_alias)
    conversation_key = (user_id, current_api_key_alias)
    
    # 初始化对话历史
    if history_key not in conversation_history:
        conversation_history[history_key] = []
    
    # 只有在不是导入记忆时才记录用户消息
    if not is_importing_memory:
        conversation_history[history_key].append(f"user: {user_message}")
    
    # 使用组合键获取当前角色的对话ID
    conversation_id = conversation_ids_by_user.get(conversation_key)

    headers = {"Authorization": f"Bearer {current_api_key}"}
    data = {"inputs": {}, "query": user_message, "user": str(chat_id), "response_mode": "streaming",
            "files": files if files else []}
    if conversation_id:
        data["conversation_id"] = conversation_id
        print(f"Continuing conversation: {chat_id=}, {conversation_id=}, role={current_api_key_alias}")
    else:
        print(f"Starting new conversation: {chat_id=}, role={current_api_key_alias}")
    full_text_response = ""
    max_retries = 3  # 限制最大重试次数
    retry_count = 0
    consecutive_404_count = 0  # 连续 404 错误的计数器
    timeout_seconds = 60  # 设置流式响应的超时时间

    while retry_count <= max_retries:
        last_chunk_time = time.time()  # 记录接收到最后一个数据块的时间
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
                response = await client.post(DIFY_API_URL + "/chat-messages", headers=headers, json=data)

                if response.status_code == 200:
                    print(f"Dify API status code: 200 OK")
                    first_chunk_received = False
                    empty_response_count = 0
                    consecutive_404_count = 0  # 重置 404 计数器
                    async for chunk in response.aiter_lines():
                        if chunk.strip() == "":
                            continue

                        last_chunk_time = time.time()  # 更新时间

                        if chunk.startswith("data:"):
                            try:
                                response_data = json.loads(chunk[5:])
                                event = response_data.get("event")
                                if not first_chunk_received:
                                    first_chunk_received = True
                                    response_conversation_id = response_data.get("conversation_id")
                                    if response_conversation_id:
                                        # 使用组合键存储对话ID
                                        conversation_ids_by_user[conversation_key] = response_conversation_id
                                        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                        print(f"Stored/Updated conversation_id: {response_conversation_id} for user: {user_id}, role: {current_api_key_alias}")
                                    else:
                                        print("Warning: conversation_id not found in the first chunk!")

                                if event == "message_file":
                                    file_url, file_type = response_data.get("url"), response_data.get("type")
                                    if file_url and file_type == "image":
                                        await bot.send_photo(chat_id=chat_id, photo=file_url)
                                        print(f"Sent image: {file_url}")
                                        empty_response_count = 0
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
                                                await bot.send_document(chat_id=chat_id, document=bytes(audio_bytes),
                                                                        filename="dify_voice.mp3",
                                                                        caption="Dify voice (download to play)")
                                                print("Sent as document.")
                                            except Exception as doc_err:
                                                print(f"Error sending document as voice: {doc_err}")
                                        empty_response_count = 0
                                elif event == "message":
                                    text_chunk = response_data.get("answer", "")
                                    if text_chunk:
                                        full_text_response += text_chunk
                                        empty_response_count = 0
                                    else:
                                        print("Warning: Received 'message' event with no answer.")
                                        empty_response_count += 1
                                elif event == "error":
                                    print("Received event: error, clearing conversation_id and informing user.")
                                    # 提供保存记忆的选项
                                    keyboard = [
                                        [
                                            InlineKeyboardButton("是", callback_data=f"save_memory_{conversation_ids_by_user.get(conversation_key, 'new')}"),
                                            InlineKeyboardButton("否", callback_data="new_conversation")
                                        ]
                                    ]
                                    reply_markup = InlineKeyboardMarkup(keyboard)
                                    await bot.send_message(
                                        chat_id=chat_id,
                                        text="当前对话已过期，是否保存记忆？",
                                        reply_markup=reply_markup
                                    )
                                    if conversation_key in conversation_ids_by_user:
                                        del conversation_ids_by_user[conversation_key]
                                        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                    return
                                else:
                                    print(f"Received event: {event}")
                                    empty_response_count += 1
                            except json.JSONDecodeError as e:
                                print(f"JSONDecodeError: {e}")
                                print(f"Invalid chunk: {chunk}")
                                empty_response_count += 1

                            if empty_response_count >= 3:
                                print("Warning: Received multiple consecutive empty responses. Breaking the loop.")
                                break

                            # 检查流式响应超时
                            if time.time() - last_chunk_time > timeout_seconds:
                                print("Timeout during streaming. No data received.")
                                response.close()  # 手动关闭连接
                                raise httpx.ReadTimeout("No data received during streaming.")

                        else:
                            print(f"Non-data chunk received: {chunk}")

                    if full_text_response.strip():
                        # 记录助手回复，导入记忆时也要记录
                        conversation_history[history_key].append(f"assistant: {full_text_response}")
                        segments = segment_text(full_text_response, segment_regex)
                        for i, segment in enumerate(segments):
                            await bot.send_message(chat_id=chat_id, text=segment)
                            if i < len(segments) - 1:
                                delay = random.uniform(1, 3)
                                print(f"Waiting for {delay:.2f}s")
                                await asyncio.sleep(delay)  # 控制消息发送间隔
                    else:
                        # 空回复时提供保存记忆选项
                        keyboard = [
                            [
                                InlineKeyboardButton("是", callback_data=f"save_memory_{conversation_ids_by_user.get(conversation_key, 'new')}"),
                                InlineKeyboardButton("否", callback_data="new_conversation")
                            ]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await bot.send_message(
                            chat_id=chat_id,
                            text="当前对话已过期，是否保存记忆？",
                            reply_markup=reply_markup
                        )
                        # 清除当前对话ID
                        if conversation_key in conversation_ids_by_user:
                            del conversation_ids_by_user[conversation_key]
                            save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                    return

                elif response.status_code == 404:
                    error_details = response.json()
                    if error_details.get('code') == 'not_found' and error_details.get(
                            'message') == 'Conversation Not Exists.':
                        consecutive_404_count += 1  # 增加 404 计数
                        print(f"Received 404 Conversation Not Exists for user {user_id}.")

                        if consecutive_404_count >= 2:  # 连续两次或更多 404
                            print(f"Clearing conversation_id for user {user_id} due to consecutive 404 errors.")
                            if conversation_key in conversation_ids_by_user:
                                del conversation_ids_by_user[conversation_key]
                                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                            await bot.send_message(chat_id=chat_id, text="请重新给我发消息吧。")
                            return  # 直接返回, 不再重试

                        await bot.send_message(chat_id=chat_id, text="哎呀，我换新手机了，之前的聊天记录搞丢了，你刚刚发的消息也没收到耶。")
                        retry_count += 1
                        print(f"Retrying with conversation_id, attempt {retry_count}...")
                        continue

                    else:
                        print(f"Dify API status code: {response.status_code} Error")
                        error_message = f"好像出了点错误（{response.status_code}），稍后再试试吧。"
                        error_message += f" 详细信息：{error_details}"
                        await bot.send_message(chat_id=chat_id, text=error_message)
                    break

                else:
                    print(f"Dify API status code: {response.status_code} Error")
                    error_message = f"哎呀，好像出了点错误（{response.status_code}），稍后再试试吧。"
                    try:
                        error_details = response.json()
                        error_message += f" 详细信息：{error_details}"
                    except (httpx.HTTPError, json.JSONDecodeError):
                        error_message += " 无法获取详细错误信息。"
                    await bot.send_message(chat_id=chat_id, text=error_message)
                    break


        except (httpx.RequestError, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
            print(f"Dify API 请求失败 (尝试 {retry_count + 1}/{max_retries}): {e}")
            if retry_count == max_retries - 1:
                await bot.send_message(chat_id=chat_id,
                                       text=f"网络好像不太好，试了好几次都没发出去消息，稍后再试试吧。")
                print("达到最大重试次数。")
                return
            await asyncio.sleep(5)
            retry_count += 1

        except Exception as e:
            print(f"Unexpected error: {e}")
            await bot.send_message(chat_id=chat_id, text=f"发生了一些奇怪的错误：{e}，稍后再联系吧！")
            return


async def handle_message(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理传入的 Telegram 消息。"""
    global conversation_history
    
    user_id = str(update.effective_user.id)

    # --- 黑名单检查 ---
    if user_id in blocked_users:
        print(f"用户 {user_id} 在黑名单中，消息被忽略。")
        return  # 直接返回，不处理消息

    message = update.message
    chat_id = update.effective_chat.id
    bot = context.bot

    # 检查文件类型
    if message.document:
        if message.document.mime_type not in SUPPORTED_DOCUMENT_MIME_TYPES:
            await bot.send_message(chat_id=chat_id, text="这个文件我打不开呀，抱歉啦。")  # 更自然的拒绝
            return

    # 确定消息类型和内容
    message_type = "unknown"
    message_content = None
    file_info = None

    if message.text:
        message_type = "text"
        message_content = message.text
    elif message.photo:
        message_type = "photo"
        message_content = message.caption if message.caption else "看看这张图片"  # 图片可以有标题
        file_info = {"file_id": message.photo[-1].file_id, "file_type": "image", "file_name": f"photo_{uuid.uuid4()}.jpg",
                     "mime_type": "image/jpeg"}
    elif message.voice:
        message_type = "voice"
        message_content = message.caption if message.caption else "语音消息"  # 语音也可以有标题
        file_info = {"file_id": message.voice.file_id, "file_type": "audio", "file_name": f"voice_{uuid.uuid4()}.ogg",
                     "mime_type": "audio/ogg"}
    elif message.document:
        message_type = "document"
        message_content = message.caption if message.caption else "看看这个文件"  # 文件也可以有标题
        file_info = {"file_id": message.document.file_id, "file_type": "document",
                     "file_name": message.document.file_name or f"document_{uuid.uuid4()}",
                     "mime_type": message.document.mime_type}
    elif message.sticker:
        message_type = "sticker"
        message_content = "用户发送了一个表情"  # sticker 没有 caption

    # 将消息加入队列
    await message_queue.put((update, context, message_type, message_content, file_info))
    print(f"消息已加入队列: 类型: {message_type}, 来自用户: {update.effective_user.id}，chat_id: {update.effective_chat.id}")


async def process_message_queue(application: Application):
    """处理消息队列中的消息。"""
    print("process_message_queue started")
    while True:
        try:
            # 1. 从队列中获取一个消息
            update, context, message_type, message_content, file_info = await message_queue.get()
            user_id = str(update.effective_user.id)
            chat_id = update.effective_chat.id
            bot = context.bot
            current_user_queue = []  # 存储当前用户的消息
            
            # 获取当前用户的API key和别名
            current_api_key, current_api_key_alias = get_user_api_key(user_id)
            # 定义组合键
            conversation_key = (user_id, current_api_key_alias)

            if not is_connected:  # 如果 Telegram 连接断开，则暂停处理
                print("Telegram 连接断开，消息处理暂停。")
                await message_queue.put((update, context, message_type, message_content, file_info))  # 放回队列
                message_queue.task_done()
                await asyncio.sleep(1)  # 等待一段时间
                continue

            # 速率限制
            message_arrival_time = time.time()
            last_processed_time = user_last_processed_time.get(user_id, 0)

            if message_arrival_time - last_processed_time < rate_limit:
                remaining_time = rate_limit - (message_arrival_time - last_processed_time)
                print(f"用户 {user_id} 触发基本速率限制, 剩余等待时间: {remaining_time:.2f} 秒")
                await asyncio.sleep(remaining_time)  # 等待

            # 2. 将当前消息添加到当前用户的队列
            current_user_queue.append((update, context, message_type, message_content, file_info))

            # 3. 尽力合并来自同一用户的连续消息
            while not message_queue.empty():
                try:
                    next_update, next_context, next_message_type, next_message_content, next_file_info = message_queue.get_nowait()
                    if str(next_update.effective_user.id) == user_id:
                        current_user_queue.append((next_update, next_context, next_message_type, next_message_content,
                                                  next_file_info))
                        message_queue.task_done()
                    else:
                        # 如果不是来自同一用户的消息，则将其放回队列
                        await message_queue.put(
                            (next_update, next_context, next_message_type, next_message_content, next_file_info))
                        break
                except asyncio.QueueEmpty:
                    break
            # 4. 处理当前用户的消息队列
            collected_text = ""
            collected_files = []

            for update, context, message_type, message_content, file_info in current_user_queue:
                if message_type == "sticker":
                    await bot.send_message(chat_id=chat_id, text="看不懂你发的啥捏")  # 更自然的表情回复
                elif message_type == "text":
                    collected_text += (message_content if message_content else "") + "\n"
                elif message_type in ("photo", "voice", "document"):
                    if message_content:
                        collected_text += message_content + "\n"
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
                        upload_result = await upload_file_to_dify(bytes(file_bytes), file_info['file_name'],
                                                                file_info['mime_type'], user_id)
                        if upload_result and upload_result.get("id"):
                            collected_files.append({"type": file_info['file_type'], "transfer_method": "local_file",
                                                    "upload_file_id": upload_result["id"]})
                    except Exception as e:
                        print(f"文件上传/处理错误: {e}")
                        await bot.send_message(chat_id=chat_id, text="处理文件的时候出了点小问题...")

            # 5. 发送合并后的消息
            try:
                if collected_text.strip() or collected_files:
                    print(f"合并消息: {collected_text}, 文件: {collected_files}")
                    await dify_stream_response(collected_text.strip(), chat_id, bot, files=collected_files)
            except TimedOut as e:
                print(f"Error in process_message_queue during dify_stream_response: {e}")
                # 将消息重新放回队列
                await message_queue.put((update, context, message_type, message_content, file_info))
                # 等待一段时间后继续
                await asyncio.sleep(5)
            except Exception as e:
                print(f"Error in process_message_queue during dify_stream_response: {e}")
                try:
                    await bot.send_message(chat_id=chat_id, text="处理消息时发生错误，请稍后再试。")
                except:
                    pass  # 忽略发送错误消息时的异常

            # 6. 更新最后处理时间
            user_last_processed_time[user_id] = time.time()
            message_queue.task_done()

        except Exception as e:
            print(f"Unexpected error in process_message_queue: {e}")
            await asyncio.sleep(5)  # 出错时等待一段时间
            continue


async def start(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 /start 命令。"""
    welcome_message = """
哈喽！我是你的聊天小助手！

可以给我发文字、图片、语音或者文件哦，我会尽力理解的。

想换个人聊？用 /set 命令，比如：/set dave

准备好跟我聊天了吗？😊
    """
    await update.message.reply_text(welcome_message)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """错误处理程序。"""
    print(f"Exception while handling an update: {context.error}")
    try:
        if update and update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="哎呀，出错了，稍后再找我吧。")  # 更自然的通用错误
    except Exception as e:
        print(f"Error in error handler: {e}")


async def block_user(update: telegram.Update, context: CallbackContext) -> None:
    """拉黑用户（管理员命令）。"""
    user_id = str(update.effective_user.id)
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("你没有权限执行此操作。")
        return

    if not context.args:
        await update.message.reply_text("请指定要拉黑的用户 ID，例如：/block 123456789")
        return

    try:
        target_user_id = str(context.args[0])
        blocked_users.add(target_user_id)  # 添加到黑名单
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)  # 保存
        await update.message.reply_text(f"用户 {target_user_id} 已被拉黑。")
    except (ValueError, KeyError):
        await update.message.reply_text("无效的用户 ID。")


async def unblock_user(update: telegram.Update, context: CallbackContext) -> None:
    """取消拉黑用户（管理员命令）。"""
    user_id = str(update.effective_user.id)
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("你没有权限执行此操作。")
        return

    if not context.args:
        await update.message.reply_text("请指定要取消拉黑的用户 ID，例如：/unblock 123456789")
        return

    try:
        target_user_id = str(context.args[0])
        if target_user_id in blocked_users:
            blocked_users.remove(target_user_id)  # 从黑名单移除
            save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)  # 保存
            await update.message.reply_text(f"用户 {target_user_id} 已被取消拉黑。")
        else:
            await update.message.reply_text(f"用户 {target_user_id} 不在黑名单中。")
    except (ValueError, KeyError):
        await update.message.reply_text("无效的用户 ID。")


async def clean_conversations(update: telegram.Update, context: CallbackContext) -> None:
    """清除所有用户的聊天 ID 记录（管理员命令）。"""
    user_id = str(update.effective_user.id)
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("你没有权限执行此操作。")
        return

    global conversation_ids_by_user  # 声明为全局变量
    conversation_ids_by_user = {}  # 清空字典
    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)  # 保存更改
    await update.message.reply_text("所有用户的聊天 ID 记录已清除。")


async def check_telegram_connection(application: Application):
    """检查 Telegram 连接状态"""
    global is_connected
    while True:
        try:
            await application.bot.get_me()
            if not is_connected:
                print("Telegram connection restored.")
                is_connected = True
                # 重启消息队列处理器
                print("Message queue processor restarted.")
                print("process_message_queue started")
                # 清除所有用户的最后处理时间，确保消息能立即处理
                user_last_processed_time.clear()
        except Exception as e:
            if is_connected:
                print(f"Telegram connection lost: {e}")
                is_connected = False
            else:
                print(f"Telegram connection check error: {e}")
        
        await asyncio.sleep(5)  # 每5秒检查一次


async def load_memories():
    """从数据库加载所有保存的记忆"""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            'SELECT user_id, conversation_id, api_key_alias, chat_content FROM chat_memories'
        ) as cursor:
            memories = await cursor.fetchall()
            for user_id, conversation_id, api_key_alias, chat_content in memories:
                # 使用组合键存储记忆
                history_key = (user_id, api_key_alias)
                conversation_key = (user_id, api_key_alias)
                
                # 恢复对话历史
                conversation_history[history_key] = chat_content.split('\n')
                # 恢复对话ID
                conversation_ids_by_user[conversation_key] = conversation_id


async def init_db():
    """初始化数据库表并加载记忆"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_memories (
                user_id TEXT,
                conversation_id TEXT,
                api_key_alias TEXT,
                chat_content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, conversation_id, api_key_alias)
            )
        ''')
        await db.commit()
    
    # 加载保存的记忆
    await load_memories()


async def connect_telegram():
    """连接 Telegram 机器人。"""
    global is_connected, telegram_application
    retry_delay = 10  # 初始重试延迟（秒）
    max_retry_delay = 300  # 最大重试延迟（5分钟）

    while True:  # 无限重试循环
        try:
            if telegram_application is None:
                telegram_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
                # 添加处理器
                telegram_application.add_handler(CommandHandler("start", start))
                telegram_application.add_handler(CommandHandler("set", set_api_key))
                telegram_application.add_handler(CommandHandler("block", block_user))
                telegram_application.add_handler(CommandHandler("unblock", unblock_user))
                telegram_application.add_handler(CommandHandler("clean", clean_conversations))
                telegram_application.add_handler(CommandHandler("save", save_memory_command))
                telegram_application.add_handler(CallbackQueryHandler(button_callback))
                telegram_application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
                telegram_application.add_error_handler(error_handler)

            async with telegram_application:
                if not telegram_application.running:
                    # 先初始化数据库和加载记忆
                    await init_db()
                    
                    await telegram_application.start()
                    await telegram_application.updater.start_polling()
                    print("Bot started or re-started.")
                    # 清除所有用户的最后处理时间
                    user_last_processed_time.clear()
                    # 启动连接检查和消息处理
                    asyncio.create_task(check_telegram_connection(telegram_application))
                    asyncio.create_task(process_message_queue(telegram_application))
                    is_connected = True
                    retry_delay = 10  # 重置重试延迟

                await asyncio.Future()  # 持续运行，直到被外部取消

        except Exception as e:
            print(f"Connection error: {e}")
            is_connected = False
            if telegram_application:
                try:
                    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                    if telegram_application.running:
                        await telegram_application.updater.stop()
                        await telegram_application.stop()
                except Exception as stop_error:
                    print(f"Error stopping application: {stop_error}")
                telegram_application = None
            
            print(f"Waiting {retry_delay} seconds before reconnecting...")
            await asyncio.sleep(retry_delay)
            # 指数退避重试延迟，但不超过最大值
            retry_delay = min(retry_delay * 2, max_retry_delay)
            print("Attempting to reconnect...")
            continue


async def main() -> None:
    """主函数。"""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("请设置 TELEGRAM_BOT_TOKEN")
        return
    if not DIFY_API_URL or DIFY_API_URL == "YOUR_DIFY_API_URL":
        print("请设置 DIFY_API_URL")
        return
    await connect_telegram()


# 修改 button_callback 函数
async def button_callback(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """处理按钮回调"""
    query = update.callback_query
    user_id = str(update.effective_user.id)
    current_api_key, current_api_key_alias = get_user_api_key(user_id)
    
    # 使用组合键来获取对话历史和对话ID
    history_key = (user_id, current_api_key_alias)
    conversation_key = (user_id, current_api_key_alias)
    
    if query.data.startswith("save_memory_"):
        # 检查这个用户是否正在导入记忆
        if user_importing_memory.get(user_id, False):
            try:
                await query.edit_message_text("请等待当前记忆导入完成后再试。")
            except telegram.error.BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise e
            return
            
        conversation_id = query.data.replace("save_memory_", "")
        
        # 获取该用户当前角色的完整对话历史
        if history_key in conversation_history and conversation_history[history_key]:
            # 标记该用户正在导入记忆
            user_importing_memory[user_id] = True
            
            # 过滤掉包含前缀的行
            filtered_history = [
                line for line in conversation_history[history_key] 
                if not line.startswith("以下是过去的对话历史：")
            ]
            
            # 保存当前的对话历史
            chat_content = "\n".join(filtered_history)
            # 在保存记忆时也包含角色信息
            await save_memory(user_id, conversation_id, chat_content, current_api_key_alias)
            
            try:
                await query.edit_message_text("记忆已保存，正在导入历史对话...")
            except telegram.error.BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise e
            
            # 创建一个异步任务来处理等待和导入记忆
            asyncio.create_task(delayed_memory_import(user_id, conversation_id, current_api_key_alias, update, context))
        else:
            try:
                await query.edit_message_text("没有找到可以保存的对话历史。")
            except telegram.error.BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise e

async def delayed_memory_import(user_id: str, conversation_id: str, api_key_alias: str, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """延迟导入记忆的异步任务"""
    try:
        print("等待30秒以确保数据库操作完成...")
        await asyncio.sleep(30)  # 给数据库足够的时间
        
        # 使用组合键
        history_key = (user_id, api_key_alias)
        conversation_key = (user_id, api_key_alias)
        
        # 使用保存的记忆开始新对话
        memory = await get_memory(user_id, conversation_id, api_key_alias)
        if memory:
            # 清除旧的对话ID
            if conversation_key in conversation_ids_by_user:
                del conversation_ids_by_user[conversation_key]
                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
            
            # 创建一个新的列表来存储记忆，不包含前缀
            memory_lines = memory.split('\n')
            conversation_history[history_key] = memory_lines
            
            # 创建一个临时变量来标记是否应该记录对话
            global is_importing_memory
            is_importing_memory = True
            
            # 确保创建新的对话
            if conversation_key in conversation_ids_by_user:
                del conversation_ids_by_user[conversation_key]
            save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
            
            # 在发送给 dify 时添加前缀
            memory_with_prefix = "以下是过去的对话历史：\n" + memory
            
            # 将记忆导入消息加入消息队列
            await message_queue.put((update, context, "text", memory_with_prefix, None))
            
            # 恢复记录对话
            is_importing_memory = False
    finally:
        # 无论成功与否，都要清除导入状态
        user_importing_memory.pop(user_id, None)

# 修改 test_memory 函数为 save_memory_command
async def save_memory_command(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """保存记忆命令"""
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    current_api_key, current_api_key_alias = get_user_api_key(user_id)
    
    # 使用组合键
    history_key = (user_id, current_api_key_alias)
    conversation_key = (user_id, current_api_key_alias)
    
    # 检查这个用户是否正在导入记忆
    if user_importing_memory.get(user_id, False):
        await context.bot.send_message(chat_id=chat_id, text="请等待当前记忆导入完成后再试。")
        return
    
    # 获取该用户当前角色的完整对话历史
    if history_key in conversation_history and conversation_history[history_key]:
        # 标记该用户正在导入记忆
        user_importing_memory[user_id] = True
        
        # 过滤掉包含前缀的行
        filtered_history = [
            line for line in conversation_history[history_key] 
            if not line.startswith("以下是过去的对话历史：")
        ]
        
        # 保存当前的对话历史
        chat_content = "\n".join(filtered_history)
        conversation_id = conversation_ids_by_user.get(conversation_key, 'new')
        
        # 在保存记忆时也包含角色信息
        await save_memory(user_id, conversation_id, chat_content, current_api_key_alias)
        await context.bot.send_message(chat_id=chat_id, text="记忆已保存，正在导入历史对话...")
        
        # 创建一个异步任务来处理等待和导入记忆
        asyncio.create_task(delayed_memory_import(user_id, conversation_id, current_api_key_alias, update, context))
    else:
        await context.bot.send_message(chat_id=chat_id, text="没有找到可以保存的对话历史。")


async def save_memory(user_id: str, conversation_id: str, chat_content: str, api_key_alias: str):
    """保存对话记忆到数据库"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            'INSERT OR REPLACE INTO chat_memories (user_id, conversation_id, api_key_alias, chat_content) VALUES (?, ?, ?, ?)',
            (user_id, conversation_id, api_key_alias, chat_content)
        )
        await db.commit()
        print("记忆保存成功，等待30秒以确保数据库操作完成...")
        await asyncio.sleep(30)  # 给数据库足够的时间完成操作

async def get_memory(user_id: str, conversation_id: str, api_key_alias: str):
    """从数据库获取对话记忆"""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            'SELECT chat_content FROM chat_memories WHERE user_id = ? AND conversation_id = ? AND api_key_alias = ?',
            (user_id, conversation_id, api_key_alias)
        ) as cursor:
            result = await cursor.fetchone()
            if result:
                print("记忆获取成功，等待30秒以确保数据库操作完成...")
                await asyncio.sleep(30)  # 给数据库和系统足够的时间
                return result[0]
            return None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
