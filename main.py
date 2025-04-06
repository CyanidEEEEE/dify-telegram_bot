import telegram
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    CommandHandler,
    ContextTypes,
    CallbackContext,
    ConversationHandler,
    CallbackQueryHandler,
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
import gc
import base64
import aiosqlite
import pickle
from telegram.error import NetworkError, TimedOut, TelegramError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest

# --- 配置部分 ---
TELEGRAM_BOT_TOKEN = "7522JHCMFjJY"  # 替换为你的 Telegram Bot Token
DIFY_API_URL = "http://192.1"  # 替换为你的 Dify API URL
ADMIN_IDS = ["603"]  # 替换为你的管理员 ID，可以有多个
API_KEYS = {
    "dave": "a",
    "dean": "ap587g",
}


DEFAULT_API_KEY_ALIAS = "dave"


MEMORY_CONFIG = {
    'max_history_length': 200,  # 历史记录限制
    'max_queue_size': 200,     # 消息队列大小限制
    'max_file_size': 10 * 1024 * 1024  # 文件大小限制(10MB)
}

# --- 代码部分 ---
message_queue = asyncio.Queue(maxsize=MEMORY_CONFIG['max_queue_size'])
rate_limit = 25  # 基础速率限制（秒）
user_last_processed_time = {}
segment_regex = r'[^。！？!?\.…]+[。！？!?\.…]+|[^。！？!?\.…]+$'

SUPPORTED_DOCUMENT_MIME_TYPES = [
    "text/plain", "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]

DATA_FILE = "bot_data.pickle"

# is_connected = True  # 全局变量，用于跟踪 Telegram 连接状态, 现在由 TelegramConnectionMonitor 内部维护
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

# 添加一个全局变量来存储延迟保存的任务
delayed_memory_tasks = {}

# 修改 TELEGRAM_PROXY 配置
TELEGRAM_PROXY = {
    'url': 'socks5://127.0.0.1:10808',  # 使用 socks5 协议
    'connect_timeout': 60,  # 连接超时时间（秒）- 增加超时时间
    'read_timeout': 60,     # 读取超时时间（秒）- 增加超时时间
    'write_timeout': 60,    # 写入超时时间（秒）- 增加超时时间
    'pool_timeout': 120,    # 连接池超时时间 - 新增
    'pool_connections': 20  # 连接池大小 - 新增
}

# 修改 DIFY_TIMEOUT 配置
DIFY_TIMEOUT = {
    'connect': 300.0,    # 连接超时
    'read': 300.0,       # 读取超时
    'stream': 300.0      # 流式响应超时
}

# 添加打字延迟配置
TYPING_CONFIG = {
    'min_delay': 1,    # 最小延迟（秒）
    'max_delay': 10,   # 最大延迟（秒）
    'chars_per_sec': {
        'min': 5,      # 最慢打字速度（字/秒）
        'max': 15      # 最快打字速度（字/秒）
    }
}

# 添加全局变量来跟踪消息处理队列任务
message_queue_task = None

# 在代码开头添加这些全局变量
connection_monitor = None  # 全局变量，存储连接监控器实例
WATCHDOG_TIMEOUT = 1800  # 看门狗超时时间（秒）- 从600秒改为1800秒（30分钟）
last_activity_time = time.time()  # 记录最后活动时间
last_message_queue_size = 0  # 记录上次消息队列大小，用于检测队列是否卡住

def load_data():
    """加载保存的会话数据和 API 密钥。"""
    global API_KEYS, delayed_memory_tasks, conversation_history  # 添加 conversation_history
    try:
        with open(DATA_FILE, "rb") as f:
            data = pickle.load(f)
            conversation_ids_by_user = data.get('conversation_ids_by_user', {})
            loaded_api_keys = data.get('api_keys', {})
            # 更新全局API_KEYS但不覆盖原始值
            API_KEYS.update(loaded_api_keys)
            user_api_keys = data.get('user_api_keys', {})
            blocked_users = data.get('blocked_users', set())
            # 加载对话历史
            loaded_conversation_history = data.get('conversation_history', {})
            # 更新全局conversation_history
            for key, value in loaded_conversation_history.items():
                conversation_history[key] = value
            # 确保 delayed_memory_tasks 被初始化为空字典
            delayed_memory_tasks = {}
            
            print(f"已加载 {len(conversation_ids_by_user)} 个对话ID, {len(loaded_conversation_history)} 个对话历史记录")
            return conversation_ids_by_user, loaded_api_keys, user_api_keys, blocked_users
    except (FileNotFoundError, EOFError, pickle.UnpicklingError) as e:
        print(f"Error loading data from {DATA_FILE}: {e}, using default values.")
        conversation_history = {}  # 初始化对话历史
        delayed_memory_tasks = {}
        return {}, API_KEYS, {}, set()
    except Exception as e:
        print(f"Unexpected error loading from pickle: {e}")
        conversation_history = {}  # 初始化对话历史
        delayed_memory_tasks = {}
        return {}, API_KEYS, {}, set()


def save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users):
    """保存会话数据和 API 密钥。"""
    data = {
        'conversation_ids_by_user': conversation_ids_by_user,
        'api_keys': api_keys,
        'user_api_keys': user_api_keys,
        'blocked_users': blocked_users,
        'conversation_history': conversation_history,  # 保存对话历史
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

    # 获取所有可用角色列表
    available_roles = list(api_keys.keys())
    role_list = "\n".join([f"• {role}" for role in available_roles])

    if not context.args:
        await update.message.reply_text(f"想换个人聊天吗？我可以帮你摇人，我认识这些家伙：\n{role_list}\n")
        return

    alias = context.args[0].lower()
    if alias in api_keys:
        old_alias = user_api_keys.get(user_id)
        user_api_keys[user_id] = alias  # 更新为新的 alias

        # 不清除对话ID，让每个角色保持自己的对话
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
        await update.message.reply_text(f"好嘞，让 {alias} 来跟你聊吧！")
    else:
        await update.message.reply_text(f"呃，我不认识叫 '{alias}' 的家伙，我可以帮你摇人，但是我只认识这些家伙：\n{role_list}\n")


def segment_text(text, segment_regex):
    """将文本分段，以便逐段发送。
    使用更自然的分段逻辑：
    1. 按标点分段
    2. 处理括号内容
    3. 忽略纯标点符号的段落
    """
    segments = []
    current = ""

    # 分割文本为初步片段
    lines = text.split('\n')

    for line in lines:
        if not line.strip():
            continue

        # 处理括号内容
        bracket_parts = re.findall(r'（[^）]*）|\([^)]*\)|[^（(]+', line)

        for part in bracket_parts:
            # 如果是括号内容，直接作为独立段落
            if part.startswith('（') or part.startswith('('):
                if current.strip():
                    segments.append(current.strip())
                    current = ""
                segments.append(part.strip())
                continue

            # 处理句子结尾
            sentences = re.findall(r'[^。！？!?\.…]+[。！？!?\.…]+|[^。！？!?\.…]+$', part)
            for sentence in sentences:
                if sentence.strip():
                    current += sentence
                    # 检查是否以结束标点结尾
                    if any(sentence.strip().endswith(p) for p in ['。', '！', '!', '？', '?', '.', '…', '...']):
                        if current.strip():
                            segments.append(current.strip())
                            current = ""

    # 处理最后剩余的内容
    if current.strip():
        segments.append(current.strip())

    # 过滤掉纯标点符号的段落
    valid_segments = []
    punctuation_marks = '，。！？!?…""''()（）.、～~'  # 添加更多标点符号
    for seg in segments:
        # 检查段落是否全是标点符号
        if not all(char in punctuation_marks or char.isspace() for char in seg):
            valid_segments.append(seg)

    return valid_segments


async def upload_file_to_dify(file_bytes, file_name, mime_type, user_id):
    """上传文件到 Dify。"""
    # 添加文件大小检查
    if len(file_bytes) > MEMORY_CONFIG['max_file_size']:
        print(f"文件过大: {len(file_bytes)} bytes")
        return None

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


async def send_message_naturally(bot, chat_id, text):
    """以更自然的方式发送消息"""
    # 基础延迟参数
    char_delay = 0.1  # 每个字符的基础延迟
    min_delay = 1.0   # 最小延迟
    max_delay = 3.0   # 最大延迟

    # 根据文本长度计算延迟时间
    typing_delay = min(max(len(text) * char_delay, min_delay), max_delay)

    # 显示"正在输入"状态并等待
    await bot.send_chat_action(chat_id, "typing")
    await asyncio.sleep(typing_delay)

    # 发送消息
    await bot.send_message(chat_id=chat_id, text=text)

    # 如果不是最后一段，添加短暂停顿
    if len(text) > 0:
        await asyncio.sleep(0.5)


async def dify_stream_response(user_message: str, chat_id: int, bot: telegram.Bot, files=None) -> None:
    """向 Dify 发送消息并处理流式响应。"""
    global conversation_history, is_importing_memory, last_activity_time
    user_id = str(chat_id)
    current_api_key, current_api_key_alias = get_user_api_key(user_id)
    history_key = (user_id, current_api_key_alias)
    conversation_key = (user_id, current_api_key_alias)

    # 更新活动时间
    last_activity_time = time.time()

    # 初始化对话历史
    if history_key not in conversation_history:
        conversation_history[history_key] = []

    # 使用更小的历史记录限制
    max_history_length = MEMORY_CONFIG['max_history_length']
    if len(conversation_history[history_key]) > max_history_length:
        # 保留最新的记录，但在清理时释放内存
        conversation_history[history_key] = conversation_history[history_key][-max_history_length:]
        # 强制垃圾回收
        gc.collect()

    # 只有在不是导入记忆时才记录用户消息
    if not is_importing_memory:
        conversation_history[history_key].append(f"user: {user_message}")

    # 使用组合键获取当前角色的对话ID
    conversation_id = conversation_ids_by_user.get(conversation_key)

    headers = {"Authorization": f"Bearer {current_api_key}"}
    data = {"inputs": {}, "query": user_message, "user": str(chat_id), "response_mode": "streaming",
            "files": files if files else []}

    # 只在有有效的 conversation_id 时才添加到请求中
    if conversation_id and conversation_id != 'new':
        data["conversation_id"] = conversation_id
        print(f"Continuing conversation: {chat_id=}, {conversation_id=}, role={current_api_key_alias}")
    else:
        print(f"Starting new conversation: {chat_id=}, role={current_api_key_alias}")
        # 记录没有找到现有会话ID的情况
        if conversation_key in conversation_ids_by_user:
            print(f"警告: 对话键 {conversation_key} 在字典中但值为: {conversation_ids_by_user.get(conversation_key)}")
        else:
            print(f"信息: 对话键 {conversation_key} 不在字典中")

    full_text_response = ""
    typing_message = None
    last_typing_update = 0
    typing_interval = 4

    try:
        typing_message = await bot.send_chat_action(chat_id=chat_id, action="typing")
        last_typing_update = time.time()

        async with httpx.AsyncClient(trust_env=False, timeout=DIFY_TIMEOUT['stream']) as client:
            response = await asyncio.wait_for(client.post(DIFY_API_URL + "/chat-messages", headers=headers, json=data), timeout=DIFY_TIMEOUT['stream'])


            if response.status_code == 200:
                print(f"Dify API status code: 200 OK")
                first_chunk_received = False
                empty_response_count = 0
                async for chunk in response.aiter_lines():
                    if chunk.strip() == "":
                        continue

                    if chunk.startswith("data:"):
                        try:
                            response_data = json.loads(chunk[5:])
                            event = response_data.get("event")
                            print(f"Received event: {event}")

                            if event == "error":
                                error_message = response_data.get("message", "")
                                error_code = response_data.get("code", "")
                                print(f"Error details: {error_message}")
                                print(f"Error code: {error_code}")
                                print(f"Full response data: {response_data}")

                                # 检查是否是心跳信息
                                if "ping" in error_message.lower():
                                    print("收到心跳信息，继续处理...")
                                    continue

                                # 检查是否是配额限制错误
                                if ("Resource has been exhausted" in error_message or
                                    "Rate Limit Error" in error_message or
                                    "No valid model credentials available" in error_message):
                                    print("检测到配额限制错误")
                                    if conversation_key in conversation_ids_by_user:
                                        del conversation_ids_by_user[conversation_key]
                                        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                    # 标记这个对话需要延迟处理
                                    delayed_memory_tasks[conversation_key] = None
                                    await bot.send_message(
                                        chat_id=chat_id,
                                        text="抱歉啦，我现在有点累了，需要休息一下~不过别担心，你想继续的话，我5分钟后再来找你哦！"
                                    )
                                    await offer_save_memory(bot, chat_id, conversation_key)
                                    return

                                # 其他所有错误都提供保存记忆的选项
                                print(f"收到错误事件: {error_message}")
                                if conversation_key in conversation_ids_by_user:
                                    del conversation_ids_by_user[conversation_key]
                                    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                await offer_save_memory(bot, chat_id, conversation_key)
                                return

                            if time.time() - last_typing_update >= typing_interval:
                                await bot.send_chat_action(chat_id=chat_id, action="typing")
                                last_typing_update = time.time()

                            if not first_chunk_received:
                                first_chunk_received = True
                                response_conversation_id = response_data.get("conversation_id")
                                if response_conversation_id:
                                    # 使用组合键保存对话ID
                                    old_id = conversation_ids_by_user.get(conversation_key, "无")
                                    conversation_ids_by_user[conversation_key] = response_conversation_id
                                    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                    print(f"Stored/Updated conversation_id: {response_conversation_id} for user: {user_id}, role: {current_api_key_alias} (旧ID: {old_id})")
                                else:
                                    print("Warning: conversation_id not found in the first chunk!")

                            if event == "message":
                                text_chunk = response_data.get("answer", "")
                                if text_chunk:
                                    full_text_response += text_chunk
                                    empty_response_count = 0
                            elif event == "error":
                                print("Received event: error, clearing conversation_id and informing user.")
                                if conversation_key in conversation_ids_by_user:
                                    del conversation_ids_by_user[conversation_key]
                                    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                await offer_save_memory(bot, chat_id, conversation_key)
                                return

                        except json.JSONDecodeError as e:
                            print(f"JSONDecodeError: {e}")
                            print(f"Problem chunk: {chunk}")
                            continue

                if full_text_response.strip():
                    # 记录助手的回复
                    conversation_history[history_key].append(f"assistant: {full_text_response}")

                    # 再次检查历史记录长度
                    if len(conversation_history[history_key]) > max_history_length:
                        conversation_history[history_key] = conversation_history[history_key][-max_history_length:]
                        print(f"添加回复后历史记录超出限制，已截取最新的 {max_history_length} 条记录")

                    segments = segment_text(full_text_response, segment_regex)
                    for segment in segments:
                        await send_message_naturally(bot, chat_id, segment)
                else:
                    await bot.send_message(chat_id=chat_id, text="呜呜，今天的流量已经用光了，过一段时间再聊吧~")
                return

            elif response.status_code == 400:
                # 处理 400 错误（配额限制）
                try:
                    error_data = response.json()
                    error_message = error_data.get('message', '')
                    error_code = error_data.get('code', '')
                    print(f"400 错误详情: {error_data}")

                    print("检测到配额限制错误")
                    if conversation_key in conversation_ids_by_user:
                        del conversation_ids_by_user[conversation_key]
                        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                    # 标记这个对话需要延迟处理
                    delayed_memory_tasks[conversation_key] = None
                    await bot.send_message(
                        chat_id=chat_id,
                        text="抱歉啦，我现在有点累了，需要休息一下~不过别担心，你想继续的话，我5分钟后再来找你哦！"
                    )
                    await offer_save_memory(bot, chat_id, conversation_key)
                except Exception as e:
                    print(f"处理 400 错误时出错: {e}")
                    await bot.send_message(chat_id=chat_id, text="处理消息时出现错误，请稍后重试。")
                return

            else:
                # 其他状态码的错误也提供保存记忆的选项
                print(f"Dify API status code: {response.status_code} Error")
                if conversation_key in conversation_ids_by_user:
                    del conversation_ids_by_user[conversation_key]
                    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                await offer_save_memory(bot, chat_id, conversation_key)
                return

    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.RemoteProtocolError, asyncio.TimeoutError, Exception) as e:
        print(f"Error in dify_stream_response: {e}")
        # 连接错误等异常也提供保存记忆的选项
        if conversation_key in conversation_ids_by_user:
            del conversation_ids_by_user[conversation_key]
            save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
        await offer_save_memory(bot, chat_id, conversation_key)
        return

    # 添加文件大小检查
    if files:
        total_file_size = 0
        for file_info in files:
            if isinstance(file_info, dict) and 'size' in file_info:
                total_file_size += file_info['size']
        if total_file_size > MEMORY_CONFIG['max_file_size']:
            await bot.send_message(
                chat_id=chat_id,
                text="文件总大小超过限制，请分开发送或压缩后重试"
            )
            return


async def offer_save_memory(bot, chat_id, conversation_key):
    """提供保存记忆的选项"""
    keyboard = [
        [
            InlineKeyboardButton("是", callback_data=f"save_memory_{conversation_ids_by_user.get(conversation_key, 'new')}"),
            InlineKeyboardButton("否", callback_data="new_conversation")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await bot.send_message(
        chat_id=chat_id,
        text="对方似乎有点忙，是否继续对话？感觉如果不继续的话对方很快就会把你忘了。",
        reply_markup=reply_markup
    )


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
        # 添加文件大小检查
        if message.document.file_size > MEMORY_CONFIG['max_file_size']:
            await bot.send_message(
                chat_id=chat_id,
                text="文件太大啦，能不能发个小一点的？(最大10MB)"
            )
            return

    # 检查队列大小
    if message_queue.qsize() >= MEMORY_CONFIG['max_queue_size'] * 0.9:  # 90%阈值
        await bot.send_message(
            chat_id=chat_id,
            text="我现在有点忙，请稍后再发消息~"
        )
        return

    # 确定消息类型和内容
    message_type = "unknown"
    message_content = None
    file_info = None

    # 直接处理不支持的消息类型
    if message.sticker:
        await bot.send_message(chat_id=chat_id, text="看不懂你发的啥捏~")  # 更自然的表情回复
        return

    # 处理支持的消息类型
    if message.text:
        message_type = "text"
        message_content = message.text
    elif message.photo:
        message_type = "photo"
        message_content = message.caption if message.caption else "看看这张图片"
        file_info = {"file_id": message.photo[-1].file_id, "file_type": "image", "file_name": f"photo_{uuid.uuid4()}.jpg",
                     "mime_type": "image/jpeg"}
    elif message.voice:
        message_type = "voice"
        message_content = message.caption if message.caption else "语音消息"
        file_info = {"file_id": message.voice.file_id, "file_type": "audio", "file_name": f"voice_{uuid.uuid4()}.ogg",
                     "mime_type": "audio/ogg"}
    elif message.document:
        message_type = "document"
        message_content = message.caption if message.caption else "看看这个文件"
        file_info = {"file_id": message.document.file_id, "file_type": "document",
                     "file_name": message.document.file_name or f"document_{uuid.uuid4()}",
                     "mime_type": message.document.mime_type}

    # 将消息加入队列
    await message_queue.put((update, context, message_type, message_content, file_info))
    print(f"消息已加入队列: 类型: {message_type}, 来自用户: {update.effective_user.id}，chat_id: {update.effective_chat.id}")


async def process_message_queue(application: Application):
    """处理消息队列中的消息。"""
    global last_activity_time, connection_monitor
    print("process_message_queue started")
    
    # 添加处理状态跟踪
    last_successful_process = time.time()
    processing_timeout = 300  # 5分钟无成功处理视为卡住
    
    while True:
        try:
            # 检查是否长时间无处理成功
            current_time = time.time()
            if current_time - last_successful_process > processing_timeout:
                print(f"消息处理循环已 {current_time - last_successful_process:.1f} 秒未成功处理消息，可能已卡住")
                # 重置状态
                print("尝试重置消息处理状态...")
                last_successful_process = current_time  # 重置计时器，避免连续报警
                
                # 如果队列不为空但处理停滞，可能是卡在了某个消息上
                if not message_queue.empty():
                    queue_size = message_queue.qsize()
                    print(f"消息队列中有 {queue_size} 条消息等待处理，但处理停滞")
                    
                    # 尝试取一条消息，如果超时则继续循环
                    try:
                        async with asyncio.timeout(10):  # 10秒超时
                            update, context, message_type, message_content, file_info = await message_queue.get()
                            print(f"成功获取一条停滞的消息: 类型 {message_type}")
                    except asyncio.TimeoutError:
                        print("获取消息超时，跳过当前尝试")
                        continue
                    except Exception as e:
                        print(f"获取停滞消息时错误: {e}")
                        continue
                else:
                    print("消息队列为空，等待新消息")
                    # 更新活动时间，避免看门狗误判
                    last_activity_time = current_time
                    continue
            
            # 正常从队列中获取一个消息，设置超时以避免永久阻塞
            try:
                async with asyncio.timeout(60):  # 60秒超时
                    print("等待从队列获取消息...")
                    update, context, message_type, message_content, file_info = await message_queue.get()
                    print(f"获取到消息: 类型: {message_type}, 来自用户: {update.effective_user.id}")
            except asyncio.TimeoutError:
                print("等待消息超时，继续循环")
                # 更新活动时间，避免看门狗误判
                last_activity_time = time.time()
                continue
            
            user_id = str(update.effective_user.id)
            chat_id = update.effective_chat.id
            bot = context.bot

            # 更新活动时间 - 从队列获取消息也是一种活动
            last_activity_time = time.time()
            
            try:
                # 更新连接监控器的最后消息处理时间
                if connection_monitor:
                    connection_monitor.last_message_processed_time = time.time()
                    connection_monitor.last_heartbeat = time.time()  # 也更新心跳时间
                    
                # 检查是否是记忆操作
                if message_type == "memory_operation":
                    # 获取用户的 API key 信息
                    current_api_key, current_api_key_alias = get_user_api_key(user_id)
                    conversation_key = (user_id, current_api_key_alias)

                    # 清除当前对话ID，以开始新对话
                    if conversation_key in conversation_ids_by_user:
                        del conversation_ids_by_user[conversation_key]
                        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)

                    # 设置导入状态
                    global is_importing_memory
                    is_importing_memory = True

                    try:
                        # 处理记忆操作
                        await dify_stream_response(message_content, chat_id, bot)
                    except Exception as e:
                        print(f"处理记忆操作时出错: {e}")
                        await bot.send_message(chat_id=chat_id, text="处理记忆时出现错误，请稍后重试。")
                    finally:
                        is_importing_memory = False

                    message_queue.task_done()
                    # 更新成功处理时间
                    last_successful_process = time.time()
                    continue

                # 如果不是记忆操作，则进行正常的消息合并处理
                current_user_queue = [(update, context, message_type, message_content, file_info)]
                
                # 收集队列中该用户的其他普通消息
                other_messages = []

                while not message_queue.empty():
                    try:
                        next_message = message_queue.get_nowait()
                        next_update = next_message[0]
                        next_user_id = str(next_update.effective_user.id)
                        next_type = next_message[2]  # 获取消息类型

                        if next_user_id == user_id and next_type != "memory_operation":
                            # 只合并非记忆操作的消息
                            current_user_queue.append(next_message)
                        else:
                            # 其他用户的消息或记忆操作都放回队列
                            other_messages.append(next_message)
                    except asyncio.QueueEmpty:
                        break

                # 将其他消息放回队列
                for other_message in other_messages:
                    await message_queue.put(other_message)

                # 处理合并的消息
                collected_text = ""
                collected_files = []

                for update, context, message_type, message_content, file_info in current_user_queue:
                    if message_type == "sticker":
                        await bot.send_message(chat_id=chat_id, text="看不懂你发的啥捏~")  # 更自然的表情回复
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
                        
                        # 使用超时机制避免永久阻塞
                        async with asyncio.timeout(DIFY_TIMEOUT['stream'] + 30):  # 给予额外30秒余量
                            await dify_stream_response(collected_text.strip(), chat_id, bot, files=collected_files)
                            print(f"用户 {user_id} 的消息已成功处理")
                except asyncio.TimeoutError:
                    print(f"处理用户 {user_id} 消息超时")
                    await bot.send_message(chat_id=chat_id, text="处理消息时超时，请稍后再试。")
                except TimedOut as e:
                    print(f"Error in process_message_queue during dify_stream_response: {e}")
                    await message_queue.put((update, context, message_type, message_content, file_info))
                except Exception as e:
                    print(f"Error in process_message_queue during dify_stream_response: {e}")
                    try:
                        await bot.send_message(chat_id=chat_id, text="处理消息时发生错误，请稍后再试。")
                    except:
                        pass

                # 处理完消息后等待 rate_limit 秒
                print(f"用户 {user_id} 消息处理完成，等待 {rate_limit} 秒后处理下一条消息")
                await asyncio.sleep(rate_limit)
                
                # 处理完成后标记任务完成
                for _ in range(len(current_user_queue)):
                    message_queue.task_done()
                    
                # 更新活动时间和成功处理时间
                last_activity_time = time.time()
                last_successful_process = time.time()
                
            except TimedOut as e:
                print(f"TimedOut in process_message_queue: {e}")
                # 对于超时错误，我们重新放回队列尝试稍后再处理
                message_queue.task_done()  # 先标记当前任务完成
                await message_queue.put((update, context, message_type, message_content, file_info))
                await asyncio.sleep(5)  # 等待一段时间后继续
                
            except (NetworkError, TelegramError) as e:
                print(f"Network error in process_message_queue: {e}")
                # 对于网络错误，我们不重新放回队列，但会通知用户
                try:
                    if getattr(bot, '_initialized', False):  # 确保 bot 仍然可用
                        await bot.send_message(chat_id=chat_id, text="网络连接问题，请稍后重新发送消息。")
                except Exception:
                    pass
                message_queue.task_done()
                await asyncio.sleep(5)
                
            except Exception as e:
                print(f"Unexpected error in process_message_queue: {e}")
                # 对于其他错误，我们标记任务完成但不重试
                message_queue.task_done()
                await asyncio.sleep(5)
                
        except asyncio.CancelledError:
            # 任务被取消时正常退出
            print("消息处理队列任务被取消")
            break
            
        except Exception as e:
            # 捕获队列操作本身的错误
            print(f"Critical error in process_message_queue main loop: {e}")
            await asyncio.sleep(10)
            # 更新成功处理时间以避免误判
            last_successful_process = time.time()


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
    """清除所有用户的聊天 ID 记录和记忆（管理员命令）。"""
    user_id = str(update.effective_user.id)
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("你没有权限执行此操作。")
        return

    try:
        # 发送处理中的消息
        processing_msg = await update.message.reply_text("正在清除所有记录，请稍候...")

        # 清除全局变量
        global conversation_ids_by_user, conversation_history
        conversation_ids_by_user = {}  # 清空对话ID
        conversation_history = {}      # 清空对话历史

        # 清除数据库中的记忆
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('DELETE FROM chat_memories')
            await db.commit()

        # 保存更改
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)

        # 更新消息
        await processing_msg.edit_text(
            "✅ 清除完成！\n"
            "- 所有对话ID已重置\n"
            "- 所有对话历史已清除\n"
            "- 所有保存的记忆已删除"
        )

    except Exception as e:
        print(f"清除记录时出错: {e}")
        await update.message.reply_text(
            "❌ 清除过程中出现错误。\n"
            "请检查日志或联系开发者。"
        )


# 修改连接监控器类
class TelegramConnectionMonitor:
    def __init__(self, application: Application):
        self.application = application
        self.is_healthy = True
        self.last_heartbeat = time.time()
        self.consecutive_failures = 0
        self._monitor_task = None
        self._reconnect_lock = asyncio.Lock()
        self.is_connected = True
        self.heartbeat_timeout = 120
        self.base_retry_delay = 10  # 基础重试延迟
        self.max_retry_delay = 300  # 最大重试延迟
        self._stop_event = asyncio.Event()  # 添加停止事件
        self.last_message_processed_time = time.time()  # 添加最后消息处理时间
        self.last_message_queue_size = 0  # 记录上次消息队列大小

    async def start_monitoring(self):
        """启动连接状态监控。"""
        self._monitor_task = asyncio.create_task(self._monitor_connection())
        print("Connection monitoring started")

    async def stop_monitoring(self):
        """停止连接状态监控。"""
        if self._monitor_task:
            self._stop_event.set()
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
            print("Connection monitoring stopped")

    async def _monitor_connection(self):
        """定期检查连接状态。"""
        check_interval = 30  # 检查间隔（秒）
        while not self._stop_event.is_set():
            try:
                # 尝试向 Telegram 发送一个简单请求以检查连接
                await self._check_connection()
                
                # 记录最后一次成功的心跳时间
                if self.is_healthy:
                    self.last_heartbeat = time.time()
                    self.consecutive_failures = 0
                
                # 如果已经超时未收到心跳，将标记为不健康
                elapsed = time.time() - self.last_heartbeat
                if elapsed > self.heartbeat_timeout and self.is_healthy:
                    print(f"Connection considered unhealthy: No heartbeat for {elapsed:.1f} seconds")
                    self.is_healthy = False
                    await self._trigger_reconnect()
                
                # 等待指定时间再检查
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                # 任务被取消时退出
                break
            except Exception as e:
                print(f"Error in connection monitor: {e}")
                # 发生错误时等待较短的时间，以便更快地重新检查
                await asyncio.sleep(5)
                
    async def _check_connection(self):
        """检查连接是否健康。"""
        global last_activity_time, message_queue_task
        
        try:
            # 尝试向 Telegram 发送一个简单的 getMe 请求
            if not self.application.bot:
                print("Bot is not available, cannot check connection")
                self.is_healthy = False
                return
                
            attempt = self.consecutive_failures + 1
            print(f"Checking connection health (attempt {attempt})...")
            
            # 检查消息队列状态 - 即使队列大小没变，仍然有消息在被处理
            current_queue_size = message_queue.qsize()
            current_time = time.time()
            
            # 更新全局活动时间 - 执行健康检查本身就是一种活动
            last_activity_time = current_time
            
            # 如果队列大小长时间不变且不为空，可能表示处理停滞
            if (current_queue_size > 0 and 
                current_queue_size == self.last_message_queue_size and
                current_time - self.last_message_processed_time > 300):  # 5分钟无变化
                print(f"消息队列大小 {current_queue_size} 在过去5分钟没有变化，可能处理停滞")
                self.is_healthy = False
                self.consecutive_failures += 1
                return
                
            self.last_message_queue_size = current_queue_size
            
            # 设置超时，防止请求卡住
            try:
                async with asyncio.timeout(30):  # 30秒超时
                    me = await self.application.bot.get_me()
            except asyncio.TimeoutError:
                print(f"Connection health check timed out after 30 seconds")
                self.is_healthy = False
                self.consecutive_failures += 1
                return
            
            # 检查成功，更新状态
            print(f"Connection health check successful: @{me.username}")
            self.is_healthy = True
            self.last_heartbeat = current_time  # 更新心跳时间
            last_activity_time = current_time  # 更新活动时间
            self.consecutive_failures = 0  # 重置失败计数
            
        except Exception as e:
            # 记录错误并增加失败计数
            retry_delay = min(self.base_retry_delay * (2 ** min(self.consecutive_failures, 4)), self.max_retry_delay)
            print(f"Connection health check failed (attempt {attempt}). Retrying in {retry_delay} seconds. Error: {e}")
            self.is_healthy = False
            self.consecutive_failures += 1

    async def _trigger_reconnect(self):
        """触发重连流程，无限重试"""
        global message_queue_task, last_activity_time
        
        async with self._reconnect_lock:
            if not self.is_healthy and self.is_connected:
                self.is_connected = False
                print("Triggering reconnection...")
                
                # 保存所有重要状态
                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                print("状态数据已保存")
                
                # 保存当前未处理的消息队列
                queue_backup = []
                while not message_queue.empty():
                    try:
                        queue_item = message_queue.get_nowait()
                        queue_backup.append(queue_item)
                    except asyncio.QueueEmpty:
                        break
                    
                print(f"已备份 {len(queue_backup)} 条待处理消息")

                # 停止当前的消息处理队列任务
                if message_queue_task and not message_queue_task.done():
                    message_queue_task.cancel()
                    try:
                        await message_queue_task
                    except asyncio.CancelledError:
                        pass
                    message_queue_task = None
                    print("消息处理队列已停止")

                reconnect_attempt = 0
                while True:  # 无限重试循环
                    reconnect_attempt += 1
                    try:
                        print(f"尝试重连 (第 {reconnect_attempt} 次)...")
                        # 完全关闭旧实例
                        if hasattr(self.application, 'running') and self.application.running:
                            try:
                                await self.stop_monitoring()  # 先停止监控
                                if hasattr(self.application, 'updater') and self.application.updater:
                                    await self.application.updater.stop()
                                await self.application.stop()
                                if hasattr(self.application, 'shutdown'):
                                    await self.application.shutdown()
                                print("Application stopped successfully")
                            except Exception as e:
                                print(f"Error stopping application: {e}")

                        # 等待一段时间确保旧实例完全关闭
                        await asyncio.sleep(15)
                        
                        # 重新创建更健壮的应用实例
                        await self._recreate_application()
                        
                        # 恢复消息队列
                        for item in queue_backup:
                            await message_queue.put(item)
                        print(f"已恢复 {len(queue_backup)} 条待处理消息到队列")
                        
                        # 启动新的消息处理队列
                        message_queue_task = asyncio.create_task(process_message_queue(self.application))
                        print("消息处理队列已重新启动")
                        
                        # 重新开始监控
                        await self.start_monitoring()
                        
                        # 更新状态
                        self.is_connected = True
                        self.is_healthy = True
                        self.consecutive_failures = 0
                        last_activity_time = time.time()  # 更新活动时间
                        
                        break  # 重连成功，退出重试循环
                    except Exception as e:
                        print(f"重连尝试 {reconnect_attempt} 失败: {e}")
                        # 随机等待时间，避免所有客户端同时重连
                        retry_delay = min(self.base_retry_delay * (2 ** min(reconnect_attempt % 10, 4)) + random.uniform(0, 5), self.max_retry_delay)
                        print(f"将在 {retry_delay:.1f} 秒后重试...")
                        await asyncio.sleep(retry_delay)
                        continue
    
    async def _recreate_application(self):
        """重新创建应用实例"""
        global telegram_application, conversation_ids_by_user, api_keys, user_api_keys, blocked_users, conversation_history
        
        # 重新加载保存的数据
        try:
            loaded_data = load_data()
            if loaded_data:
                loaded_conversation_ids, loaded_api_keys, loaded_user_api_keys, loaded_blocked_users = loaded_data
                # 合并数据，保留内存中未保存的数据
                for key, value in loaded_conversation_ids.items():
                    conversation_ids_by_user[key] = value
                api_keys.update(loaded_api_keys)
                user_api_keys.update(loaded_user_api_keys)
                blocked_users.update(loaded_blocked_users)
                print("已从文件恢复状态数据")
        except Exception as e:
            print(f"加载保存的数据时出错: {e}")
        
        # 创建新的 Application 实例
        try:
            # 创建请求对象
            request = HTTPXRequest(
                proxy=TELEGRAM_PROXY.get('url'),
                connect_timeout=TELEGRAM_PROXY.get('connect_timeout', 60),
                read_timeout=TELEGRAM_PROXY.get('read_timeout', 60),
                write_timeout=TELEGRAM_PROXY.get('write_timeout', 60),
                pool_timeout=TELEGRAM_PROXY.get('pool_timeout', 120),
                connection_pool_size=TELEGRAM_PROXY.get('pool_connections', 20)
            )
            
            telegram_application = (
                Application.builder()
                .token(TELEGRAM_BOT_TOKEN)
                .request(request)
                .build()
            )
            
            # 手动初始化请求对象
            if hasattr(telegram_application.bot, 'request') and not getattr(telegram_application.bot.request, '_initialized', False):
                print("手动初始化Bot的请求对象")
                await telegram_application.bot.initialize()
            
            # 注册处理函数
            register_handlers(telegram_application)
            
            # 初始化和启动
            await init_db()
            await telegram_application.initialize()
            
            # 确保Bot已初始化
            if not getattr(telegram_application.bot, '_initialized', False):
                print("手动初始化Bot")
                await telegram_application.bot.initialize()
            
            # 启动应用和轮询
            await telegram_application.start()
            await telegram_application.updater.start_polling(
                poll_interval=1.0,
                bootstrap_retries=-1,
                timeout=60,
                read_timeout=60,
                write_timeout=60,
                connect_timeout=60,
                pool_timeout=60,
                allowed_updates=["message", "callback_query"]
            )
            
            # 更新实例引用
            self.application = telegram_application
            print("应用程序重启成功")
            
        except Exception as e:
            print(f"创建应用程序实例时出错: {e}")
            raise

async def connect_telegram():
    """连接 Telegram 机器人，无限重试"""
    global telegram_application, message_queue_task, connection_monitor
    base_retry_delay = 10
    max_retry_delay = 300
    retry_count = 0

    while True:
        try:
            if telegram_application and telegram_application.running:
                try:
                    await telegram_application.updater.stop()
                    await telegram_application.stop()
                    await telegram_application.shutdown()
                except Exception as e:
                    print(f"Error stopping existing application: {e}")

            telegram_application = None
            await asyncio.sleep(10)  # 等待确保旧实例完全关闭

            telegram_application = (
                Application.builder()
                .token(TELEGRAM_BOT_TOKEN)
                .request(
                    HTTPXRequest(
                        proxy=TELEGRAM_PROXY['url'],
                        connect_timeout=TELEGRAM_PROXY['connect_timeout'],
                        read_timeout=TELEGRAM_PROXY['read_timeout'],
                        write_timeout=TELEGRAM_PROXY['write_timeout'],
                        pool_timeout=TELEGRAM_PROXY.get('pool_timeout', 120),
                        connection_pool_size=TELEGRAM_PROXY.get('pool_connections', 20)  # 使用正确的参数而不是 limits
                    )
                )
                .build()
            )

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
                    await init_db()
                    await telegram_application.start()
                    
                    await telegram_application.updater.start_polling(
                        poll_interval=1.0,
                        bootstrap_retries=-1,
                        timeout=60,
                        read_timeout=60,
                        write_timeout=60,
                        connect_timeout=60,
                        pool_timeout=60,
                        allowed_updates=["message", "callback_query"]
                    )

                    print("Bot started successfully")
                    retry_count = 0  # 重置重试计数

                    # 启动连接监控
                    connection_monitor = TelegramConnectionMonitor(telegram_application)
                    await connection_monitor.start_monitoring()

                    # 启动消息处理
                    if message_queue_task is None or message_queue_task.done():
                        message_queue_task = asyncio.create_task(process_message_queue(telegram_application))
                        print("消息处理队列已启动")

                    # 等待停止信号
                    stop_event = asyncio.Event()
                    await stop_event.wait()

        except Exception as e:
            retry_count += 1
            retry_delay = min(base_retry_delay * (2 ** (retry_count - 1)), max_retry_delay)
            print(f"Connection error: {e}")
            print(f"Retrying in {retry_delay} seconds (attempt {retry_count})...")

            # 停止连接监控
            if connection_monitor:
                await connection_monitor.stop_monitoring()
                connection_monitor = None

            # 停止消息处理队列
            if message_queue_task and not message_queue_task.done():
                message_queue_task.cancel()
                try:
                    await message_queue_task
                except asyncio.CancelledError:
                    pass
                message_queue_task = None
                print("消息处理队列已停止")

            if telegram_application:
                try:
                    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                    if telegram_application.running:
                        await telegram_application.updater.stop()
                        await telegram_application.stop()
                except Exception as stop_error:
                    print(f"Error stopping application: {stop_error}")
                telegram_application = None

            await asyncio.sleep(retry_delay)
            print("Attempting to reconnect...")
            continue

async def main():
    """主函数"""
    global telegram_application, connection_monitor, message_queue_task, last_activity_time
    
    # 最外层无限重试循环
    consecutive_failures = 0
    max_failures = 10  # 允许的最大连续失败次数
    
    while True:
        try:
            # 加载保存的数据
            load_data()
            
            # 初始化数据库
            await init_db()
            
            # 创建并配置 Telegram 应用
            request = HTTPXRequest(
                proxy=TELEGRAM_PROXY.get('url'),
                connect_timeout=TELEGRAM_PROXY.get('connect_timeout', 60),
                read_timeout=TELEGRAM_PROXY.get('read_timeout', 60),
                write_timeout=TELEGRAM_PROXY.get('write_timeout', 60),
                pool_timeout=TELEGRAM_PROXY.get('pool_timeout', 120),
                connection_pool_size=TELEGRAM_PROXY.get('pool_connections', 20)
            )
            
            telegram_application = (
                Application.builder()
                .token(TELEGRAM_BOT_TOKEN)
                .request(request)
                .build()
            )
            
            # 注册处理函数
            register_handlers(telegram_application)
            
            # 启动应用程序
            await telegram_application.initialize()
            await telegram_application.start()
            await telegram_application.updater.start_polling(
                poll_interval=1.0,
                bootstrap_retries=-1,
                timeout=60,
                read_timeout=60,
                write_timeout=60,
                connect_timeout=60,
                pool_timeout=60,
                allowed_updates=["message", "callback_query"]
            )
            
            # 启动连接监控
            connection_monitor = TelegramConnectionMonitor(telegram_application)
            await connection_monitor.start_monitoring()
            
            # 启动消息处理队列
            message_queue_task = asyncio.create_task(process_message_queue(telegram_application))
            
            # 启动看门狗监控
            watchdog_task = asyncio.create_task(watchdog_monitor())
            
            # 启动数据清理任务
            cleanup_task = asyncio.create_task(cleanup_old_data())
            
            # 更新最后活动时间
            last_activity_time = time.time()
            
            # 重置连续失败计数
            consecutive_failures = 0
            
            # 等待任务完成（实际上会一直运行）
            await asyncio.gather(
                message_queue_task,
                watchdog_task,
                cleanup_task
            )
            
        except Exception as e:
            consecutive_failures += 1
            print(f"严重错误，整个系统将重启 (连续失败: {consecutive_failures}/{max_failures}): {e}")
            
            # 记录带有堆栈跟踪的详细错误
            import traceback
            print(f"错误详情: {traceback.format_exc()}")
            
            # 保存数据
            try:
                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
            except Exception as save_error:
                print(f"保存数据时出错: {save_error}")
            
            # 清理资源
            try:
                # 停止消息处理任务
                if message_queue_task and not message_queue_task.done():
                    message_queue_task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(message_queue_task), timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                
                # 停止连接监控
                if connection_monitor:
                    await connection_monitor.stop_monitoring()
                
                # 停止应用程序
                if telegram_application and telegram_application.running:
                    await telegram_application.stop()
                    
                print("资源清理完成")
            except Exception as cleanup_error:
                print(f"清理资源时出错: {cleanup_error}")
            
            # 如果连续失败次数过多，增加延迟或进行特殊处理
            retry_delay = min(30 * (2 ** min(consecutive_failures, 4)), 600) + random.uniform(0, 10)
            print(f"将在 {retry_delay:.1f} 秒后重启系统...")
            await asyncio.sleep(retry_delay)
            print("正在重启系统...")
            continue  # 继续循环，重新开始


# 添加看门狗监控函数
async def watchdog_monitor():
    """看门狗监控函数，检测系统是否长时间无响应"""
    global last_activity_time, conversation_ids_by_user, message_queue_task, last_message_queue_size
    
    # 添加消息处理任务监控
    last_conversation_ids_count = len(conversation_ids_by_user)
    last_check_time = time.time()
    last_queue_process_time = time.time()
    last_queue_size = message_queue.qsize() if message_queue else 0
    message_process_timeout = 600  # 10分钟
    
    while True:
        try:
            await asyncio.sleep(60)  # 每分钟检查一次
            
            current_time = time.time()
            elapsed = current_time - last_activity_time
            
            # 监控对话ID状态变化
            current_conversation_ids_count = len(conversation_ids_by_user)
            if current_conversation_ids_count != last_conversation_ids_count:
                print(f"对话ID数量发生变化: {last_conversation_ids_count} -> {current_conversation_ids_count}")
                last_conversation_ids_count = current_conversation_ids_count
                # 如果数量变化，认为系统是活跃的
                last_activity_time = current_time
            
            # 检查消息队列状态
            current_queue_size = message_queue.qsize() if message_queue else 0
            queue_changed = current_queue_size != last_queue_size
            
            # 检查消息队列，如果有消息待处理，认为系统是活跃的
            if not message_queue.empty():
                queue_size = message_queue.qsize()
                print(f"看门狗检测到消息队列中有 {queue_size} 条消息等待处理")
                
                # 检查队列大小是否长时间不变
                if (queue_size > 0 and queue_size == last_queue_size and 
                    current_time - last_queue_process_time > message_process_timeout):
                    print(f"警告: 消息队列大小 {queue_size} 已 {current_time - last_queue_process_time:.1f} 秒无变化")
                    print("看门狗认为消息处理任务可能已卡住，尝试重启消息处理任务")
                    
                    # 尝试重启消息处理任务
                    if message_queue_task and not message_queue_task.done():
                        # 取消现有任务
                        try:
                            message_queue_task.cancel()
                            await asyncio.sleep(5)  # 等待任务取消
                        except Exception as e:
                            print(f"取消消息处理任务时出错: {e}")
                            
                    # 创建新的消息处理任务
                    print("创建新的消息处理任务")
                    message_queue_task = asyncio.create_task(process_message_queue(telegram_application))
                    last_queue_process_time = current_time  # 重置时间
                    last_activity_time = current_time  # 更新活动时间
                    print("消息处理任务已重启")
                
                if queue_changed:
                    last_queue_size = current_queue_size
                    last_queue_process_time = current_time
                    last_activity_time = current_time  # 更新活动时间
                    continue
                
            # 检查消息处理任务状态
            if message_queue_task and not message_queue_task.done():
                # 验证任务实际在运行，不仅仅是存在
                if connection_monitor and current_time - connection_monitor.last_message_processed_time < 300:
                    print(f"看门狗检测到消息处理任务活跃（{(current_time - connection_monitor.last_message_processed_time):.1f}秒前有活动），重置活动时间")
                    last_activity_time = current_time
                    continue
                else:
                    # 任务存在但可能已经长时间无活动（5分钟以上）
                    if current_time - connection_monitor.last_message_processed_time > 300:
                        print(f"警告: 消息处理任务已 {current_time - connection_monitor.last_message_processed_time:.1f} 秒无活动，可能已卡住")
                        print("触发系统完全重启...")
                        
                        # 不只是重启消息处理任务，而是触发整个系统重连
                        if connection_monitor:
                            connection_monitor.is_healthy = False
                            try:
                                await connection_monitor._trigger_reconnect()
                                last_activity_time = current_time  # 更新活动时间
                                print("系统完全重启流程已启动")
                            except Exception as e:
                                print(f"系统重启失败: {e}")
                                # 触发主循环重启
                                raise Exception("触发主循环重启")
                        else:
                            # 直接触发主循环重启
                            raise Exception("触发主循环重启 - 无连接监控器")
            else:
                # 消息处理任务不存在或已完成，需要重新创建
                if not message_queue_task or message_queue_task.done():
                    print("消息处理任务不存在或已完成，创建新任务")
                    message_queue_task = asyncio.create_task(process_message_queue(telegram_application))
                    last_activity_time = current_time  # 更新活动时间
            
            # 定期保存状态数据，即使没有变化
            if current_time - last_check_time > 300:  # 每5分钟自动保存一次
                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                print("看门狗定期保存状态数据完成")
                last_check_time = current_time
            
            if elapsed > WATCHDOG_TIMEOUT:
                print(f"看门狗检测到系统 {elapsed:.1f} 秒无活动，触发重启...")
                
                # 在重启前保存所有状态
                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                print("看门狗在重启前保存了状态数据")
                
                # 强制重启整个系统
                if telegram_application:
                    # 尝试触发 connection_monitor 的重连逻辑
                    if connection_monitor:
                        connection_monitor.is_healthy = False
                        try:
                            await connection_monitor._trigger_reconnect()
                            # 重置活动时间，避免立即再次触发
                            last_activity_time = time.time()
                        except Exception as e:
                            print(f"看门狗触发重连失败: {e}")
                            # 如果触发重连失败，直接抛出异常，让主循环处理重启
                            raise Exception("Watchdog forced restart")
                else:
                    # 如果没有 telegram_application，也抛出异常让主循环处理
                    raise Exception("Watchdog forced restart - no application")
            
            # 如果已经超过 WATCHDOG_TIMEOUT 的一半时间无活动，记录警告
            elif elapsed > WATCHDOG_TIMEOUT / 2:
                print(f"警告: 系统已 {elapsed:.1f} 秒无活动")
                # 检查系统基本状态
                print(f"系统状态: 队列大小={message_queue.qsize()}, 对话数量={len(conversation_ids_by_user)}")
                print(f"消息处理任务状态: {'运行中' if message_queue_task and not message_queue_task.done() else '未运行'}")
                if connection_monitor:
                    print(f"连接状态: {'健康' if connection_monitor.is_healthy else '不健康'}, " 
                          f"最后心跳={current_time - connection_monitor.last_heartbeat:.1f}秒前, "
                          f"最后处理消息={current_time - connection_monitor.last_message_processed_time:.1f}秒前")
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"看门狗监控出错: {e}")
            await asyncio.sleep(30)  # 出错后等待一段时间再继续


# 修改 button_callback 函数
async def button_callback(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """处理按钮回调"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    try:
        await query.answer("正在处理...")

        current_api_key, current_api_key_alias = get_user_api_key(user_id)
        history_key = (user_id, current_api_key_alias)
        conversation_key = (user_id, current_api_key_alias)

        if query.data.startswith("save_memory_"):
            # 检查这个用户是否正在导入记忆
            if user_importing_memory.get(user_id, False):
                await query.edit_message_text("已有一个记忆导入任务正在进行，请等待完成后再试。")
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

                if filtered_history:
                    chat_content = "\n".join(filtered_history)

                    # 检查是否是由于配额限制触发的保存
                    if conversation_key in delayed_memory_tasks:
                        print(f"用户 {user_id} 的记忆将在5分钟后保存（由于配额限制）")
                        await query.edit_message_text(
                            "我需要休息一小会儿，5分钟后再来找你！\n"
                            "你先去忙别的吧，我们之后再聊~"
                        )
                        # 创建延迟任务
                        async def delayed_save():
                            print(f"开始执行延迟保存任务 - 用户: {user_id}")
                            await asyncio.sleep(300)  # 等待5分钟 (300秒)
                            try:
                                print(f"正在执行延迟保存 - 用户: {user_id}")
                                # 保存记忆到数据库
                                await save_memory(user_id, conversation_id, chat_content, current_api_key_alias)
                                print(f"记忆保存成功 - 用户: {user_id}")

                                # 清除当前对话ID
                                if conversation_key in conversation_ids_by_user:
                                    del conversation_ids_by_user[conversation_key]
                                    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)

                                # 设置导入状态
                                global is_importing_memory
                                is_importing_memory = True

                                try:
                                    # 导入记忆到新对话
                                    memory_with_prefix = "以下是过去的对话历史：\n" + chat_content
                                    await dify_stream_response(memory_with_prefix, int(user_id), context.bot)
                                    print(f"记忆导入成功 - 用户: {user_id}")
                                    await context.bot.send_message(
                                        chat_id=user_id,
                                        text="来了来了，我们继续吧~"
                                    )
                                except Exception as e:
                                    print(f"记忆导入时出错 - 用户: {user_id}, 错误: {e}")
                                    await context.bot.send_message(
                                        chat_id=user_id,
                                        text="哎呀，事情还是没做完，要不你再等等，让我再试试？"
                                    )
                                finally:
                                    is_importing_memory = False

                            except Exception as e:
                                print(f"延迟保存记忆时出错 - 用户: {user_id}, 错误: {e}")
                                await context.bot.send_message(
                                    chat_id=user_id,
                                    text="哎呀，事情还是没做完，要不你再等等，让我再试试？"
                                )
                            finally:
                                print(f"延迟保存任务完成 - 用户: {user_id}")
                                del delayed_memory_tasks[conversation_key]
                                user_importing_memory[user_id] = False

                        # 存储延迟任务
                        delayed_memory_tasks[conversation_key] = asyncio.create_task(delayed_save())
                        print(f"已创建延迟保存任务 - 用户: {user_id}")
                    else:
                        # 正常保存流程
                        await save_memory(user_id, conversation_id, chat_content, current_api_key_alias)
                        await query.edit_message_text("我需要休息一小会儿，5分钟后再来找你！\n"
                                                      "你先去忙别的吧，我们之后再聊~")
                        user_importing_memory[user_id] = False
                else:
                    await query.edit_message_text("咦，你好像没和我说过话呢...")
                    user_importing_memory[user_id] = False
            else:
                await query.edit_message_text("咦，你好像没和我说过话呢...")
                user_importing_memory[user_id] = False

        elif query.data == "new_conversation":
            # 用户选择不保存记忆，直接开始新对话
            # 清除当前对话ID和历史
            if conversation_key in conversation_ids_by_user:
                del conversation_ids_by_user[conversation_key]
                save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)

            if history_key in conversation_history:
                conversation_history[history_key] = []

            await query.edit_message_text("好的，让我们开始新的对话吧！")

            # 发送一个欢迎消息开启新对话
            await context.bot.send_message(
                chat_id=user_id,
                text="你可以继续和我聊天了！"
            )

    except Exception as e:
        print(f"按钮回调处理出错: {e}")
        await query.edit_message_text(
            "处理请求时出现错误，请稍后重试。\n"
            "如果问题持续存在，请联系管理员。"
        )
        user_importing_memory.pop(user_id, None)


async def save_memory_command(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """测试记忆保存流程的命令"""
    user_id = str(update.effective_user.id)
    if user_id in user_importing_memory:
        await update.message.reply_text("已有一个记忆操作正在进行，请等待完成后再试。")
        return

    try:
        user_importing_memory[user_id] = True
        chat_id = update.effective_chat.id

        processing_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="正在处理记忆存储请求...\n⏳ 请耐心等待，这可能需要一点时间。"
        )

        current_api_key, current_api_key_alias = get_user_api_key(user_id)
        history_key = (user_id, current_api_key_alias)
        conversation_key = (user_id, current_api_key_alias)

        if history_key in conversation_history and conversation_history[history_key]:
            filtered_history = [
                line for line in conversation_history[history_key]
                if not line.startswith("以下是过去的对话历史：")
            ]

            if filtered_history:
                chat_content = "\n".join(filtered_history)
                conversation_id = conversation_ids_by_user.get(conversation_key, 'new')

                # 保存当前记忆到数据库
                await save_memory(user_id, conversation_id, chat_content, current_api_key_alias)

                # 将记忆操作加入消息队列
                memory_content = "以下是过去的对话历史：\n" + "\n".join(filtered_history)
                await message_queue.put((update, context, "memory_operation", memory_content, None))

                await processing_msg.edit_text(
                    "记忆已保存并加入处理队列...\n"
                    "🔄 请等待系统处理。"
                )
            else:
                await processing_msg.edit_text("没有找到可以保存的有效对话历史。")
        else:
            await processing_msg.edit_text("没有找到可以保存的对话历史。")

    except Exception as e:
        print(f"保存记忆时出错: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="处理请求时出现错误，请稍后重试。"
        )
    finally:
        user_importing_memory.pop(user_id, None)


async def save_memory(user_id: str, conversation_id: str, chat_content: str, api_key_alias: str):
    """保存对话记忆到数据库"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                'INSERT OR REPLACE INTO chat_memories (user_id, conversation_id, api_key_alias, chat_content, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)',
                (user_id, conversation_id, api_key_alias, chat_content))
            await db.commit()
            print("记忆保存成功")
    except Exception as e:
        print(f"保存记忆时出错: {e}")
        raise


async def get_memory(user_id: str, conversation_id: str, api_key_alias: str):
    """从数据库获取对话记忆"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                'SELECT chat_content FROM chat_memories WHERE user_id = ? AND conversation_id = ? AND api_key_alias = ?',
                (user_id, conversation_id, api_key_alias)
            ) as cursor:
                result = await cursor.fetchone()
                if result:
                    print("记忆获取成功")
                    return result[0]
                return None
    except Exception as e:
        print(f"获取记忆时出错: {e}")
        return None


# 修改数据库初始化函数
async def init_db():
    """初始化数据库，创建必要的表"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            # 检查表是否存在
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_memories'") as cursor:
                table_exists = await cursor.fetchone()

                if not table_exists:
                    # 如果表不存在，创建新表
                    await db.execute('''
                        CREATE TABLE chat_memories (
                            user_id TEXT,
                            conversation_id TEXT,
                            api_key_alias TEXT,
                            chat_content TEXT,
                            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (user_id, conversation_id, api_key_alias)
                        )
                    ''')
                else:
                    # 如果表存在，检查是否需要迁移
                    async with db.execute("PRAGMA table_info(chat_memories)") as cursor:
                        columns = await cursor.fetchall()
                        has_created_at = any(col[1] == 'created_at' for col in columns)

                        if not has_created_at:
                            print("需要迁移数据库以添加 created_at 列")
                            # 创建新表
                            await db.execute('''
                                CREATE TABLE chat_memories_new (
                                    user_id TEXT,
                                    conversation_id TEXT,
                                    api_key_alias TEXT,
                                    chat_content TEXT,
                                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                                    PRIMARY KEY (user_id, conversation_id, api_key_alias)
                                )
                            ''')

                            # 复制旧数据
                            await db.execute('''
                                INSERT INTO chat_memories_new (user_id, conversation_id, api_key_alias, chat_content)
                                SELECT user_id, conversation_id, api_key_alias, chat_content FROM chat_memories
                            ''')

                            # 删除旧表
                            await db.execute('DROP TABLE chat_memories')

                            # 重命名新表
                            await db.execute('ALTER TABLE chat_memories_new RENAME TO chat_memories')

                            print("数据库迁移完成")

            await db.commit()
            print("数据库初始化成功")
    except Exception as e:
        print(f"数据库初始化错误: {e}")
        raise


# 修改清理函数
async def cleanup_old_data():
    """定期清理旧数据"""
    while True:
        try:
            # 清理数据库中的旧记录
            async with aiosqlite.connect(DB_FILE) as db:
                # 删除30天前的记录
                await db.execute('''
                    DELETE FROM chat_memories 
                    WHERE datetime(created_at) < datetime('now', '-30 days')
                ''')
                await db.commit()

            # 清理内存中的旧对话历史
            for key in list(conversation_history.keys()):
                if len(conversation_history[key]) > MEMORY_CONFIG['max_history_length']:
                    conversation_history[key] = conversation_history[key][-MEMORY_CONFIG['max_history_length']:]

            # 强制垃圾回收
            gc.collect()

        except Exception as e:
            print(f"清理数据时出错: {e}")
            print("将在下次循环重试")

        await asyncio.sleep(3600)  # 每小时清理一次

# 添加这个函数来注册所有处理程序
def register_handlers(app):
    """注册所有消息处理程序"""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_api_key))
    app.add_handler(CommandHandler("block", block_user))
    app.add_handler(CommandHandler("unblock", unblock_user))
    app.add_handler(CommandHandler("clean", clean_conversations))
    app.add_handler(CommandHandler("save", save_memory_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    print("所有处理程序已注册")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
