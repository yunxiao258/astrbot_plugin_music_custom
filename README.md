# 自研点歌插件（astrbot_plugin_music_custom）

AstrBot 群聊点歌插件：多音乐源聚合搜索，语音/卡片发送，选歌交互，随机/热门/统计/收藏/播放队列/链接解析。

## 功能

### 基础点歌
- `点歌 歌名`：搜索（别名：点歌姬/来首歌/放首歌/来首/放首/点歌，可用 `aliases` 追加），返回编号列表，30 秒内回复序号播放
- `点歌 高清 歌名`：以高音质搜索播放（失败自动降级）
- `点歌 下一页` / `点歌 上一页`：翻页浏览更多结果
- `点歌 歌词 片段`：按歌词片段搜歌（网易云）
- 回复「取消」退出当前选歌
- `点歌 随机`：随机关键词搜索后直接播放
- `点歌 热门`：取各源热门榜进入选歌
- 粘贴链接：网易云 / QQ音乐 / B站 分享链接直接解析播放
- 语音发送：优先直发音频 URL，失败自动下载转码 AMR 兜底（带本地缓存复用）；不可用时回退 QQ 音乐卡片 + 链接
- 播放提示点歌人与歌词（网易云源）

### 收藏 / 歌单
- `点歌 收藏 歌名`：搜索后回复序号收藏（每人上限 50 首）
- `/song fav`：查看我的收藏，回复序号播放
- `/song fav del 序号`：删除收藏

### 播放队列
- `点歌 排队 歌名`：搜索后回复序号加入队列
- `/song queue`：查看队列（显示添加人）
- `/song queue clear`：清空队列
- `/song queue next`：跳过等待立即播放下一首
- 队列按群隔离，自动逐首播放（间隔 `queue_interval` 秒，上限 `queue_limit` 首）

### 统计
- `点歌 统计`：总榜 Top 10
- `点歌 统计 周` / `点歌 统计 月`：近 7 天 / 近 30 天榜单
- `点歌 统计 我`：个人最爱 Top 5
- `点歌 统计 人`：点歌达人 Top 10
- `点歌 统计 群`：本群最受欢迎 Top 10

### 管理（管理员）
- `/song block 词`：屏蔽歌曲（匹配标题/歌手，搜索与播放双拦截）
- `/song unblock 词`：解除屏蔽；`/song blocks` 查看屏蔽列表
- `/song set 键 值`：全局动态调整配置；`/song gset 键 值`：仅当前群生效
- `/song greset [键]`：还原本群配置

### 其他
- 每日配额：`daily_limit` 限制每人每日点歌次数（0 不限），剩余不足 3 次时提示
- 定时热门推送：`hot_push_enable` + `hot_push_time` + `hot_push_groups` + `hot_push_platform`，每天定时向指定群推送热门歌单
- 频率限制：默认每人 30 秒点一次（`frequency_seconds`，群可单独覆盖）

## 音乐源

| 源 | 说明 |
| --- | --- |
| netease | 网易云音乐（搜索 + 直链 + 热门 + 链接解析 + 歌词搜索/歌词） |
| kuwo | 酷我音乐（搜索 + 直链 + 热门） |
| kugou | 酷狗音乐（搜索；播放接口有风控，通常回退） |
| qqmusic | QQ音乐（搜索 + 链接解析；语音直链易风控，主要用于卡片） |
| bilibili | bilibili（搜索 + 音频直链 + 热门 + 链接解析） |

## 配置项

见 `_conf_schema.json`。关键项：

- `sources`：启用源，逗号分隔
- `voice_mode`：`url`（直发）/ `amr`（转码发送）/ `off`（仅卡片）
- `quality`：默认音质 `standard` / `high` / `low`
- `search_limit`：每页/每源结果条数
- `daily_limit`：每人每日点歌上限（0 不限）
- `aliases`：自定义点歌指令别名（逗号分隔）
- `enable_artwork` / `enable_lyric`：封面图 / 歌词开关
- `hot_push_enable` / `hot_push_time` / `hot_push_groups` / `hot_push_platform`：定时热门推送
- `cache_max_mb`：语音缓存目录上限（自动清理最旧文件）
- `enable_card`：是否发送 QQ 音乐卡片
- `frequency_seconds`：每人点歌冷却秒数
- `queue_limit` / `queue_interval`：队列上限与播放间隔

## 依赖

- 需要 `ffmpeg`（AMR 转码用，自动探测系统 PATH 与常见路径）
- 其余仅用 requests / asyncio / re / random，无第三方库

## 数据

存储于 `plugin_data/astrbot_plugin_music_custom/`：

- `stats.json`：点歌统计
- `favorites.json`：用户收藏
- `per_group.json`：群独立配置
- `daily_quota.json`：每日配额
- `blocked.json`：屏蔽词
- `push_state.json`：定时推送状态
