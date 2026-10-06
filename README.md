# 蛋壳影院

简单好用的 TVBox 片源同步工具。

## 这是什么

蛋壳影院帮你把上游片源同步到 GitHub，提供稳定、快速、多 CDN 镜像的影视配置。

- 一主一副两套配置，互为备份
- 多 CDN 镜像，访问更稳定
- 自动清洗，无需手动管理

## 文件说明

| 文件 | 说明 |
|------|------|
| `tvbox_config.json` | 主配置 |
| `jsm_backup.json` | 副配置 |
| `mirrors.json` | CDN 镜像列表 |
| `jar/tvbox_spider.jar` | 主配置的播放工具 |
| `jar/jsm_spider.jar` | 副配置的播放工具 |
| `sync_report.md` / `sync_report.json` | 同步报告（含候补来源及更新失败原因） |
| `tvbox_config_local.json` / `jar/spider.jar` | 历史本地配置和工具，不属于自动维护的两套发布配置 |

## 快速使用

### 同步片源

```bash
python3 tools/eggtv_sync.py sync --all --push
```

这会：
1. 抓取上游片源
2. 按规则清洗（过滤不需要的站点）
3. 修正配套脚本、规则文件的相对地址，并检查这些文件能否读取
4. 为主、副配置分别下载和校验播放工具，确认包含所需站点功能
5. 所有检查通过后更新配置，生成 CDN 地址和报告，再提交推送

主配置的首选来源不可用或配套工具不完整时，会尝试整套候补来源，不会借用另一套配置的工具拼接。某套配置的所有来源都失败时保留它原来的文件，另一套配置可以继续更新。只有一套来源实际可用时，主、副菜单可能暂时来自同一个来源；工具仍分别保存，避免后续更新互相覆盖。

### 查看健康状态

```bash
python3 tools/eggtv_sync.py health
```

这会检查上游配置及其播放工具、各镜像是否更新、已发布工具的校验值、站点需要的脚本和规则文件。检查通过只说明这些文件有效、可读取；是否能搜索到影片、是否能正常播放，仍需在影院电视上试播。

查看不落盘的完整同步预览：

```bash
python3 tools/eggtv_sync.py --no-proxy sync --all --dry-run
```

### 手动触发同步

GitHub Actions 每 6 小时自动同步一次，也可手动触发：

1. 进入 https://github.com/1072103612/eggtv/actions/workflows/sync-sources.yml
2. 点击 "Run workflow"

## 片源规则

### 保留的
- 电影、电视剧、综艺、纪录片
- 磁力搜索站
- 官方影视源（爱奇艺、腾讯、优酷等）

### 移除的
- 动漫、二次元
- 名称含软件、应用、扫码等关键词的入口（普通影视 APP 入口保留）
- 4K、8K 站点
- 儿童教育类
- 哔哩相关（戏曲、小品等）
- 音乐、小说、直播类
- 搜索、网盘类

规则在 `eggtv_sync.json` 中配置，可随时修改。过滤依据是站点名称，不判断影片实际内容；重复站点标识只保留第一项。儿童、网盘、音乐、体育等关键词对两套配置都生效，直播列表也不发布。

## 客户端配置

### 主配置地址
```
https://1072103612.github.io/eggtv/tvbox_config.json
```

### 备用地址（任选其一）
```
https://cdn.jsdelivr.net/gh/1072103612/eggtv@main/tvbox_config.json
https://raw.githubusercontent.com/1072103612/eggtv/main/tvbox_config.json
```

> ⚠️ `raw.githubusercontent.com` 在国内间歇性被墙，优先使用主配置地址（GitHub Pages）或 jsDelivr 备用地址。

### 副配置地址
```
https://1072103612.github.io/eggtv/jsm_backup.json
```

## 代理设置

脚本默认使用本地代理 `http://127.0.0.1:7890`。

如需临时关闭：
```bash
python3 tools/eggtv_sync.py --no-proxy sync --all
```

如需临时更换代理：
```bash
python3 tools/eggtv_sync.py --proxy 'http://127.0.0.1:7891' sync --all
```

## 常见问题

### 源挂了打不开？
GitHub Actions 每 6 小时自动重试。如果上游长时间不可用，可能需要更换上游。

### spider.jar 是什么？
播放工具，帮助客户端读取不同站点和搜索影片。主、副配置分别使用 `tvbox_spider.jar` 和 `jsm_spider.jar`，会自动下载更新；文件损坏或与站点不匹配时不会直接发布。

### CDN 镜像是什么？
同一份配置的多条访问路径，保存在 `mirrors.json`。客户端是否自动切换取决于客户端功能；不支持时可手动改用备用配置地址。镜像有缓存，可能比 GitHub Pages 晚更新。

## 技术说明

- Python 3，无第三方 Python 依赖；系统需有 `curl`，提交推送还需 `git`
- 配置文件：`eggtv_sync.json`
- 同步脚本：`tools/eggtv_sync.py`
- 验证更新保护：`python3 -m unittest discover -s tests -v`
