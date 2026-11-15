# 蛋壳影院 APP 专用订阅

APP 单独使用 `app/tvbox_config.json`，原 `tvbox_config.json`、副配置、现有门店订阅均不修改。

## 生成与同步

`python3 tools/publish_app_feed.py` 读取已经过 eggtv 清洗及健康规则处理的主配置，只改默认入口名为“欢迎来到蛋壳影院🎞️”，不增加或恢复片源。发布配置使用内容哈希命名的独立 JAR，依赖先写入，再原子替换配置。JAR 不存在、校验不符、没有 DEX 或片源列表异常时，保留上一次可用输出。

新增的 `Publish Eggshell App Feed` 工作流在原 `Sync Sources` 成功后运行，也可以手动触发，只提交 `app/` 文件。它没有更换现有上游或旧订阅线路。

合并并生成文件后，APP 专用候选线路为：

- `https://1072103612.github.io/eggtv/app/tvbox_config.json`
- `https://gh-proxy.com/https://raw.githubusercontent.com/1072103612/eggtv/main/app/tvbox_config.json`
- `https://cdn.jsdelivr.net/gh/1072103612/eggtv@main/app/tvbox_config.json`

免费线路的门店可用性需实测。客户端应先使用 APK 内置快照及已验证缓存，再后台拉取新配置；不把启动阻塞在某个免费镜像上。API、影片网站和视频服务的访问速度不由这个订阅托管决定。

## APK 内置快照

```sh
python3 tools/publish_app_feed.py --apk-assets /path/to/TV/app/src/main/assets/egg
```

这同时生成 `config.json` 和 `spider.jar`，供构建蛋壳专属客户端。客户端每次启动后台更新订阅，更新成功后下次完整启动加载新配置，不在播放过程中强制重载。独立脚本引擎、真实搜索、首页和具体影片播放还需在客户端实机验收。

## 检查

```sh
python3 -m unittest discover -s tests -v
```

本轮 51 项检查通过，包含原同步保护和新发布器的输出隔离、快照一致性、损坏 JAR 保留上一版、非法片源及无 DEX 拦截。
