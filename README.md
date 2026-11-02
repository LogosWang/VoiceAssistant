# Carnot

macOS 桌面助手：屏幕上常驻一个人物，可以语音唤醒，也可以打字。接入 Claude、ChatGPT、Gemini、ZCode、DeepSeek、豆包，桌面 App 和 VS Code 插件共用同一套记忆和 skill。

- **语音：** 喊 "Carnot"（卡诺）唤醒，语音识别在本机离线运行。回声消除会去掉电脑正在播放的声音，不影响音量和音质。
- **界面：** 悬浮在所有桌面之上，带字幕和输入框，可附加文件和文件夹；思考时身后的蒸汽机会运转。
- **远程：** 接入 Telegram 和飞书机器人，用手机发文字、图片、文件和语音，也能让它把电脑上的文件发回手机（Telegram 最大 50 MB，飞书 30 MB）。
- **会话：** 菜单栏可以新建、切换、重命名和删除对话。
- **VS Code：** 侧边栏对话，输入 `/` 选 skill，可附带当前文件。
- **共享：** 记忆在 `~/.carnot/memory`，skill 在 `~/.claude/skills` 和 `~/.agents/skills`，换 AI、换客户端都是同一套。

## 安装

**最简单：** 下载 `Carnot-Installer.pkg`，双击安装。不需要 Homebrew 或开发工具，语音模型和运行环境都在包里。要求 Apple 芯片的 Mac 和 macOS 14 以上。

这个安装包没有经过 Apple 公证，第一次双击会被系统拦下。到"系统设置 → 隐私与安全性"，在页面下方点"仍要打开"，再输入密码。

**从源码安装：** 需要 macOS 13 以上和 [Homebrew](https://brew.sh)。其余依赖由脚本安装，首次约需下载 1 GB 模型。

```bash
git clone https://github.com/LogosWang/VoiceAssistant.git ~/.claude/skills/carnot-voice
~/.claude/skills/carnot-voice/install.sh
```

放在 `~/.claude/skills` 下，Claude Code 就能把它当 skill 用，也可以放在别处。安装后不要移动这个文件夹；如果移动了，重新运行一次 `install.sh`。

装好后：

1. 从"应用程序"打开 Carnot，允许麦克风和屏幕与系统录音，然后退出再打开一次。
2. 在 VS Code 里执行 `Developer: Reload Window`，左侧活动栏会出现 Carnot。
3. 点输入框左边的按钮，登录要用的 AI 或填 API Key。

App 默认开机自启，可以在菜单栏图标或右键人物的菜单里关闭。

## 接入的 AI

| AI | 登录方式 | 运行方式 |
|---|---|---|
| Claude | 订阅或 API Key | Claude Code |
| ChatGPT | 订阅或 OpenAI API Key | Codex 命令行（在设置里安装） |
| Gemini | Google 账号或 API Key | Gemini 命令行（在设置里安装）。免费和 Google One 账号已被转到 Antigravity CLI，这类账号请用 Key |
| ZCode | GLM Coding Plan 的 API Key | Claude Code + Z.ai 接口 |
| DeepSeek | API Key | Claude Code + DeepSeek 接口 |
| 豆包 | 火山方舟 Coding Plan 的 API Key | Claude Code + 方舟接口 |

API Key 只存在本机钥匙串里。

## 自己构建安装包

```bash
packaging/build_pkg.sh    # 生成 dist/Carnot-Installer.pkg
```

需要 Apple 芯片、Xcode 命令行工具和 Node，并且先运行过一次 `app/build.sh`（它会创建签名证书）。安装包约 200 MB，不要提交进 git，请作为 GitHub Release 发布。

## 需要知道

- **权限：** 默认"自动"，常规操作自动进行，高风险操作会被拦下。在 App 的连接设置和 VS Code 聊天框里都可以切换（只读规划、可改文件、自动、完全放行），两边各自独立，从下一条指令开始生效。"完全放行"后 AI 可以不经确认执行任何操作，而旁边任何人都能喊唤醒词，切换时会要求确认；没有语音指令可以改权限。
- **远程机器人：** 能给机器人发消息，就可能操作这台电脑。所以必须先用电脑屏幕上的配对码配对，只接受私聊，权限最高到"自动"，不能完全放行；Token 只存在钥匙串里。密钥、凭据和 `.env` 这类文件不能作为文件发出，但这不是完整的防护：AI 读到的内容仍可能写进文字回复。回复不朗读，屏幕也不显示消息内容。电脑要开着、Carnot 要在运行。飞书需要企业自建应用并启用长连接；停用飞书机器人后连接要到重启才断开，这期间只是忽略消息。
- **数据：** 指令、附件、记忆索引和 AI 打开的记忆文件，都会发给你选的那家 AI；回复文字默认由 edge-tts 转语音，会发给微软。
- **屏幕录制图标：** 回声消除要读取系统音频，所以运行时菜单栏会显示录屏图标。点输入框上的麦克风按钮关闭语音模式后，麦克风和音频采集都会停止。
- **记忆不跨电脑同步：** 换电脑要自己带上 `~/.carnot/memory`。

## 设置和排错

设置在 `server/config.toml`，日志在 `~/Library/Logs/Carnot/carnot.log`。更多见 [SKILL.md](SKILL.md)。

## 卸载

```bash
osascript -e 'quit app "Carnot"'
rm -rf /Applications/Carnot.app "$HOME/Library/Application Support/Carnot"
tccutil reset All com.carnot.assistant
"/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" --uninstall-extension carnot-local.carnot
security delete-identity -c "Carnot Local Signing"
```

记忆在 `~/.carnot`，要不要删由你决定。
