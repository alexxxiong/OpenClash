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

手机连接 MU Wi-Fi 也已能打开站点，但当前尚未建立 UDP 直连，DERP 中继使视频性能不达标。实测同一 512 KiB 分段在家中直连总耗时约 0.15 秒，经 MU 隧道约 26 秒，正文吞吐约 29 KiB/s；TCP MSS 已核对为 1240。HTTP 200/206 只证明资源可达，不能代表连续播放流畅。进一步改善需检查家中上级光猫的公网/端口映射条件，或另行配置合适的公网中继。
