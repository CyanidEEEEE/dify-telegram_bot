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
TELEGRAM_BOT_TOKEN = "759fKtEUA"  # 替换为你的 Telegram Bot Token
DIFY_API_URL = "http://127"  # 替换为你的 Dify API URL
HTTP_PROXY = "http://127"  # 替换为你的代理（如果需要）  # 未使用，但保留
ADMIN_IDS = ["10"]  # 替换为你的管理员 ID，可以有多个
API_KEYS = {
    "dave": "arxV0",
    "dean": "ap7g",
}

DEFAULT_API_KEY_ALIAS = "dave"

# --- 代码部分 ---

message_queue = asyncio.Queue()
rate_limit = 45  # 基础速率限制（秒）
user_last_processed_time = {}
segment_regex = r".*?[。？！~…]+|.+$"

SUPPORTED_DOCUMENT_MIME_TYPES = [
    "text/plain", "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]

DATA_FILE = "bot_data.pickle"

is_connected = True  # 全局变量，用于跟踪 Telegram 连接状态
telegram_application = None # 全局变量，用于存储 Application 实例


def load_data():
    """加载保存的会话数据和 API 密钥。"""
    try:
        with open(DATA_FILE, "rb") as f:
            data = pickle.load(f)
            conversation_ids_by_user = data.get('conversation_ids_by_user', {})
            api_keys = data.get('api_keys', API_KEYS)
            user_api_keys = data.get('user_api_keys', {})
            blocked_users = data.get('blocked_users', set())  # 加载黑名单
            return conversation_ids_by_user, api_keys, user_api_keys, blocked_users
    except (FileNotFoundError, EOFError, pickle.UnpicklingError) as e:
        print(f"Error loading data from {DATA_FILE}: {e}, using default values.")
        return {}, API_KEYS, {}, set()  # 默认黑名单为空集合
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
        user_api_keys[user_id] = alias  # 更新为新的 alias
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)  # 保持数据一致性
        await update.message.reply_text(f"好嘞，让 {alias} 来跟你聊吧！")  # 统一的回复
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
    user_id = str(chat_id)
    current_api_key, current_api_key_alias = get_user_api_key(user_id)
    conversation_id = conversation_ids_by_user.get(user_id)

    headers = {"Authorization": f"Bearer {current_api_key}"}
    data = {"inputs": {}, "query": user_message, "user": str(chat_id), "response_mode": "streaming",
            "files": files if files else []}
    if conversation_id:
        data["conversation_id"] = conversation_id
        print(f"Continuing conversation: {chat_id=}, {conversation_id=}")
    else:
        print(f"Starting new conversation: {chat_id=}")
    full_text_response = ""
    max_retries = 3  # 限制最大重试次数
    retry_count = 0
    consecutive_404_count = 0  # 连续 404 错误的计数器


    while retry_count <= max_retries:
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
                        if chunk.startswith("data:"):
                            try:
                                response_data = json.loads(chunk[5:])
                                event = response_data.get("event")
                                if not first_chunk_received:
                                    first_chunk_received = True
                                    response_conversation_id = response_data.get("conversation_id")
                                    if response_conversation_id:
                                        # 始终更新 conversation_id，即使它已经存在
                                        conversation_ids_by_user[user_id] = response_conversation_id
                                        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                        print(
                                            f"Stored/Updated conversation_id: {response_conversation_id} for user: {user_id}")
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
                                elif event == "error":  #  处理 "error" 事件
                                    print("Received event: error, clearing conversation_id and informing user.")
                                    if user_id in conversation_ids_by_user:
                                        del conversation_ids_by_user[user_id]
                                        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                    await bot.send_message(chat_id=chat_id, text="啊，上次说话都好久以前了，我都快忘了说了啥子了。")
                                    return  # 立即返回，停止当前请求
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
                        else:
                            print(f"Non-data chunk received: {chunk}")

                    if full_text_response.strip():
                        segments = segment_text(full_text_response, segment_regex)
                        for i, segment in enumerate(segments):
                            await bot.send_message(chat_id=chat_id, text=segment)
                            if i < len(segments) - 1:
                                delay = random.uniform(1, 3)
                                print(f"Waiting for {delay:.2f}s")
                                await asyncio.sleep(delay)  # 控制消息发送间隔
                    else:
                        await bot.send_message(chat_id=chat_id, text="呜呜，今天的流量已经用光了，刚充话费，到账需要时间，过一段时间再聊吧~")  # 更自然的空回复
                    return

                elif response.status_code == 404:
                    error_details = response.json()
                    if error_details.get('code') == 'not_found' and error_details.get(
                            'message') == 'Conversation Not Exists.':
                        consecutive_404_count += 1  # 增加 404 计数
                        print(f"Received 404 Conversation Not Exists for user {user_id}.")

                        if consecutive_404_count >= 2:  # 连续两次或更多 404
                            print(f"Clearing conversation_id for user {user_id} due to consecutive 404 errors.")
                            if user_id in conversation_ids_by_user:
                                del conversation_ids_by_user[user_id]
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

        except (httpx.RequestError, httpx.ConnectError, httpx.RemoteProtocolError) as e:
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
        # 1. 从队列中获取一个消息
        update, context, message_type, message_content, file_info = await message_queue.get()
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id
        bot = context.bot
        current_user_queue = []  # 存储当前用户的消息


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
                    current_user_queue.append((next_update, next_context, next_message_type, next_message_content, next_file_info))
                    message_queue.task_done()
                else:
                    # 如果不是来自同一用户的消息，则将其放回队列
                    await message_queue.put((next_update, next_context, next_message_type, next_message_content, next_file_info))
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
                    upload_result = await upload_file_to_dify(bytes(file_bytes), file_info['file_name'], file_info['mime_type'], user_id)
                    if upload_result and upload_result.get("id"):
                        collected_files.append({"type": file_info['file_type'],"transfer_method": "local_file","upload_file_id": upload_result["id"]})
                except Exception as e:
                    print(f"文件上传/处理错误: {e}")
                    await bot.send_message(chat_id=chat_id, text="处理文件的时候出了点小问题...")

        # 5. 发送合并后的消息
        if collected_text.strip() or collected_files:
            print(f"合并消息: {collected_text}, 文件: {collected_files}")
            await dify_stream_response(collected_text.strip(), chat_id, bot, files=collected_files)

        # 6. 更新最后处理时间
        user_last_processed_time[user_id] = time.time()
        message_queue.task_done()


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
            save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users) # 保存
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

async def check_telegram_connection(app: Application):
    """每 60 秒检查 Telegram 连接状态。"""
    global is_connected
    while True:
        await asyncio.sleep(60)  # 每 60 秒检查一次
        try:
            await app.bot.get_me()  # 尝试一个简单的 API 调用
            if not is_connected:
                is_connected = True
                print("Telegram reconnected.")
        except (NetworkError, TimedOut) as e:
            if is_connected:
                is_connected = False
                print(f"Telegram connection lost: {e}")
        except Exception as e:
            if is_connected:
                is_connected = False
                print(f"Telegram connection check error: {e}")


async def connect_telegram():
    """连接 Telegram 机器人并处理断线重连。"""
    global is_connected, telegram_application
    while True:
        try:
            if telegram_application is None: # 首次或重连时创建新的 Application 实例
                telegram_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
                telegram_application.add_handler(CommandHandler("start", start))
                telegram_application.add_handler(CommandHandler("set", set_api_key))
                telegram_application.add_handler(CommandHandler("block", block_user))         # 添加 /block 命令
                telegram_application.add_handler(CommandHandler("unblock", unblock_user))     # 添加 /unblock 命令
                telegram_application.add_handler(CommandHandler("clean", clean_conversations))  # 添加 /clean 命令
                telegram_application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
                telegram_application.add_error_handler(error_handler)

            async with telegram_application:
                if not telegram_application.running: # 如果 Application 没有运行，则启动
                    await telegram_application.start()
                    await telegram_application.updater.start_polling()
                    print("Bot started or re-started.")
                    asyncio.create_task(check_telegram_connection(telegram_application)) # 启动心跳检测
                    asyncio.create_task(process_message_queue(telegram_application)) # 启动消息队列处理
                    is_connected = True # 设置连接状态

                await asyncio.Future()  # 持续运行，直到被外部取消

        except (NetworkError, TimedOut) as e:
            print(f"Telegram 连接错误: {e}")
            print("尝试重新连接...")
            is_connected = False  # 更新连接状态
            if telegram_application and telegram_application.running:
                await telegram_application.updater.stop() # 停止 updater
                await telegram_application.stop() # 停止 application
            telegram_application = None # 移除旧的 Application 实例，下次循环会重新创建
            await asyncio.sleep(60)  # 等待 60 秒后重试

        except asyncio.CancelledError:
            print("Stopping the bot...")
            if telegram_application:
                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users) # 退出前保存数据
                if telegram_application.running:
                    await telegram_application.updater.stop()
                    await telegram_application.stop()
            break
        except Exception as e:
            print(f"Unexpected error: {e}")
            is_connected = False
            if telegram_application:
                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                if telegram_application.running:
                    await telegram_application.updater.stop()
                    await telegram_application.stop()
            telegram_application = None # 移除旧的 Application 实例，下次循环会重新创建
            await asyncio.sleep(60) # 等待 60 秒后重试


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
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
