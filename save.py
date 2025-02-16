import json
import os
import re
import sys

def get_script_dir():
    """获取脚本或 EXE 文件所在的目录。"""
    if getattr(sys, 'frozen', False):  # 检查是否是打包后的 EXE
        # 打包后的 EXE
        application_path = sys.executable
        return os.path.dirname(application_path)
    else:
        # 正常 Python 脚本
        return os.path.dirname(os.path.abspath(__file__))

def convert_to_txt(output_txt_path, bot_name="FVN_Chat"):
    """
    将程序目录下的'result.json' 聊天记录转换为 TXT 格式，
    过滤特定内容，合并连续发言，并在开头添加指定文本，优化空行处理。

    Args:
        output_txt_path (str): 输出 TXT 文件的路径。
        bot_name (str): 机器人的名字, 默认为 "FVN_Chat".
    """

    current_dir = get_script_dir()  # 使用 get_script_dir() 函数
    json_file_path = os.path.join(current_dir, 'result.json')

    try:
        with open(json_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading JSON file: {e}")
        return

    with open(output_txt_path, 'w', encoding='utf-8') as f:
        f.write("以下是过去的对话历史：\n")
        last_speaker = None

        for message in data.get('messages', []):
            # ... (其余代码与之前相同) ...
            if message.get('type') == 'message':
                text_content = message.get('text', '')

                if isinstance(text_content, list):
                    filtered_texts = []
                    for item in text_content:
                        if isinstance(item, dict) and item.get('type') == 'plain':
                            filtered_texts.append(item.get('text', ''))
                        elif isinstance(item, str):
                            filtered_texts.append(item)
                    text = "".join(filtered_texts)
                else:
                    text = text_content

                if message.get('from') == bot_name and (
                    "请稍等" in text or
                    "正在思考，请稍候..." in text or
                    "处理 Dify API" in text or
                    "哎哟等我打打字好不好" in text or
                    "Dify API request failed" in text or
                    "处理 Dify API 响应时发生意外错误" in text or
                    "记忆已保存并加入处理队列" in text or
                    "没有找到可以保存的对话历史" in text or
                    "没有找到可以保存的有效对话历史。" in text or
                    "你的 Dify API Key 已切换为" in text or
                    ("好嘞，让" in text and "来跟你聊吧" in text) or
                    "呜呜，今天的流量已经用光了" in text or
                    "抱歉啦，我现在有点累了，需要休息一下" in text or
                    "对方似乎有点忙，是否继续对话？" in text or
                    "我需要休息一小会儿，5分钟后再来找你！" in text or
                    "来了来了，我们继续吧~" in text or
                    "事情还是没做完，要不你再等等，让我再试试？" in text or
                    "已有一个记忆导入任务正在进行，请等待完成后再试。" in text or
                    "处理请求时出现错误，请稍后重试。" in text or
                    "处理消息时发生错误，请稍后再试。" in text or
                    "哎呀，出错了，稍后再找我吧。" in text or
                    "看不懂你发的啥捏~" in text or
                    "这个文件我打不开呀，抱歉啦~" in text or
                    "处理文件的时候出了点小问题..." in text or
                    "Error uploading file:"in text or
                    "文件上传失败 (尝试"in text or
                    "达到最大重试次数。"in text or
                    "Error in dify_stream_response:"in text or
                    "JSONDecodeError:"in text or
                    "Problem chunk:"in text or
                    "Dify API status code:"in text or
                    "400 错误详情:"in text or
                    "Error loading data from"in text or
                    "Unexpected error loading from pickle:"in text or
                    "Error saving data to"in text or
                    "Error in process_message_queue during dify_stream_response:"in text or
                    "Unexpected error in process_message_queue:"in text or
                    "处理记忆操作时出错:"in text or
                    "处理记忆时出现错误，请稍后重试。"in text or
                    "文件上传/处理错误:" in text or
                    "保存记忆时出错:" in text or
                    "获取记忆时出错:"in text or
                     "按钮回调处理出错:"in text or
                     "延迟保存记忆时出错 - 用户:"in text or
                     "记忆导入时出错 - 用户:" in text or
                    "Connection lost:" in text or
                    "Telegram connection check error:"in text or
                    "Telegram connection restored."in text or
                    "没有权限执行此操作。"in text or
                    "错误：参数 admin_id 缺失"in text or
                    "插件 packages.astrbot.main_op 报错" in text or
                    "处理请求时出现错误，请稍后重试。\n如果问题持续存在，请联系管理员。" in text or
                    "正在处理记忆存储请求...\n⏳ 请耐心等待，这可能需要一点时间。"in text or
                    "记忆已保存并加入处理队列...\n🔄 请等待系统处理。" in text or
                    "好的，让我们开始新的对话吧！" in text or
                    "你可以继续和我聊天了！"in text
                    ):
                    continue

                if "以下是过去的对话历史：" in text:
                    text = text.replace("以下是过去的对话历史：", "").strip()

                if isinstance(text_content, list):
                    if any(isinstance(item, dict) and item.get('type') == 'bot_command' and item.get('text',"").startswith("/") for item in text_content):
                        continue
                elif isinstance(text_content, str) and re.search(r'^\s*/(start|set|block|unblock|clean|save)\b', text_content):
                    continue
                
                text = text.strip() # 关键步骤1: 去除每条消息的首尾空白
                if not text:  # 关键步骤2: 过滤空消息
                    continue

                speaker = "user" if message.get('from') != bot_name else "assistant"

                if speaker == last_speaker:
                    f.write(" " + text)
                else:
                    if last_speaker is not None:
                        f.write("\n")  # 仅在需要时换行
                    f.write(f"{speaker}: {text}")
                    last_speaker = speaker
        f.write("\n") # 保持末尾换行
        

if __name__ == "__main__":
    output_txt_path = 'conversation_history.txt'
    convert_to_txt(output_txt_path)
    print(f"转换完成，已保存到 {output_txt_path}")
