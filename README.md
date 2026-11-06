# bt-nps

宝塔面板第三方插件「NPS 管理器」，用于安装和管理 [ehang-io/nps](https://github.com/ehang-io/nps) `v0.26.10` 的服务端 `nps` 与客户端 `npc`。

> 非宝塔官方插件。仅适配 `ehang-io/nps`，不兼容其他 NPS 分支的配置与命令参数。

## 功能

- 服务端 / 客户端独立安装，支持**本地安装包**与**在线安装**两种来源
- 两种来源都使用 `packages.json` 中固定的 SHA256 校验，校验通过后才解压替换
- 启停、重启、状态查看，启动成功后自动设置开机启动
- 表单化配置（监听地址、端口、Web 账号密码、vkey、日志级别）
- 配置文件在线编辑（Ace 编辑器，保存前做基础格式校验并保留 `.conf.bak`）
- 日志查看（读取末尾 128 KB）
- 备份并重装：保留配置、客户端和隧道数据，重装前自动打包备份
- 一键放行防火墙端口（需手动确认，不会自动放行）

## 环境要求

- Linux 宝塔面板，使用 systemd 启动
- 架构：`amd64` 或 `arm64`
- 服务端与客户端均以 root 运行；在线安装和 NTP 校时需要访问外网

## 仓库结构

```text
nps/                  插件本体（后端、页面、安装脚本、信息、图标、本地安装包）
tests/                后端与前端测试
build_nps.py          校验本地包并生成 packages.json / 图标 / nps.zip
README.md
```

## 安装插件

1. 运行 `python build_nps.py` 生成 `nps.zip`
2. 宝塔面板 → 软件商店 → 第三方应用 → 导入插件，上传 `nps.zip`
3. 打开「NPS管理器」，选择 `nps 服务端` 或 `npc 客户端`，点击「本地安装」或「在线安装」

插件不提供命令行安装方式；如需手动部署，请直接使用上游发布包并在宝塔外自行管理服务。

## 安装来源

| 来源 | 说明 |
| --- | --- |
| 本地安装包 | 使用插件目录中的 `linux_<架构>_<角色>.tar.gz`，可离线安装 |
| 在线安装 | 从 `https://github.com/ehang-io/nps/releases/download/v0.26.10/` 下载，需服务器可访问 GitHub |

在线安装额外校验下载地址必须是 GitHub 官方 HTTPS 域名。若本地包与官方资产不是同一份字节，在线安装会因 SHA256 不一致而中止，这是有意的保护：将官方包放入 `nps/` 后重新打包即可同步哈希。

## 目录与文件

| 路径 | 用途 |
| --- | --- |
| `/usr/local/bt-nps` | 服务端运行目录（程序、`conf/nps.conf`、`logs/`） |
| `/usr/local/bt-npc` | 客户端运行目录（程序、`conf/npc.conf`、`logs/`） |
| `/etc/systemd/system/bt-nps.service` | 服务端 systemd 单元 |
| `/etc/systemd/system/bt-npc.service` | 客户端 systemd 单元 |
| `/www/backup/nps` | 重装前的数据备份 |
| `/www/server/panel/plugin/nps` | 插件本体与本地安装包 |

## 服务端默认配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bridge_type` | `tcp` | 仅支持 `tcp` / `kcp`，客户端需一致 |
| `bridge_port` | `8024` | 客户端连接端口，必须 1–65535 |
| `http_proxy_port` / `https_proxy_port` | `0` | 默认关闭，避免占用宝塔网站的 80/443 |
| `p2p_port` | 空 | **留空才关闭**，启动时占用连续 3 个 UDP 端口，不能填 0 |
| `web_ip` / `web_port` | `127.0.0.1` / `18081` | 默认仅本机访问，Web 端口不能与连接端口相同 |
| `web_username` / `web_password` | `admin` / 随机生成 | 密码可在插件「服务配置」中查看 |
| `log_level` | `6` | 0 紧急 … 7 调试 |
| `log_path` | `/usr/local/bt-nps/logs/nps.log` | 插件依赖该路径读取日志，不建议修改 |

Web 管理后台默认只监听本机。推荐在宝塔网站中添加 HTTPS 反向代理到 `http://127.0.0.1:18081`，再用域名访问；如确需公网直连，请同时修改监听地址、放行防火墙并限制来源 IP。

## 客户端配置

`npc.conf` 的 `[common]` 中至少填写：

```ini
[common]
server_addr=nps.example.com:8024
vkey=从 NPS 管理后台获取
conn_type=tcp
auto_reconnection=true
crypt=true
compress=true
```

- `vkey` 是 NPS 后台某个客户端的 vkey，不是 Web 登录密码
- 未填写地址和 vkey 时不允许启动，也不会设置开机启动
- 隧道（TCP/UDP、域名转发、socks5 等）在 NPS 自带 Web 后台管理，也可直接写进配置文件

## 打包

```bash
python build_nps.py
```

脚本会校验本地安装包（ELF 架构、`ehang.io/nps` 与版本标识、服务端配置模板格式），生成 `packages.json`、图标和 `nps.zip`。

可选文件 `nps/online_hashes.json` 用于只登记在线包哈希、不打入 ZIP，适合精简包或补充其他架构：

```json
{
  "linux_arm64_server.tar.gz": {"size": 1234567, "sha256": "…64 位十六进制…"}
}
```

## 测试

```bash
python -m unittest discover -s tests -p "test_nps.py"
node tests/test_nps_ui.js
bash -n nps/install.sh
```

后端测试使用模拟的 `public` 模块与 `systemctl`，不会安装程序或修改系统服务；在线下载通过模拟响应验证，不访问 GitHub。

## 安全与限制

- 安装、重装、修改配置前会校验运行目录归属（`.bt-plugin.json` 中的来源与版本），不会接管或覆盖非本插件管理的目录
- 安装目录内存在符号链接时拒绝重装
- 服务端启动前检查 `/etc/nps`：该目录存在会被 `ehang-io/nps` 优先读取，插件拒绝启动且不会删除或覆盖它
- 在线安装只固定 `v0.26.10`，不支持切换其他版本
- 插件不迁移其他分支的数据，也不管理 NPS 后台中的隧道数据
- 卸载插件会删除 systemd 单元，**保留** `/usr/local/bt-nps`、`/usr/local/bt-npc` 与备份目录

## 许可证

插件代码为社区适配。上游 `ehang-io/nps` 及其二进制遵循其仓库自身许可证；仓库内的安装包来自上游发布包，请遵守对应许可。
