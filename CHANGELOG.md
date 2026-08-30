# 更新日志 / Changelog

## v1.0.1 (2026-08-30)

完全重写版本。以 [Java 原版](https://github.com/MCXboxBroadcast/Broadcaster) 为唯一依据，
从零重写了整个 Python 实现，修复了旧版本的全部已知问题，并经真实账号实测全链路可用
（好友加入 → NetherNet 连接 → 登录验证 → 转移进入目标服务器）。

### 新增 / 重写

- **认证链完整实现**（对应 MinecraftAuth 5.x 的标题客户端 SISU 流程）：
  MSA 设备码登录 → Xbox 设备令牌（ECDSA-P256 请求签名头）→ SISU 授权 →
  多 Relying Party XSTS 令牌 → PlayFab 登录 → Minecraft 会话 + 多人令牌（pmid）
- **令牌缓存与自动续期**：`cache/cache.json` 持久化全部令牌与密钥，
  `invalid_grant` 时自动清除缓存并重新走设备码登录
- **Xbox LIVE 会话广播**：会话创建/定时刷新/presence 心跳/断线自动重建/自动重建会话
- **NetherNet 转移服务**：
  - Franchise 信令客户端（`wss://signal.franchise.minecraft-services.net`，MCToken 鉴权，
    JSON-RPC，TURN 凭据获取，自动重连）
  - aiortc WebRTC（应答 SDP + `a=identity` ES384 身份断言 + ICE 双向）
  - Bedrock 协议 2169 最小实现：登录链验证（Mojang RS256 JWKS + ES256/ES384 客户端数据）、
    资源包流程、StartGame + Transfer 转移
  - StartGame 编码与 CloudburstMC 3.0.Beta13 原库输出**逐字节一致**
- **好友管理**：自动同意好友请求（RTA 实时通知）、自动回关、自动取关、
  好友过期清理（SQLite 玩家历史）、加入后自动发送会话邀请、
  好友列表满/隐私限制/频率限制的完整错误处理
- **多账号子会话**：`accounts add/remove <id>` 命令，每个子账号独立广播 2000 好友
- **服务器查询**：RakNet ping 同步 MOTD/人数 + checker.geysermc.org 网络回退
- **配置系统**：首次运行自动生成带注释的 `config.yml`，保留注释回写，v1→v2 迁移
- **通知**：Slack/Discord webhook（会话过期、好友限制通知）
- **控制台命令**：`restart` / `dumpsession` / `accounts list|add|remove` / `version` / `help`
- **日志分级**：默认精简日志（启动/认证/会话/玩家连接/转移/好友事件），
  `--full-log` 启用全部协议细节
- **文档**：`PORTING_NOTES.md` 记录全部协议细节（认证链、信令、WebRTC、包格式），
  README 重写

### 修复（相对旧 Python 版）

- 信令端点错误（旧版 `rpc-prod.xboxlive.com` 为无效端点，改为正确的
  franchise messaging 信令并使用 MCToken 鉴权）
- 登录包新版格式（`AuthenticationType`/`Token`）解析与 Mojang RS256 JWKS 验签
- 压缩帧布局（`[压缩头][长度][数据]`）与 raw deflate（非 zlib 包装）
- 协议号大端序、字符串长度 varint、PlayStatus/资源包状态映射
- StartGame 多处字段错误（dayCycle、limitedWorld、gamerule editable 标志、
  experiments intLE、NBT 根名、广播模式序号、移除已废弃的 tickDeath 字段），
  现与 CloudburstMC 原库输出逐字节一致
- ES256 签名 DER↔P1363 转换缺省前导零补丁（导致约半数验签失败）
- pmsgId 来源错误（应取自 MCToken JWT 的 `pmid` 声明）
- Transfer 时机：StartGame 后需延迟约 1.5 秒再发 Transfer，否则客户端忽略
- websockets v14+ 兼容、aiortc ICE 候选解析、XBL 请求签名 P1363 转换
- 调度器可重入锁（修复认证链死锁）、设备码轮询崩溃、SQLite 跨线程访问

### 变更

- 旧 Python 实现移至 `reference/` 仅作对照，不再维护
- 依赖新增 `cryptography`（XBL 签名/登录链验证）

### 已知限制

- “好友的好友”加入不可用：MinecraftLobby 会话模板的可见性严格限定为
  广播号直接好友，为微软服务端限制（Java 原版同样无法实现）。
  扩大覆盖面请使用自动回关（2000 好友上限）+ 多账号子会话
- NetherNet ICE 端口范围限制（`icePortRange`）暂不支持（aiortc 限制），配置后将被忽略
- 客户端与服务器协议版本需为 Bedrock 1.26.45（协议 2169）
