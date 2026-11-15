# MU5250 与家中 ASUS 的 Tailscale 子网互通

此目录保存当前 MU5250 适配的可维护脚本。它不是通用 OpenWrt 安装包，也不替换原来的 OpenClash / oixClash。

当前部署网段为 MU5250 `192.168.3.0/24`、家中 ASUS `192.168.50.0/24`。学习站点 `learn.dev.inspirai.store` 指向 `192.168.50.20`。所有地址、接口和路径均针对这一安装；换网段时须一并调整和测试。

## 组成

- MU：`start.sh`、`watch.sh`、`rules.sh`、`stop.sh` 安装到 `/data/tailscale-home/`。独立 Tailscale 守护进程使用 `ts-home`，身份状态留在设备私有目录。原厂服务、USB composition、网络 UCI 和分区保持由固件管理。
- `home-dns.rb` 只在 `/data/tailscale-home/home.json` 明确启用时，为学习域名添加真实地址和 Fake-IP 排除；`finalize-config.rb` 在输出最终配置前调用此可选模块。没有设置文件或模块时保持原行为。
- `apply-dns.rb enable|disable` 在现有私有命名空间中运行，保存精确字段的前像并热加载配置，不重启核心；控制器凭据只在设备内使用。
- ASUS：`router-rules.sh`、`router-watch.sh`、`router-start.sh` 分别以 `rules.sh`、`watch.sh`、`start.sh` 安装到 `/jffs/softcenter/mu5250-home-site/`。`router-init.sh` 同时保存在该目录的 `init.sh`，及酷软自己的 `S97mu5250home.sh`、`N97mu5250home.sh` 启动入口。
- `rollback.sh` 和 `router-rollback.sh` 分别回退本任务的应用改动。它们依赖部署时保留的 `*-backup-20261115` 前像，不可在缺少对应备份的其他设备直接使用。

## 前提与设置

两台网关均为各自终端的默认网关，子网不重叠，IPv4 转发已启用，均具备 `/dev/net/tun`。ASUS 使用已安装的酷软 Tailscale 插件，MU 使用经过官方校验的独立静态客户端。不要将身份私钥、授权链接、订阅或运行 YAML 提交到 Git。

两端加入同一个 tailnet 后，在各自客户端设置：

```sh
# MU：使用 /data/tailscale-home/bin/tailscale 及其专用 socket
tailscale set --advertise-routes=192.168.3.0/24 --accept-routes=true --accept-dns=false --snat-subnet-routes=false
# ASUS：使用原插件客户端，保留插件启动流程
tailscale set --advertise-routes=192.168.50.0/24 --accept-routes=true --accept-dns=false --snat-subnet-routes=false
```

在管理控制台批准两个 `/24`，并确认已有访问控制允许两子网相互访问；不选择互联网出口，不改变原代理策略组。MU 使用 `--netfilter-mode=off` 与自身限定子网的规则；ASUS 保留原插件管理的 Tailscale 规则。

ASUS 插件 2.0.8 即使 `NoSNAT=true`，仍添加独立 MASQUERADE。此适配在前面增加仅匹配家中网段→MU 网段的 NAT ACCEPT，保留其他插件规则。双方仅为两网段的 TCP SYN 做 MSS clamp。ASUS 的 iptables 1.4.12.2 不支持 `-w`，因此自身更新用 flock 串行化。

MU 的唯一新增启动调用位于 `/etc/rc.local` 的 `BEGIN MU5250 HOME ACCESS` 标记块中，以后台方式执行 `/data/tailscale-home/start.sh`。必须保留原厂保护块，并在修改后运行 `sh -n /etc/rc.local`。ASUS 使用其已有软件中心的 S/N 调度，不改 MU 原厂 init.d/procd。

路由就绪且原域名的 SNI/TLS 测试通过后，才能在现有 OpenClash 私有命名空间中执行 `ruby /data/tailscale-home/apply-dns.rb enable`。学习站点的 Traefik `dev/learn-lan` 允许列表也需保留原范围并加入 `192.168.3.0/24`；隧道不会绕过服务自身的授权或终端防火墙。

## 检查与测试

`test-home-dns.rb` 在设备私有 Ruby 环境中，以独立可写测试目录为参数运行；测试使用相邻的 `home-dns.rb`，不修改实际 DNS 设置。

两个防火墙测试只允许在不同于主机网络的 Linux 网络命名空间内运行，测试目录预置为 `/opt/mu5250-home/tests/`。例如将对应脚本复制到该目录后：

```sh
MU_HOME_HOST_NS=$(readlink /proc/self/ns/net) unshare --net --fork bash /opt/mu5250-home/tests/test-rules.sh /opt/mu5250-home/tests/rules.sh
MU_HOME_HOST_NS=$(readlink /proc/self/ns/net) unshare --net --fork bash /opt/mu5250-home/tests/test-router-rules.sh /opt/mu5250-home/tests/router-rules.sh
```

7 项 DNS 检查和双方各 6 项内核隔离检查已通过。规则测试覆盖限定子网、双向 MSS、无 SNAT、幂等、路由缺失、链冲突、部分失败回退及缺失跳转修复。

实机验收须强制走 MU 的 USB 或 Wi-Fi。电脑同时连家中 Wi-Fi 时，普通 curl 成功不能证明经过隧道。还应从家中主机反向访问 MU，核对源地址、回程和终端入站策略。2026-11-15 已通过 USB 访问学习站点及家中主机访问 MU 管理服务；未对整机进行重启验收。

公网中继前的基线：手机连接 MU Wi-Fi 能打开站点，但官方 DERP 路径上的视频性能不达标。同一 512 KiB 分段在家中直连约 0.15 秒，经 MU 隧道约 26 秒，正文吞吐约 29 KiB/s；TCP MSS 为 1240。HTTP 200/206 只证明资源可达，不能代表连续播放流畅。

## 公网 Peer Relay

双方都支持 Tailscale 1.86 以后的 Peer Relay 功能，可在有公网 UDP 入口的第三台服务器运行中继。此服务只转发经过认证的加密流量，不需要发布子网、接管 DNS 或选择互联网出口。现有网关节点与 OpenClash 分流保持。

`peer-relay.service` 是独立服务器单元模板：官方已校验的静态客户端位于 `/opt/mu5250-relay/bin/`，状态位于 `/var/lib/mu5250-peer-relay/`，专用 socket 位于 `/run/mu5250-peer-relay/`，接口为 `ts-mu-relay`。不要覆盖服务器原来的 Tailscale 或 Web/TURN/远程桌面服务。

安装服务后，先授权服务器加入同一 tailnet，再设置中继。`--relay-server-port` 属于 `set`，不应传给 `up`：

```sh
/opt/mu5250-relay/bin/tailscale --socket=/run/mu5250-peer-relay/tailscaled.sock up --hostname=mu5250-wuhan-relay --accept-dns=false --accept-routes=false --netfilter-mode=off
/opt/mu5250-relay/bin/tailscale --socket=/run/mu5250-peer-relay/tailscaled.sock set --relay-server-port=40000 --relay-server-static-endpoints='<PUBLIC_IP>:40000'
```

云安全组需允许 UDP 40000 入站；此端口的中继分配由 Tailscale 身份及能力授权控制。加入下面的 grant，替换三个 Tailscale IPv4 地址，并保留现有策略。不要直接用示例覆盖整个策略文件：

```json
{
  "src": ["<MU_TAILSCALE_IP>/32", "<ASUS_TAILSCALE_IP>/32"],
  "dst": ["<RELAY_TAILSCALE_IP>/32"],
  "app": {"tailscale.com/cap/relay": []}
}
```

使用 `tailscale ping` 和 `status` 确认实际路径为 `peer-relay(<PUBLIC_IP>:40000:...)`。初始探测可能先经过 DERP，再切换到 Peer Relay。`direct connection not established` 表示没有端到端直连，不表示 Peer Relay 失败。

2026-11-15 武汉中继初次验收：512 KiB 分段总耗时从 26.09 秒降到 2.45 秒，正文吞吐从 29.4 升到 261 KiB/s，Peer Relay 往返通常约 68–120 ms。当时手机明显改善但偶尔仍缓冲；后续进一步定位和优化见下节。

`peer-relay-rollback.sh` 只停止并禁用本适配的服务器单元；完整撤回还需精确移除本次新增的能力授权与云安全组端口规则，保留其他规则。身份状态、完整 tailnet 策略和授权链接不应进入源码。

## 家中 Web 入口的 TCP 优化

进一步测试中，公网服务器→家中可达到约 54 Mbps，公网服务器→MU 可达到约 77 Mbps；MU 经隧道下载学习视频却只有约 2–3 Mbps。MU CPU 和中继 UDP 接收缓冲有余量；按实际 UDP 数据报拆分 GRO 合包后，中继→MU 的样本未见丢包。家中 TLS 发送端的 CUBIC 则存在较多重传及窗口退缩。这个结果不支持直接把缓冲归因于购买带宽上限。

`home_tcp_optimize.py` 为单节点 K3s 的本地 Traefik 网络命名空间添加一条到 `192.168.3.0/24` 的返回路由，下一跳和接口沿用原默认路由，仅设置 `congctl bbr`、私有协议标记 242。宿主机和命名空间的全局 TCP 默认值仍是 CUBIC；网关分流、代理节点、MTU、视频编码和服务进程保持。

适用前提是 Python 3.9+、K3s、宿主机具备 BBR 模块，Traefik 的原始客户端地址得到保留。脚本拒绝宿主机网络命名空间，拒绝覆盖既有路由，用 namespace fd 防止 PID 重用，并记录自身路由；清理时只移除仍与自身记录一致的条目。

部署时把脚本安装为 `/opt/mu5250-home-tcp/optimize.py`，服务模板为 `mu5250-home-tcp.service`；其状态目录是 `/var/lib/mu5250-home-tcp/`。服务每 15 秒检查本地 Traefik 容器，可在 Pod 更换后重新应用。不需要重启 K3s、Traefik、网关或 OpenClash。此实现针对当前单节点部署，改变子网或入口控制器时须复核。

在家中 Linux 上先执行 `modprobe tcp_bbr`，再运行隔离测试：

```sh
python3 test_home_tcp_optimize.py ./home_tcp_optimize.py
```

测试自动创建临时网络命名空间，7 项内核检查覆盖宿主命名空间拒绝、外来路由保护、限定目的地、默认值保持、幂等、精确回退、用户修改保护及状态写入失败回退。生产服务启用后只读状态与下载验证通过，未执行生产服务启停或冷启动验收。

实际 A/B/A 测试，同一 4 MiB 分段在 CUBIC 下约 11–17 秒，BBR 两次约 1.65 秒；保留限定网段配置后，16 MiB 持续下载约 5.59 秒，即约 24 Mbps。高码率示例平均约 1.6 Mbps，一秒峰值约 4.1 Mbps；原文件已 faststart，关键帧间隔约 10.4 秒。新连接具备更大余量，仍需以手机实际播放验收，已有长连接应关闭页面后重新打开。

`home-tcp-rollback.sh` 停止并禁用自身优化服务，清理其仍保持原值的路由，不覆盖他人改动。既有 TCP 连接的算法在连接生命周期内保持；服务不强制断开连接。
