# 蛋壳影院片源同步工具 - 使用说明

这个工具的作用是：**自动帮你更新影视源**

你只需要管理上游链接，工具会自动抓取最新源、过滤无效内容、测速剔除坏源，然后保存到你的 GitHub 仓库。

---

## 基本概念

| 名词 | 解释 |
|------|------|
| 上游 | 原始片源地址，工具从这里获取最新内容 |
| 留底文件 | 上游原始内容的备份，以 `_upstream.json` 结尾 |
| 发布文件 | 对外供 TVBox 使用的文件 (`tvbox_config.json`、`jsm_backup.json`) |
| spider | 爬虫程序，工具会下载到仓库并自动更新地址 |

---

## 准备工作

### 1. 安装 Python

macOS 通常已经自带 Python 3，在终端输入：
```
python3 --version
```
看到类似 `Python 3.x.x` 就说明有了。

### 2. 克隆仓库到本地

如果还没有同步过，先把仓库下载到本地：
```bash
git clone https://github.com/1072103612/eggtv.git
cd eggtv
```

以后每次同步前，先拉取最新代码：
```bash
git pull
```

---

## 常用命令

### 查看当前配置
```bash
python3 tools/eggtv_sync.py list
```
会显示你配置了哪些上游链接。

### 查看清洗规则
```bash
python3 tools/eggtv_sync.py show-rules
```
会显示当前过滤了哪些关键词（哔哩、少儿、广告等）。

### 同步全部片源（推荐）
```bash
python3 tools/eggtv_sync.py sync --all
```
这会同步主配置和副配置，并自动测速剔除无效源。

### 同步单个配置
```bash
python3 tools/eggtv_sync.py sync tvbox      # 只同步主配置
python3 tools/eggtv_sync.py sync jsm        # 只同步副配置
```

### 同步并推送（同步后自动提交到 GitHub）
```bash
python3 tools/eggtv_sync.py sync --all --push
```

---

## 进阶用法

### 预览将要修改什么（不实际写入）
```bash
python3 tools/eggtv_sync.py sync tvbox --dry-run
```
非常有用！先看看会变什么，确认没问题再真正执行。

### 查看详细变更内容
```bash
python3 tools/eggtv_sync.py sync tvbox --diff
```
会显示具体修改了哪些站点的增减。

### 临时使用其他上游链接（不改配置）
```bash
python3 tools/eggtv_sync.py sync tvbox --upstream-url 'https://example.com/new.json'
```

### 设置上游链接
```bash
python3 tools/eggtv_sync.py set-url tvbox 'https://example.com/tvbox.json'
```

---

## 常见问题

**Q: 同步很慢怎么办？**
A: 工具会并发探测多个站点（最多 8 个同时），如果网络不好可以等几分钟。spider.jar 下载较慢（几 MB），这是正常的。

**Q: 某个站点被误删了怎么办？**
A: 编辑 `CLEAN_RULES.md` 中的 `drop_keywords` 可以调整过滤规则。或者查看留底文件恢复。

**Q: 提示 "WARNING: invalid payload" 是什么意思？**
A: 表示上游的 spider.jar 下载回来的不是有效的 jar 文件，工具保留了仓库里现有的版本。如果长期出现这种情况，说明上游可能有问题。

**Q: 想要完全自动化更新**
A: 仓库已配置 GitHub Actions，每 6 小时自动检查一次上游，有变化会自动更新并推送。

---

## 工作流程建议

1. **平时**：用 `--dry-run` 预览，觉得没问题了用 `--push` 正式同步
2. **检查**：`--diff` 查看详细变更，确认没有误删重要源
3. **自动化**：GitHub Actions 已经在后台自动运行，你也可以手动触发

```bash
# 推荐流程
git pull                          # 先拉取最新代码
python3 tools/eggtv_sync.py sync --all --dry-run  # 预览
python3 tools/eggtv_sync.py sync --all --push    # 确认没问题后执行
```

---

## 代理说明

工具默认使用本地 Clash 代理（`127.0.0.1:7890`），如果上游访问不了会自动切换直连。

如果不想用代理：
```bash
python3 tools/eggtv_sync.py sync --all --no-proxy
```

GitHub Actions 运行在服务器上，不使用你本机的代理，它会直接访问上游。
