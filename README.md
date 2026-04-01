# eggtv Sync Tool

这个仓库现在带了一套可运行的源同步工具，用来把上游 TVBox JSON 源同步到当前 GitHub 仓库，再由 TVBox 等客户端直接连接这里的 Raw 地址。

## 目标

- 你只维护上游链接
- 脚本自动拉取上游 JSON
- 自动保存上游留底文件
- 按你当前仓库里的发布文件规则生成对外发布版本
- 自动下载 `spider.jar`，计算 MD5，并改写成 GitHub Raw 地址
- 可选自动 `git add / commit / push`

## 文件

- `tools/eggtv_sync.py`: 同步 CLI
- `eggtv_sync.json`: 配置文件
- `CLEAN_RULES.md`: 当前清洗规则说明
- `tvbox_upstream.json`: 主配置上游留底
- `tvbox_config.json`: 主配置发布文件
- `jsm_upstream.json`: 备用配置上游留底
- `jsm_backup.json`: 备用配置发布文件

## 依赖

只需要 Python 3，不依赖第三方包。

## 代理

仓库默认已经配置本地 Clash 代理：

- `http://127.0.0.1:7890`

本地运行脚本时会优先走这个代理，失败后再自动直连。

如果你临时不想走代理：

```bash
python3 tools/eggtv_sync.py --no-proxy sync --all
```

如果你以后更换了代理端口，也可以临时指定：

```bash
python3 tools/eggtv_sync.py --proxy 'http://127.0.0.1:7891' sync --all
```

注意：

- 这个 `7890` 代理只对你自己的电脑有效
- GitHub Actions 运行在 GitHub 服务器上，不能使用你本机的 `127.0.0.1:7890`
- 所以工作流里已经固定使用 `--no-proxy`

## 常用命令

列出当前已配置的 profile：

```bash
python3 tools/eggtv_sync.py list
python3 tools/eggtv_sync.py show-rules
```

给某个 profile 设置上游链接：

```bash
python3 tools/eggtv_sync.py set-url tvbox 'https://example.com/tvbox.json'
python3 tools/eggtv_sync.py set-url jsm 'https://example.com/jsm.json'
```

同步单个 profile：

```bash
python3 tools/eggtv_sync.py sync tvbox
```

同步全部 profile：

```bash
python3 tools/eggtv_sync.py sync --all
```

同步后直接提交并推送：

```bash
python3 tools/eggtv_sync.py sync tvbox --push
python3 tools/eggtv_sync.py sync --all --push --commit-message 'chore(sync): refresh sources'
```

临时指定一个上游链接，不改配置文件：

```bash
python3 tools/eggtv_sync.py sync tvbox --upstream-url 'https://example.com/tvbox.json'
```

## 工作方式

脚本不是简单把上游文件原样覆盖到发布文件，而是把两层规则叠加起来：

1. 显式规则
2. 当前发布结果继承

具体执行顺序：

1. 读取上一次的 `*_upstream.json`
2. 读取当前对外发布文件，比如 `tvbox_config.json`
3. 先按主配置和备用配置共用的一套显式关键词规则过滤 `sites`
4. 再反推出你当前保留了哪些站点、顺序是什么、哪些字段被你改过
5. 用这些规则套到新抓到的上游 JSON 上
6. 最终结果再跑一次显式规则过滤，避免旧条目被继承回来
7. 对最终保留的站点做一轮轻量测速，自动剔除失效或明显过慢的源

这正好对应你现在仓库里已经在做的事情，只是把手工流程变成了命令。

## 当前仓库的一个约束

现在 `tvbox` 和 `jsm` 两个 profile 共用同一个 `jar/spider.jar`。

所以脚本在同步其中一个 profile 时，如果这个 jar 发生变化，也会顺手把另一个发布文件里的 `spider` MD5 一起对齐，避免出现同一个 jar 对应两个不同 MD5 的情况。

另外，脚本现在会校验下载回来的 `spider` 是否真的是 jar/zip 文件。如果上游返回的是网页或错误页，脚本会保留仓库里的现有 jar，不会把它覆盖坏。

## GitHub Raw 地址

客户端可以继续直接连你仓库的 Raw 文件，例如：

- `https://raw.githubusercontent.com/1072103612/eggtv/main/tvbox_config.json`
- `https://raw.githubusercontent.com/1072103612/eggtv/main/jsm_backup.json`

## 建议工作流

1. 用 `set-url` 把每个上游链接配置进去
2. 运行 `sync`
3. 检查生成结果
4. 没问题就加 `--push`

如果你后面要做完全自动化，可以再加 GitHub Actions 定时跑这个脚本。
