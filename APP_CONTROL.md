# 蛋壳影院 APP 远程总控

适用于加入本功能的蛋壳定制电视客户端。现有门店安装的原版“影视”不会因为新增此文件而自动获得本功能；需要先安装定制版。目前客户端尚无编译通过的 APK，本文件合并也不代表门店已生效。

以后调整所有门店的订阅或首页默认片源，只需在 GitHub 编辑根目录的 `app_control.json`，提交到 main，等待 GitHub Pages 发布：

```json
{
  "subscription_url": "https://1072103612.github.io/eggtv/tvbox_config.json",
  "home_source_key": "drpy_js_豆瓣"
}
```

- `subscription_url`：实际 TVBox JSON 的完整 HTTPS 地址。当前继续使用原地址。
- `home_source_key`：首页打开时默认选中的片源 **key**，不是显示名称。填空字符串 `""` 表示首次采用订阅列表第一项。当前“豆瓣”是入口，客户端显示“欢迎来到蛋壳影院🎞️”。这不会修改订阅列表原有排序。

不需要填写版本号。启动先读取本地已验证配置，后台检查总控和订阅；停留首页时约每五分钟检查一次，回到首页也会按间隔检查。只有首页在前台、没有播放任务时，才加载待生效配置；视频播放、暂停或缓冲时保留当前配置。

变更订阅或默认 key 后，新设置应用一次。此后店员手动换源会被保留，普通片库同步不会重复重置。默认 key 暂时不存在时保留可用的本地选择，否则退回第一项；该 key 后续恢复时再应用一次。

新订阅须通过 JSON、片源 key 和顶层 spider JAR 完整性校验，再暂存。客户端原生解析失败或切换未确认便退出，下次恢复上一份配置；同一失败版本不会反复切换。总控不可用时继续更新上次接受的订阅。服务器提供 Last-Modified 时拒绝更旧总控，减少缓存倒退；无该响应头时按实际返回内容处理。远程变更并非全设备实时推送。

## 查 key 和提交前检查

```sh
python3 tools/eggtv_control.py sites
python3 tools/eggtv_control.py check
python3 -m unittest discover -s tests -p test_control.py -v
```

换成新订阅时，先保存该订阅 JSON，再校验其 key：

```sh
python3 tools/eggtv_control.py sites --catalog /path/to/new-config.json
python3 tools/eggtv_control.py check --catalog /path/to/new-config.json
```

校验工具只读取本地文件，不会测试影片播放、下载 JAR 或测量门店网速。客户端当前支持本项目这种标准 TVBox JSON：非空 sites 列表、唯一 key、HTTPS 顶层 spider 及 `;md5;` 校验；不承诺兼容任意加密、多仓或没有校验值的订阅。

总控固定入口：`https://1072103612.github.io/eggtv/app_control.json`。本轮沿用 GitHub Pages，尚未解决国内首次联网的线路可用性，也未加入控制入口的域名迁移。文件公开，请勿放账号密码或密钥。

原来的 `tvbox_config.json`、清洗规则和自动同步流程继续工作。总控只告诉定制客户端“到哪里取、默认选哪个”，不会复制出另一套片库。
