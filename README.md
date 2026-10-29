# Carnot 语音助手（Claude Code skill）

用 Mac 麦克风喊 "Carnot"（卡诺）唤醒，说话交给 Claude Code，回答用语音读出来。语音识别在本机离线运行。电脑正在播放的声音会被回声消除去掉，播放的音量和音质不受影响。

## 部署到新电脑

1. 把整个 `carnot-voice` 文件夹拷到新电脑的 `~/.claude/skills/carnot-voice`。这样 Claude Code 能用这个 skill，也可以放在别的位置。
2. 运行安装脚本，大约需要几分钟，会下载 1 GB 模型：
   ```
   ~/.claude/skills/carnot-voice/install.sh
   ```
   需要先装好：macOS 13 以上、Homebrew、Claude Code（并已登录）。Xcode 命令行工具和 Python 由脚本检查并提示安装。
3. 启动：
   ```
   ~/.claude/skills/carnot-voice/run.sh
   ```
   第一次启动时，要给终端 App 授权 **麦克风** 和 **屏幕与系统录音**。授权后重开终端。

设置文件是 `app/config.toml`，包括 Claude 工作目录、模型、麦克风、唤醒灵敏度和声音。也可以直接让 Claude Code 帮你改，它会按 SKILL.md 操作。
