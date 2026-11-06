# 原厂屏幕“网络”页 OpenClash 开关

2026-11-07，在国行 MU5250 B27 的原厂“网络”页末尾加入原生开关。显示
`OpenClash · 运行中 / 已关闭 / 启动中 / 停止中 / 启动失败 / 状态未知`。
触摸只调用独立应用的 enable/start 或 disable/stop；状态同时核对核心、
监听器、内部时钟与应用接管规则，不把 UCI 的启用位当作运行成功。

## 当前实现

| 文件 | 用途 |
| --- | --- |
| `screen-network.c` | 同 GUI 线程的定时器与原生 TUSettingSwitchButton；异步执行固定控制命令 |
| `screen-control.sh` | 序列化 on/off；有界、只读的真实状态检查 |
| `screen-status-poll.sh` | 独立进程轮询，原子发布状态缓存 |
| `screen-network-watch.sh` | 原厂挂载空间中的启动接入；校验文件、等待 UI、每个 PID 只尝试一次 |
| `screen-ready.c` | 只读检查显示缓冲、LVGL 定时器及界面主循环心跳；替代固定延迟 |
| `screen-start.sh` | 早期与后期入口共用的幂等调度器，保留现有监控及日志 |
| `screen-early.init` | 单独的 S19 屏幕扩展入口，后台等待；不创建 procd 实例 |
| `screen-loader/` | ARM64 musl 加载工具、隔离进程夹具、便携构建及验证工具源码 |
| `test-screen-control.py`, `test-screen-child.c` | 离线状态/串行控制回归和真实 Linux 子进程回收回归 |

不改原厂 UI 可执行文件、虚表或磁盘固件，不重启原厂 UI。加载器只在
被确认阻塞的主线程上暂时调用目标 libc 和显式入口；返回后逐项核对
GPR、FP/NEON、TLS、系统调用号和信号屏蔽字再分离。共享库与少量 scratch
内存保留到 UI 进程退出，移除入口只删除新增控件和定时器。

这套 native 地址只适用于 UI SHA-256：

```text
571a201d18ba5ae80ce9f2b92948e63e9f73f2ead22b1920d3f58a6716f07028
```

`/data/openclash/lib/screen-network.manifest` 的单行格式是：
`UI_SHA256 LOADER_SHA256 LIBRARY_SHA256 /data/openclash/lib/screen-network-HASH12.so`。
它和二进制均为 root 所有，私有权限。源码构建不会自动安装、生成设备清单
或把某个历史 SHA 指派给新二进制。必须先完成新工具的隔离进程验证。
加载超时不保证能撤销目标 loader 锁或构造函数副作用，不对同一 PID 自动重试。

## 重启与关闭状态

独立 LuCI 和核心仍由现有 `/etc/rc.local` 后台调用 `start-openclash.sh`。
屏幕扩展另设应用自己的 `/etc/init.d/mu5250-screen` 和
`/etc/rc.d/S19mu5250-screen -> ../init.d/mu5250-screen`。该入口排在原厂
`S19zte_topsw_daemon` 之前，只后台调度屏幕 watcher，前台立即返回。
它不创建 procd 实例、不加入 ZTE 守护进程同步屏障、不启动代理核心。

早期入口与原有后期入口都调用 `screen-start.sh`；完整 argv/PID 检查和
flock 防止重复启动，已有 watcher 的日志不再被后期调用截断。数据文件
尚不可用时只有后台 worker 最多等待 60 秒，不阻塞固件启动队列；原有
rc.local 屏幕调度仍保留作为后期回退。

watcher 等原厂 UI 就绪后加载控件；没有打开“网络”页时，控件定时器会
等待页面对象出现。屏幕入口提前不会提前独立 LuCI 或核心；早期后台尚未
就绪时控件可能先显示“状态未知”并禁用操作，随后由原状态轮询自动更新。

已移除原来的固定 30 秒延迟。未接入的 UI 每秒运行一次只读探测：确认
原厂进程身份和固定版本指令、320×480 RGB565 DRM 显示映射、LVGL 定时器
启用、32 字节节点的有效定时器链表，再观察界面定时器主循环的计时值实际
推进。单次探测最多观察 1.2 秒；未就绪退出 75，watcher 继续等，不消耗
唯一的加载尝试。真正调用加载器前再确认 PID；加载失败仍不对同一 PID 重试。

探测器只用 O_RDONLY 读取 proc 文件和进程内存，不 ptrace、不暂停线程、
不写进程内存、不调用 UI。它与加载器、控件库分别校验 SHA256；探测器
校验值单独存于 `/data/openclash/lib/screen-ready.sha256`。当前已加载的
控件直接由新 watcher 接管，不卸载、不重新注入。

关闭开关调用服务 `disable`（`uci set ...enable=0; uci commit openclash`），
然后 `stop`。开启则保存 1，再启动。`private-init.sh` 仅在 enable=1 时
调用 `start-enabled-core.sh`，后者每次重试前再次检查启用位。因此：

- 开启后重启：LuCI、屏幕控件和 OpenClash 核心会按启动流程启动。
- 关闭后重启：LuCI 和屏幕控件会启动，核心保持关闭；可以在屏幕重新开启。
- 用户自行重启后，已实测开启状态下核心及屏幕入口自动恢复。关闭状态
  重启只核对了落盘和启动 guard，尚未实测。
- 就绪检测优化没有再次重启设备；新启动顺序经模拟回归、实机只读探测和
  watcher 接管验证。随后用户重启证明 S95 调度本身偏晚：原厂 UI 进程
  24.68 秒启动，监控 55.93 秒启动，而监控启动后约 1 秒即完成加载。
- 新增独立 S19 屏幕入口解决启动阶段偏晚；部署时不重启整机，完整冷启动
  显示时机仍待实测，不能把当前运行中接管时间当作冷启动时间。

固件升级改变 UI 校验和时 watcher 会跳过接入；需要针对新 UI 再验证。

## 实机验收

用户在实际屏幕完成关闭、开启。首轮发现原厂 SIGCHLD 回收了控制子进程，
按钮仍保留旧子 PID 而被禁用；已处理 `waitpid()` 的 `ECHILD`。
第二轮开启成功，但旧缓存曾把开关短暂画回关闭；已让 pending 请求保持
目标位置并显示过渡状态，直到新状态确认完成或失败。用户最终确认
“关闭和开启正常，没有回退”。

状态缓存采用 `/proc/uptime` 对应的 `CLOCK_BOOTTIME`，避免休眠后
MONOTONIC 与缓存时间相差导致误报。未知/过期缓存禁用控制并显示未知。

29 项 bridge 测试通过；设备原生 Linux 上 4 项子进程测试通过（正常、
失败、外部回收、SIGCHLD 忽略）。隔离 dummy 的构造、入口、移除、完整
寄存器恢复、canary、heartbeat 和协作退出通过。多次 UI 接入/移除保持
原 UI PID、LuCI 命名空间及 Web PID，TracerPid 恢复 0。

Wi-Fi 请求 Google 204、国内站点 200；美国 Fusion 08、Others→Proxy、
Domestic→DIRECT 保留。原厂 UI、防火墙/DHCP UCI、rc.local、OpenClash
配置、订阅文件、运行配置和接管脚本哈希与接入前相同。托管 provider 缓存
在核心启动/刷新后有变化，不作为静态订阅或策略已改变的证据。此轮未重测 USB。

## 回退

本机部署保存的回退入口：
`/data/openclash-backups/screen-network-94479ca23f04/rollback.sh`。
它按完整 argv 停止两个自有辅助进程，调用版本校验的 `mu_screen_uninstall`，
并恢复接入前的 `/data/openclash/bin/start-openclash.sh`。不停止代理核心、
不修改用户的订阅或节点，也不重启原厂服务。脚本已语法检查；完整回退未
实机执行，原生控件移除/恢复入口已反复验证。

## 验证命令

```sh
python3 contrib/mu5250/test-screen-control.py
clang -std=c11 -O2 -Wall -Wextra -Werror contrib/mu5250/test-screen-child.c -o /tmp/test-screen-child
/tmp/test-screen-child
python3 contrib/mu5250/screen-loader/build.py --zig /path/to/zig
python3 contrib/mu5250/screen-loader/verify_build.py
# Linux 上执行只读就绪探测夹具与 watcher 调度回归，不连接设备。
cc -std=c11 -O2 -Wall -Wextra -Werror contrib/mu5250/screen-ready.c -o /tmp/oc-screen-ready
MU_OC_READY_TEST_BINARY=/tmp/oc-screen-ready python3 contrib/mu5250/test-screen-ready.py
python3 contrib/mu5250/test-screen-watch.py
python3 contrib/mu5250/test-screen-start.py
```

子进程测试仅在 Linux 原生运行；Windows 只交叉编译 ARM64 版本，不能直接
执行。`screen-loader/test_dummy.py` 仅用于已审阅的独立 AArch64 测试进程，
不能作为原厂 UI 的自动验收脚本。
