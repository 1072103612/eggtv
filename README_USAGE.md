# 蛋壳影院片源同步工具 - 使用说明

这个工具的作用是：**自动帮你更新影视源**

你只需要维护上游链接，工具会自动抓取最新源并同步到 GitHub，简单、稳定、不会出错。

---

## 基本概念

| 名词 | 解释 |
|------|------|
| 上游 | 原始片源地址，工具从这里获取最新内容 |
| 留底文件 | 上游原始内容的备份，以 `_upstream.json` 结尾 |
| 发布文件 | 对外供 TVBox 使用的文件 (`tvbox_config.json`、`jsm_backup.json`) |
| spider | 爬虫程序，工具会下载到仓库并自动更新地址 |
| mirrors.json | 多 CDN 镜像配置，解决 GitHub Raw 访问慢的问题 |

---

## 同步逻辑

**极简主义：上游增就增，上游删就删，上游变就变**

- 不做测速（网络波动会导致误判）
- 不做过滤（规则复杂难以维护）
- 信任上游（简单透明可预测）

---

## 常用命令

### 查看当前配置
```bash
python3 tools/eggtv_sync.py list
```

### 健康检查
```bash
python3 tools/eggtv_sync.py health
```
检查上游片源、spider.jar、CDN 镜像是否正常。

### 同步全部片源
```bash
python3 tools/eggtv_sync.py sync --all
```

### 同步并推送
```bash
python3 tools/eggtv_sync.py sync --all --push
```

---

## 进阶用法

### 预览将要修改什么（不实际写入）
```bash
python3 tools/eggtv_sync.py sync --all --dry-run
```

### 查看详细变更
```bash
python3 tools/eggtv_sync.py sync --all --diff
```

### 临时使用其他上游链接
```bash
python3 tools/eggtv_sync.py sync tvbox --upstream-url 'https://example.com/new.json'
```

### 设置上游链接
```bash
python3 tools/eggtv_sync.py set-url tvbox 'https://example.com/tvbox.json'
```

---

## 工作流程

1. **平时**：用 `--dry-run` 预览，确认没问题后用 `--push` 同步
2. **自动化**：GitHub Actions 每 6 小时自动检查，有变化自动更新

```bash
# 推荐流程
git pull                      # 先拉取最新代码
python3 tools/eggtv_sync.py sync --all --dry-run  # 预览
python3 tools/eggtv_sync.py sync --all --push     # 确认后执行
```

---

## 故障处理

| 问题 | 解决方式 |
|------|---------|
| 上游挂了 | GitHub Actions 会自动重试，spider 有多源兜底 |
| CDN 访问慢 | 使用 mirrors.json 中的其他 CDN 地址 |
| spider 下载失败 | 自动切换备用源 |

---

## 代理说明

工具默认使用本地 Clash 代理（`127.0.0.1:7890`），如果上游访问不了会自动切换直连。

如果不想用代理：
```bash
python3 tools/eggtv_sync.py sync --all --no-proxy
```

GitHub Actions 运行在服务器上，不使用你本机的代理，它会直接访问上游。
