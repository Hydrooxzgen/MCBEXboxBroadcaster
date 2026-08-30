# Java → Python 移植笔记（协议细节备忘）

本文件记录移植过程中查证的协议细节，是新实现的依据。
权威来源：MinecraftAuth 5.x 源码、kastle netty-transport-nethernet 1.7.3 源码（sources.jar）、
CloudburstMC Protocol Bedrock_v2169、df-mc/nethernet-spec、go-nethernet。

## 1. 认证链（对应 MinecraftAuth BedrockAuthManager，标题客户端路径）

常量：
- MSA client id: `0000000048183522`（Bedrock Android title id），scope: `service::user.auth.xboxlive.com::MBI_SSL`
- MSA 使用 login.live.com 的旧版 LIVE 环境（MinecraftAuth 默认）：
  - 设备码: `https://login.live.com/oauth20_connect.srf`（表单: client_id, scope, response_type=device_code）
  - 轮询/刷新: `https://login.live.com/oauth20_token.srf`（表单: client_id, grant_type=device_code/refresh_token, ...）
- 设备认证: `https://device.auth.xboxlive.com/device/authenticate`，Properties:
  DeviceType="Android", Id="{uuid}", AuthMethod="ProofOfPossession", ProofKey(JWK EC P-256)，
  RelyingParty="http://auth.xboxlive.com", TokenType="JWT" —— 需要 Signature 头
- SISU: `https://sisu.xboxlive.com/authorize`，body: Sandbox="RETAIL", UseModernGamertag=true,
  AppId=client_id, AccessToken="t=<msa access_token>", DeviceToken=<device token>,
  ProofKey(JWK), RelyingParty="https://multiplayer.minecraft.net/" —— 需要 Signature 头
  → 返回 UserToken / TitleToken / AuthorizationToken(bedrock XSTS)
- XSTS: `https://xsts.auth.xboxlive.com/xsts/authorize`，Properties: SandboxId="RETAIL",
  DeviceToken, UserTokens:[userToken], TitleToken；RelyingParty 分四种:
  - xboxlive: `http://xboxlive.com`（会话目录/社交/presence/RTA 用）
  - playfab: `https://b980a380.minecraft.playfabapi.com/`
  - bedrock: `https://multiplayer.minecraft.net/`（SISU 直接给出）
  - realms: `https://pocket.realms.minecraft.net/`
  → DisplayClaims.xui[0].uhs + Token；Authorization 头 = `XBL3.0 x=<uhs>;<token>`
- 个人资料: `https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag`
  （XBL3.0 头 + x-xbl-contract-version: 3）
- PlayFab: `https://20ca2.playfabapi.com/Client/LoginWithXbox`，body: CreateAccount=true,
  InfoRequestParameters={GetPlayerProfile,GetUserAccountInfo}, TitleId="20CA2",
  XboxToken=playfab XSTS 的 XBL3.0 头 → data.SessionTicket
- MC 会话: `https://authorization.franchise.minecraft-services.net/api/v1.0/session/start`
  body: device{applicationType:"MinecraftPE", gameVersion, id:无横线deviceId, memory:34359738368,
  hardwareMemoryTier:5, platform:"Windows10", playFabTitleId:"20CA2", storePlatform:"uwp.store",
  type:"Windows10"}, user{language:"en", regionCode:"US", languageCode:"en-US",
  tokenType:"PlayFab", token:SessionTicket} → result.authorizationHeader（"MCToken xxx"）
- MC 多人令牌: `https://authorization.franchise.minecraft-services.net/api/v1.0/multiplayer/session/start`
  body: publicKey=base64(SPKI(EC-P384))，Authorization=MC 会话头 → result.signedToken(JWT)；
  JWT payload 的 `pmid` 即 pmsgId

XBL Signature 请求头（device.auth / sisu 需要）：
- content = int32(1) + 0x00 + int64(winTimestamp) + 0x00 + method + 0x00 + path+query + 0x00
  + Authorization头(若有) + 0x00 + body + 0x00
- winTimestamp = (unix秒 + 11644473600) * 10^7
- sig = ECDSA-P256/SHA256（P1363 裸 r||s，64 字节）用设备密钥签名 content
- header 值 = base64( int32(1) + int64(winTimestamp) + sig )

## 2. NetherNet 信令（对应 kastle NetherNetXboxRpcSignaling）

- 端点: `wss://signal.franchise.minecraft-services.net/ws/v1.0/messaging/connect`
- 握手头: Authorization=<MC 会话 authorizationHeader>, User-Agent: `libHttpClient/1.0.0.0`,
  session-id=<uuid>, request-id=<uuid>
- JSON-RPC 方法:
  - `Signaling_TurnAuth_v1_0` {} → {TurnAuthServers:[{Urls,Username,Password}]}（ICE 服务器）
  - `Signaling_SendClientMessage_v1_0` {toPlayerId, messageId:uuid, message:<字符串>}
  - `Signaling_ReceiveMessage_v1_0` params: {From, Message, Id}（需回 DeliveryNotification）
  - `System_Ping_v1_0` / `System_Pong_v1_0`（30s 间隔 ping，50s 超时周期）
  - 内层方法: `Signaling_WebRtc_v1_0` params:{netherNetId, message:"<TYPE> <connId> <data>"},
    `Signaling_DeliveryNotification_V1_0` params:{messageId}
- 信号字符串格式: `CONNECTREQUEST|CONNECTRESPONSE|CANDIDATEADD|CONNECTERROR <connId> <data>`
- 发送 = SendClientMessage(目标networkId, 内层JSON字符串)；收到 ReceiveMessage 时先回
  SendClientMessage(From, DeliveryNotification 内层)，再解析内层 WebRTC 消息

## 3. WebRTC 协商（对应 kastle NetherNetServerChannel）

- 收到 CONNECTREQUEST（offer SDP, data 部分为 SDP 文本）:
  - 建 RTCPeerConnection：iceServers=TurnAuth 结果，bundlePolicy=max-bundle，
    端口分配: 禁 TCP、启用 IPv6/AnyAddress/共享 socket
  - setRemoteDescription(offer) → createAnswer → setLocalDescription
  - 答案 SDP 需要 **a=identity 增强**（ServerIdentity）：
    - P-384 EC 密钥，JWT(ES384) payload 含 `cpk`（= base64(标准 DER SPKI 公钥)）、iat、iss("self")
    - fingerprint 断言: 对 canonical fingerprint JSON 签名（JWS compact 的 header..signature 部分，
      即 `<b64url(header)>..<b64url(sig)>`）；canonical JSON =
      `{"fingerprint":[{"algorithm":"<algo>","digest":"<digest>"},...]}`
      （从答案 SDP 的 `a=fingerprint:` 行提取，按 `{"algorithm":"x","digest":"y"}` 拼接）
    - identity JSON = {"idp":{"domain":"self","protocol":"default"},
      "assertion":{"token":<jwt>,"fingerprints":<header..sig>}} 的 base64（标准 b64）
    - 在 SDP 首个 `m=` 行前插入 `a=identity:<b64>` 行
  - 发送 CONNECTRESPONSE 给对方 networkId
- 本地 ICE candidate → CANDIDATEADD（candidate.sdp 原文）；收到 CANDIDATEADD → addIceCandidate
- 数据通道由**客户端**创建：label 分别为 `ReliableDataChannel`（ordered）/`UnreliableDataChannel`
  （unordered, maxRetransmits=0）。只用 reliable。
- 消息分片: 每条 SCTP 消息首字节=剩余段数；>10000 字节分片（每片最多 9999 字节 payload）。
  最后一片(剩余=0)拼接后为: **varuint32 长度 + 包数据**（可多个包连发）——
  这一层之上才是 Bedrock 的压缩层。

## 4. Bedrock 层（协议 2169 / 1.26.45，压缩自 protocol≥649 加头）

- 帧格式: varuint32 包长 + 包字节（kastle NetherNetPacketDecoder）
- 包字节首部（压缩后）: 0x00=zlib(raw), 0x01=snappy, 0xff=不压缩（阈值 1，>1 字节才压缩）
- 流程: RequestNetworkSettings(protocol) → NetworkSettings(threshold=0, ZLIB) → Login
  (payload=登录链JWT, clientJwt) → 校验链（Mojang 根公钥 ES256）→ PlayStatus(LOGIN_SUCCESS)
  → ResourcePacksInfo → ClientCacheStatus → ResourcePackClientResponse(HAVE_ALL_PACKS)
  → ResourcePackStack → COMPLETED → StartGame + Transfer(ip, port) → 客户端连真实服务器
- 会话中的 SupportedConnections: ConnectionType=7(jsonrpc), NetherNetId, PmsgId

## 5. 会话目录 REST

- CREATE_SESSION: `https://sessiondirectory.xboxlive.com/serviceconfigs/4fc10100-.../sessionTemplates/MinecraftLobby/sessions/<sid>`
- CREATE_HANDLE: `https://sessiondirectory.xboxlive.com/handles`（activity/invite）
- JOIN_SESSION: `https://sessiondirectory.xboxlive.com/handles/<handleId>/session`
- 头: XBL3.0 + x-xbl-contract-version: 107
- presence: `https://userpresence.xboxlive.com/users/xuid(<xuid>)/devices/current/titles/current`
  body {"state":"active"}, contract-version 3, 按 X-Heartbeat-After 重发
- 社交: peoplehub.xboxlive.com（followers/social, contract 5）、social.xboxlive.com（people/
  summary/friendrequests, contract 3/7 等，见 Java FriendManager）

## 6. 其它

- **"好友的好友"(FOF)不可用（实测结论）**：MinecraftLobby 模板带 'userAuthorizationStyle'
  能力，禁止 joinRestriction/readRestriction 设为 none；保持 followed 时会话目录只对
  所有者的直接关注者可见。修改自定义 Joinability 字段、或整个省略 system 属性均无法让
  FOF 玩家看到/加入会话。扩大覆盖面用自动回关(2000 上限)+子会话账号。

- **Transfer 时机（实测关键）**：StartGame 和 Transfer 不能同时发——客户端需要约 1.5 秒
  处理 StartGame 进入世界加载状态后才接受 Transfer。立即发送会被客户端忽略并断开。
  Python 实现采用 StartGame → 延迟 1.5s → Transfer → 延迟 2s 关闭连接。
- StartGame 编码已与 CloudburstMC 3.0.Beta13（2026-08-28 快照）输出逐字节对齐，
  注意：dayCycleStopTime 默认 0、gamerule 带 editable 标志（v844+）、
  experiments 数量是 intLE、limW/limH 默认 0、无 tickDeathSystemsEnabled（新协议已删）。

- RakNet ping: 0x01 + magic(16B) + guid(LE 8B)；pong: 0x1c + time(LE) + **serverGUID(LE)**
  + magic + ushort BE 长度 + UTF-8 字符串
- web ping fallback: `https://checker.geysermc.org/ping?hostname=&port=` → data.ping.pong
- config v1→v2 迁移规则见 Java ConfigLoader（remote-address 等移入 session 节等）
- 旧版（DeepSeek）实现在 reference/，其信令端点（rpc-prod.xboxlive.com）是错误的
