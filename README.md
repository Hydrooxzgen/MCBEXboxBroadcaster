<p align="center">
  <img src="https://img.shields.io/badge/📡-MCXboxBroadcast-5865F2?style=for-the-badge" alt="MCXboxBroadcast">
</p>

<p align="center">
  <b>如果这个项目对你有帮助，请点一个 ⭐ Star 支持一下吧！</b><br/>
  <b>If you find this project useful, please give it a ⭐ Star — it means a lot!</b>
</p>

<p align="center">
  <a href="https://github.com/Hydrooxzgen/MCBEXboxBroadcaster/stargazers">
    <img src="https://img.shields.io/github/stars/Hydrooxzgen/MCBEXboxBroadcaster?style=social" alt="Star MCBEXboxBroadcaster">
  </a>
</p>

---

# 📡 MCBEXboxBroadcaster

> 一个把现有的 Geyser / Bedrock 服务器广播到 Xbox Live 网络的工具（**Python 移植版**）。
> A simple tool that broadcasts an existing [Geyser](https://github.com/GeyserMC/Geyser) / Bedrock server over Xbox Live (**Python port**).

本仓库是 [MCXboxBroadcast/Broadcaster](https://github.com/MCXboxBroadcast/Broadcaster) 的 **Python 重写版**，用 Python 忠实还原了原版 Java 项目的全部核心功能：Xbox Live 登录、会话广播、好友同步、NetherNet 重定向等。<br>**别忘了给[原作者:MCXboxBroadcast](https://github.com/MCXboxBroadcast)点点star噢。**

This repository is a **Python rewrite** of [MCXboxBroadcast/Broadcaster](https://github.com/MCXboxBroadcast/Broadcaster) by [@rtm516](https://github.com/rtm516). It faithfully ports all core features — Xbox Live auth, session broadcasting, friend sync and NetherNet redirect — from the original Java project.<br>**Don't forget to give [Original Author: MCXboxBroadcast](https://github.com/MCXboxBroadcast) a star.**

登录账号的好友会在游戏内看到该服务器并可一键加入会话。This shows up to the authenticated account's friends in-game as a joinable session.

![Example screenshot](https://user-images.githubusercontent.com/5401186/159083033-b965bfba-de17-4708-8979-1f33bfd5fa28.png)

---

## ⭐ 原项目 / Original Project

**本项目基于以下原项目修改（Fork）而来，感谢原作者的无私贡献！**

**This project is forked from the following original project — huge thanks to the original author!**

- **原项目 / Original repo**: [MCXboxBroadcast/Broadcaster](https://github.com/MCXboxBroadcast/Broadcaster)
- **原作者 / Original author**: [@rtm516](https://github.com/rtm516)
- **原项目许可证 / License**: [GPL-3.0](LICENSE)

如果你需要 **Java 版**（Geyser 扩展 / 独立 jar / Web Manager），请前往原项目；本仓库仅提供 **Python 版**。

> If you need the **Java version** (Geyser extension / standalone jar / Web Manager), please visit the original repository. This repo only provides the **Python version**.

---

## ✨ 特性 / Features

- 📋 **MOTD 与服务信息同步 / MOTD and server details syncing**
- 🤝 **自动好友管理 / Automatic friend list management**（自动关注回、自动取关、过期清理）
- 👁️ **会话可见性可配置 / Configurable session visibility**（`friends` 仅好友 / `public` 所有人）
- 🎮 **在 Xbox 应用和网站上显示为在线并正在游玩 Minecraft / Shows as online and playing Minecraft in the Xbox app and website**
- 👥 **多账号支持 / Multi-account support**（子会话）
- 🖼️ **上传自定义头像图片 / Uploading of a custom image for the account**
- 🔁 **RTA 心跳保活、会话自动重建 / RTA heartbeat and automatic session re-creation**
- 🌐 **NetherNet 重定向（WebRTC）/ NetherNet redirect via WebRTC**（让玩家从 Xbox 直接加入你的服务器）

---

## 📦 环境要求 / Requirements

| 项目 / Item | 要求 / Requirement |
|------|------|
| Python | 3.10+（推荐 3.11+ / recommended 3.11+） |
| 目标服务器 / Target server | 任意 Bedrock 服务器（如 Geyser / BDS），需可公网访问 / Any Bedrock server (e.g. Geyser / BDS), must be publicly reachable |

依赖清单 / Dependencies（`requirements.txt`）：`requests`、`websockets`、`PyYAML`、`aiortc`

> ⚠️ `aiortc` 是**必需依赖**（对应原版 Java 的 kastle 库）。未安装时 NetherNet 传输不可用，会话无法被加入。

---

## 🚀 快速开始 / Quick Start

### 1. 克隆并安装依赖 / Clone & install dependencies

```bash
git clone https://github.com/Hydrooxzgen/MCBEXboxBroadcaster.git
cd MCBEXboxBroadcaster
pip install -r python/requirements.txt
```

### 2. 准备配置文件 / Prepare the config

```bash
cp python/config.yml.example config.yml
```

编辑 `config.yml`，主要修改以下项：

```yaml
session:
  remoteAddress: 你的服务器公网IP    # 改成你的服务器地址
  remotePort: auto                  # 端口，通常留 auto
  visibility: friends               # friends=仅好友可加入（默认）| public=所有人可加入
  sessionInfo:
    hostName: 服务器名              # 好友列表里显示的名称
```

### 3. 运行 / Run

```bash
# 方式一：在仓库根目录直接运行（推荐）/ Option 1: run from the repo root (recommended)
python main.py config.yml

# 方式二：进入包目录运行 / Option 2: run inside the package dir
cd python && python -m mcxboxbroadcast config.yml
```

首次运行会进行**微软账号设备码登录**：终端会打印一个链接和代码，用浏览器打开并输入代码授权即可。Token 会自动缓存（`./cache`），后续自动续期，无需重复登录。

On first run you will be asked to sign in with the **Microsoft device-code flow**: open the printed link, enter the code, and authorize. Tokens are cached in `./cache` and auto-refreshed.

> 也可以 `cd python && pip install -e .` 安装为命令行工具，之后直接运行 `mcxboxbroadcast config.yml`。

> You can also install it as a CLI tool with `cd python && pip install -e .`, then run `mcxboxbroadcast config.yml` directly.

---

## 🎮 运行命令 / Commands

| Command | Description |
| --- | --- |
| `quit` / `exit` | 退出程序 / Exits the program |
| `restart` | 重启会话 / Restarts the session |
| `list` | 列出所有会话 / Lists all sessions |
| `dump` | 打印当前会话信息 / Dumps the current session data |
| `help` | 显示帮助 / Shows this help |

---

## 🖼️ 自定义头像图片 / Custom Image

在 `config.yml` 同目录放置 `screenshot.jpg`，即可为该账号设置自定义资料图。

You can add a custom image to the profile page for the account by placing a `screenshot.jpg` in the same directory as the `config.yml`.

推荐设置：`1200x675`、质量 `90`、色度子采样 `4:2:0`。Xbox 服务器可能需要几分钟才会生效。

The best settings for this image are `1200x675`, quality `90` and chroma subsampling `4:2:0`. This can take a few minutes to update on the Xbox Live servers.

---

## ⚠️ 免责声明 / DISCLAIMER

你自行承担使用本项目的风险，贡献者不对软件造成的任何损害或损失负责。由于我们模拟了客户端的部分功能（可能违反或可能不违反服务条款），建议使用**小号**运行本工具。

You use this project at your own risk, the contributors are not responsible for any damage or loss caused by the software. We suggest you use an **alt account** for running the tool in case the account is banned, as we emulate some features of a client which may or may not be against TOS.

---

## 📜 许可证 / License

[GPL-3.0](LICENSE) — 与原项目一致 / Same as the original project.

原项目 Copyright © [@rtm516](https://github.com/rtm516) & MCXboxBroadcast contributors. 本仓库为 Python 移植版。
