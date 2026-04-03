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
| `jar/spider.jar` | 爬虫程序 |
| `sync_report.json` | 同步报告 |

## 快速使用

### 同步片源

```bash
python3 tools/eggtv_sync.py sync --all --push
```

这会：
1. 抓取上游片源
2. 按规则清洗（过滤不需要的站点）
3. 更新 spider.jar
4. 生成 CDN 镜像
5. 自动提交推送

### 查看健康状态

```bash
python3 tools/eggtv_sync.py health
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
- APP 类站点（扫码付费类）
- 4K、8K 站点
- 儿童教育类
- 哔哩相关（戏曲、小品等）
- 音乐、小说、直播类
- 搜索、网盘类

规则在 `eggtv_sync.json` 中配置，可随时修改。

## 客户端配置

### 主配置地址
```
https://raw.githubusercontent.com/1072103612/eggtv/main/tvbox_config.json
```

### 备用地址（任选其一）
```
https://cdn.jsdelivr.net/gh/1072103612/eggtv@main/tvbox_config.json
https://mirror.ghproxy.com/https://raw.githubusercontent.com/1072103612/eggtv/main/tvbox_config.json
```

### 副配置地址
```
https://raw.githubusercontent.com/1072103612/eggtv/main/jsm_backup.json
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
爬虫程序，用于搜索磁力链接等。会自动下载更新。

### CDN 镜像是什么？
多条访问路径。客户端会自动尝试，哪条通就走哪条。

## 技术说明

- 纯 Python 3，无第三方依赖
- 配置文件：`eggtv_sync.json`
- 同步脚本：`tools/eggtv_sync.py`
