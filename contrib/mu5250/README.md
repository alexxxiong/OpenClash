# MU5250 原厂系统独立 OpenClash 适配

维护分支：`codex/mu5250-integration`。上游跟踪 `pickrui/OpenClash` 的 **dev**，不是默认 master。当前适配以 `8ceb9d1fb874edb51ab6296798f1aed771d78ee9`、OpenClash 0.48.012 为基础，实机为国行 MU5250 B27。

本目录保存已在原厂 OpenWrt 系统上运行的独立 LuCI/OpenClash 适配源码及离线测试。上游 LuCI 页面、订阅处理和 YAML 逻辑继续来自仓库主体；设备上的账号、密码、订阅、主机密钥、备份、日志和运行配置均不放入 Git。

这是已有独立安装的维护源码，不是通用固件或可直接对原厂环境运行的 opkg 安装包。首次部署所需 LuCI、Ruby、rpcd、uhttpd、内核支持和恢复条件，应按实际固件另外核对。

## 运行结构

| 文件 | 用途 / 已安装位置 |
|---|---|
| `namespace.rs` | 静态 ARM64 `oc-namespace` 的源码，创建或进入私有挂载命名空间 |
| `start-openclash.sh`, `private-init.sh` | `/data/openclash/bin/`；启动独立 ubus、rpcd、LuCI 与已启用的核心 |
| `wait-private-ubus.sh`, `ubus-wrapper.sh`, `firmware-readonly.uc` | LuCI 就绪检测与只读原厂状态桥；包装器及 ucode 放入独立 overlay 的对应目录 |
| `openclash-service.sh`, `managed-init.sh` | 应用服务生命周期；`managed-init.sh` 只用于私有 `/etc/init.d/openclash`，绝不能覆盖原厂全局服务 |
| `core-ready.sh`, `start-enabled-core.sh` | 监听器 / TUN 就绪检测、有限开机重试 |
| `finalize-config.rb`, `core-clock-guard.sh` | 应用端口、TUN、内部 NTP 与同步后清 DNS 缓存保护 |
| `network-rules.sh` | 只拥有 `MU_OC_*` 链、路由表 7250、优先级 17250；覆盖 `br-lan` 的 Wi-Fi / USB 客户端 |
| `managed-plugin-update.sh` | 独立适配安装的整包插件升级保护 |

常用管理入口：设备 LAN 地址的 `:8081/cgi-bin/luci/admin/services/openclash`。当前默认 LAN 为 `192.168.3.1`，原 OpenUI `:8080` 和密钥 SSH `:2222` 保留。路径、端口及接口默认值针对当前已验证安装；更换 LAN 或固件时应先复核。

独立服务将上游 `root/etc/init.d/openclash` 作为 `/usr/share/openclash/mu5250-upstream-init.sh` 库使用，只保留订阅与配置生成逻辑。私有命名空间叠加 `/usr`、`/www`、`/etc/init.d`；应用文件落在 `/data/openclash`，原厂 DHCP、防火墙 UCI 与启动屏障服务保持由固件管理。启动入口只使用已有 `/etc/rc.local` 后台调用。

## 核心时钟修复

配套分支：[alexxxiong/mihomo-oix — codex/mu5250-ntp-clock](https://github.com/alexxxiong/mihomo-oix/tree/codex/mu5250-ntp-clock)。

原厂系统 epoch 与 UTC 有约 8 小时偏差。专用 DNS 签名及托管请求应使用核心 NTP 时钟；`finalize-config.rb` 保持 `write-to-system=false`，不修正固件系统时间。应用启动前等待本次 NTP 同步，再清除早期 DNS 答案、安装自己的接管规则；运行期间在同步事件后再次清缓存。

核心修改与此保护需要一并保留。仅启用 NTP、仅刷 DNS、仅同步订阅均不能代替核心取时修正。原固定发布核心上的等价修改已实机验证动态解析、Wi-Fi IPv4 自动代理、国内直连和 UDP；IPv6 目标完整代理仍未验证成功。本轮没有对新增保护做完整整机重启验证。

## 合并上游并更新

```sh
git remote add upstream https://github.com/pickrui/OpenClash.git # 已配置则跳过
git fetch upstream
git switch codex/mu5250-integration
git merge upstream/dev
python contrib/mu5250/boot-tests/run_tests.py
python contrib/mu5250/boot-tests/test_ubus_readiness.py
python contrib/mu5250/clock-tests.py
python contrib/mu5250/network-tests/run_tests.py
git push origin codex/mu5250-integration
```

测试支持 Linux Bash 或 Windows Git Bash；可用 `MU_OC_TEST_BASH` 指定启动 / 时钟测试的 Bash。它们用临时目录和网络命令模拟器，不连接设备、不写电脑或设备防火墙。

更新现有设备时：

1. 先保存 `/data/openclash` 和 `/etc/config/openclash` 的可用备份；原始订阅、账号状态及节点选择单独保留。
2. 将候选脚本放到 `/data/openclash/stage/`，用 `sh -n` 与 `ruby -c` 检查；上游库文件及页面只更新私有 overlay。
3. 核心先在回环监听、无 TUN / 防火墙的独立目录验证，确认 NTP 签名、动态解析、实际代理和订阅更新通过。
4. 仅停止独立应用核心后替换核心和对应脚本，再从私有命名空间启动；核对 `run/clock-ready.pid`、节点、分流及管理入口。
5. 出现异常时恢复已保存的核心与配置。不能对原厂环境运行仓库主体的安装钩子或直接用网页整包升级替换适配。

原固件保护要求：不改分区、USB composition、原厂 init.d/procd、firewall include 或系统服务 UCI；不停止启动屏障中的 ZTE 守护进程。改动 `/etc/rc.local` 后必须 `sh -n`，FOTA 更新可能清除其内容，应保留恢复资料。
