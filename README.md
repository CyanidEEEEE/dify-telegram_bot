# dify-telegram_bot

## 简介

本项目是一个基于 Telegram 和 [Dify](https://dify.ai/) 的聊天机器人。它允许用户通过 Telegram 与 Dify 上的 AI 应用进行交互，支持文本、图片、语音、文件等多种消息类型，并具有以下特性：

*   **流式响应:** 支持 Dify 的流式响应，提供更快的交互体验。
*   **多用户支持:** 可以同时与多个 Telegram 用户进行对话。
*   **会话管理:** 自动管理与 Dify 的会话，支持上下文理解, 并支持保存和导入历史对话。
*   **多角色 (API 密钥) 切换:** 可以在 Telegram 中通过命令切换不同的 Dify API 密钥，实现与不同“人格”的 AI 交互。
*   **消息队列:** 使用消息队列处理并发消息，提高稳定性。
*   **速率限制:** 限制发送消息的频率，防止滥用。
*   **文件上传:** 支持将 Telegram 中的文件上传到 Dify，并自动处理 Dify 返回的图片和语音消息。
*   **管理员功能:**
    *   拉黑/取消拉黑用户
    *   清除所有用户的聊天 ID 记录
*   **错误处理和重连:** 遇到网络问题或其他错误时，自动重试或重新连接。
*   **持久化存储**: 将会话、黑名单等数据保存到本地 (pickle 文件和 SQLite 数据库)，重启后可恢复。
*   **对话记忆**:  支持通过内联按钮保存和导入特定对话的记忆。

## 功能

*   与 Dify AI 应用进行文本、图片、语音、文件对话。
*   通过 `/set` 命令切换 Dify API 密钥（人格），例如：`/set dave`。
*   管理员可以通过 `/block` 命令拉黑用户。
*   管理员可以通过 `/unblock` 命令取消拉黑用户。
*   管理员可以通过 `/clean` 命令清除所有用户的聊天 ID 记录。
*   支持 Dify 的流式响应。
*   自动处理与 Dify 的会话。
*  支持保存对话记忆到数据库，并在需要时导入：
    *  在对话出错或者机器人重启后，通过内联键盘选择是否保存/载入记忆.
    *  通过 `/save` 命令手动保存和载入当前对话的记忆。

## 安装

1.  **克隆仓库:**

    ```bash
    git clone https://github.com/your-username/your-repo-name.git
    cd your-repo-name
    ```

2.  **安装依赖:**

    ```bash
    pip install -r requirements.txt
    ```

## 配置

1.  **Telegram Bot Token:**
    *   在 Telegram 中与 [@BotFather](https://t.me/BotFather) 对话，创建一个新的机器人，获取 Bot Token。
    *   将 `TELEGRAM_BOT_TOKEN` 变量的值替换为你的 Bot Token。

2.  **Dify API URL:**
    *   将 `DIFY_API_URL` 变量的值替换为你的 Dify API 地址。

3.  **Dify API 密钥:**
    *   在 `API_KEYS` 字典中添加你的 Dify API 密钥，可以添加多个，并为每个密钥设置一个别名 (例如, `'dave': 'app-WAxYlivp...'`)。
    *   `DEFAULT_API_KEY_ALIAS` 变量指定默认使用的 API 密钥别名 (例如, `'dave'`)。

4.  **管理员 ID:**
    *   将 `ADMIN_IDS` 列表中的 ID 替换为你自己的 Telegram 用户 ID (例如, `['103']`)。

5.  **(可选) 代理:**
    *   HTTPX会自动识别系统代理。

## 使用

1.  **运行机器人:**

    ```bash
    python main.py
    ```

2.  **在 Telegram 中与机器人对话:**

    *   向机器人发送 `/start` 命令，开始对话。
    *   发送文本、图片、语音或文件消息。
    *   使用 `/set <api_key_alias>` 命令切换 Dify API 密钥（人格），例如 `/set dave`。

3.  **管理员命令（仅限管理员）:**
    *   `/block <user_id>`：拉黑用户。
    *   `/unblock <user_id>`：取消拉黑用户。
    *   `/clean`：清除所有用户的聊天 ID 记录。

4. **对话记忆:**
    * 当对话出现问题，或者 Dify 报告对话不存在时，机器人会自动询问是否保存/载入当前对话记忆。
    *  你也可以随时使用 `/save` 命令来手动保存和载入当前对话的记忆。

## 贡献

欢迎提交 issue 或 pull request。  如果你有任何改进建议或发现了 bug，请随时告知。
