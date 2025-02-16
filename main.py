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


# --- 代码部分 ---
message_queue = asyncio.Queue()
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

# 添加一个全局变量来存储延迟保存的任务
delayed_memory_tasks = {}

# 修改 TELEGRAM_PROXY 配置
TELEGRAM_PROXY = {
    'url': 'socks5://127.0.0.1:10808',  # 改用 socks5 协议
    'connect_timeout': 30,  # 连接超时时间（秒）
    'read_timeout': 30,    # 读取超时时间（秒）
    'write_timeout': 30,   # 写入超时时间（秒）
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

def load_data():
    """加载保存的会话数据和 API 密钥。"""
    global API_KEYS, delayed_memory_tasks, conversation_history  # 添加 conversation_history
    try:
        with open(DATA_FILE, "rb") as f:
            data = pickle.load(f)
            conversation_ids_by_user = data.get('conversation_ids_by_user', {})
            loaded_api_keys = data.get('api_keys', {})
            API_KEYS.update(loaded_api_keys)
            user_api_keys = data.get('user_api_keys', {})
            blocked_users = data.get('blocked_users', set())
            # 加载对话历史
            conversation_history = data.get('conversation_history', {})
            # 确保 delayed_memory_tasks 被初始化为空字典
            delayed_memory_tasks = {}
            return conversation_ids_by_user, API_KEYS, user_api_keys, blocked_users
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
    global conversation_history, is_importing_memory
    user_id = str(chat_id)
    current_api_key, current_api_key_alias = get_user_api_key(user_id)
    history_key = (user_id, current_api_key_alias)
    conversation_key = (user_id, current_api_key_alias)
    
    # 初始化对话历史
    if history_key not in conversation_history:
        conversation_history[history_key] = []
    
    # 检查历史记录长度，如果超过限制则保留最新的部分
    max_history_length = 1000  # 设置一个合理的历史记录长度限制
    if len(conversation_history[history_key]) > max_history_length:
        # 保留最新的记录
        conversation_history[history_key] = conversation_history[history_key][-max_history_length:]
        print(f"历史记录超出限制，已截取最新的 {max_history_length} 条记录")
    
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
    
    full_text_response = ""
    typing_message = None
    last_typing_update = 0
    typing_interval = 4

    try:
        typing_message = await bot.send_chat_action(chat_id=chat_id, action="typing")
        last_typing_update = time.time()
        
        async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
            response = await client.post(DIFY_API_URL + "/chat-messages", headers=headers, json=data)

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
                                        text="检测到配额限制，如果你选择保存记忆，系统会在5分钟后处理。"
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
                                    conversation_ids_by_user[conversation_key] = response_conversation_id
                                    save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
                                    print(f"Stored/Updated conversation_id: {response_conversation_id} for user: {user_id}, role: {current_api_key_alias}")
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
                        text="检测到配额限制，如果你选择保存记忆，系统会在5分钟后处理。"
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

    except Exception as e:
        print(f"Error in dify_stream_response: {e}")
        # 连接错误等异常也提供保存记忆的选项
        if conversation_key in conversation_ids_by_user:
            del conversation_ids_by_user[conversation_key]
            save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
        await offer_save_memory(bot, chat_id, conversation_key)
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
        text="对话出现异常，是否保存当前记忆？",
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
            
            if not is_connected:
                print("Telegram 连接断开，消息处理暂停。")
                await message_queue.put((update, context, message_type, message_content, file_info))
                message_queue.task_done()
                await asyncio.sleep(1)
                continue

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
                await message_queue.put((update, context, message_type, message_content, file_info))
            except Exception as e:
                print(f"Error in process_message_queue during dify_stream_response: {e}")
                try:
                    await bot.send_message(chat_id=chat_id, text="处理消息时发生错误，请稍后再试。")
                except:
                    pass

            # 处理完消息后等待 rate_limit 秒
            await asyncio.sleep(rate_limit)
            
            # 只在处理完所有消息后调用一次 task_done
            for _ in range(len(current_user_queue)):
                message_queue.task_done()

        except Exception as e:
            print(f"Unexpected error in process_message_queue: {e}")
            await asyncio.sleep(5)
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


async def check_telegram_connection(application: Application):
    """检查 Telegram 连接状态"""
    global is_connected
    while True:
        try:
            # 修改代理配置格式
            async with httpx.AsyncClient(
                proxy=TELEGRAM_PROXY['url'],  # 直接使用 proxy 参数
                timeout=30.0
            ) as client:
                await application.bot.get_me()
                
            if not is_connected:
                print("Telegram connection restored.")
                is_connected = True
                user_last_processed_time.clear()
                
        except Exception as e:
            if is_connected:
                print(f"Telegram connection lost: {e}")
                is_connected = False
            else:
                print(f"Telegram connection check error: {e}")
        
        await asyncio.sleep(5)


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
    retry_delay = 10
    max_retry_delay = 300

    while True:
        try:
            if telegram_application is None:
                # 使用新的代理配置方式
                telegram_application = (
                    Application.builder()
                    .token(TELEGRAM_BOT_TOKEN)
                    .proxy(TELEGRAM_PROXY['url'])  # 使用 proxy 而不是 proxy_url
                    .connect_timeout(TELEGRAM_PROXY['connect_timeout'])
                    .read_timeout(TELEGRAM_PROXY['read_timeout'])
                    .write_timeout(TELEGRAM_PROXY['write_timeout'])
                    .get_updates_read_timeout(42)
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
                            "由于配额限制，系统将在5分钟后处理记忆保存。\n"
                            "你可以先去做别的事，系统会自动处理。\n"
                            "处理完成后对话会自动继续。"
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
                                        text="✅ 记忆已成功保存并导入！\n现在你可以继续新的对话了。"
                                    )
                                except Exception as e:
                                    print(f"记忆导入时出错 - 用户: {user_id}, 错误: {e}")
                                    await context.bot.send_message(
                                        chat_id=user_id,
                                        text="记忆已保存，但导入时出现错误，请稍后重试。"
                                    )
                                finally:
                                    is_importing_memory = False
                                
                            except Exception as e:
                                print(f"延迟保存记忆时出错 - 用户: {user_id}, 错误: {e}")
                                await context.bot.send_message(
                                    chat_id=user_id,
                                    text="❌ 保存记忆时出现错误，请稍后重试。"
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
                        await query.edit_message_text("✅ 记忆已保存！\n现在你可以继续新的对话了。")
                        user_importing_memory[user_id] = False
                else:
                    await query.edit_message_text("没有找到可以保存的有效对话历史。")
                    user_importing_memory[user_id] = False
            else:
                await query.edit_message_text("没有找到可以保存的对话历史。")
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
                'INSERT OR REPLACE INTO chat_memories (user_id, conversation_id, api_key_alias, chat_content) VALUES (?, ?, ?, ?)',
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        save_data(conversation_ids_by_user, api_keys, user_api_keys, blocked_users)
