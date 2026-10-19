# 积木工坊 BrickForge

四人课设：可视化拼工厂，用操作系统算法算产量。Python 3.10+，只用标准库。

```text
python -m brickforge
```

浏览器打开 <http://127.0.0.1:8787>。**必须在项目根目录运行**，否则会报找不到模块。

调度：FCFS / SJF / SRTF / 优先级 / RR / MLFQ。磁盘：SSTF / SCAN / C-SCAN。内存：首次/最佳/最坏适应 + LRU/FIFO 分页。另有银行家死锁避免、PCB、系统调用轨迹。

## 覆盖的操作系统原理（对照课件）

| 课件要求 | 落在哪 | 怎么看 |
| --- | --- | --- |
| 进程控制和管理 | `workers.py` 每车间一个 `Process`，启动登记、结束注销 | 实验台「进程」页 PCB 表 |
| **守护进程** | `daemon.py` 独立进程，完整创建流程，**发现进程失联就真拉起** | 实验台「进程」页被守护进程表；`data/daemon.log` |
| **信号处理 / 可靠信号 / 信号处理程序** | `signals.py` POSIX 用真信号（含实时信号 SIGRTMIN），Windows 降级为软信号 | 实验台「同步」页信号一栏 |
| **进程间通信四种齐全** | 管道 `ipc.make_pipes` · 消息队列 `Queue` · 信号量 `Semaphore` · 共享内存 `SharedMemory` | 实验台「同步」页 |
| 线程创建、调度、同步 | `workers.py` 每机器一个 `Thread`；`state_lock` 互斥；共享工装信号量 | 实验台「同步」页共享工装等待 |
| 文件 I/O 与文件锁 | `persist.py` JSON 读写 + 跨平台 `msvcrt`/`fcntl` 文件锁 | 系统调用轨迹首尾两行 |
| 处理机调度 | `scheduler.py` 六种算法 | 实验台「调度」页对照表 + 甘特图 |
| 内存管理 | `warehouse.py` 三种放置策略 + 碎片率 | 实验台「内存」页 |
| 虚拟内存 | `memory.py` FIFO / LRU | 实验台「内存」页步进表 |
| 磁盘调度 | `shipping.py` SSTF / SCAN / C-SCAN | 实验台「设备」页路径图 |
| 死锁避免 | `banker.py` 安全性算法 | 实验台「内存」页安全序列 |
| 图形界面 | `app/` 积木画布 + **实验台** | 页面底部六个页签 |

## 四人分工（按答辩主讲切，不要抢文件）

| 人 | 主讲 | 只改这些 | 做成什么才算交差 | 别人不准动 |
| --- | --- | --- | --- | --- |
| **甲** | 进程/线程怎么被用户拼出来 | `app/`、`app/js/lab.js`、`app/js/bench.js` | 厂区两层积木、连线、工人、件/分；实验台把 `lab.*` 画出来 | 不写四种调度、不写 Daemon |
| **乙** | 处理机调度 | `brickforge/scheduler.py`、`tests/test_sched.py` | FCFS / SJF / 优先级 / RR / SRTF / MLFQ；甘特条；算法对照表 | 不改画布 DOM、不写 fork |
| **丙** | 同步与 IPC | `brickforge/sync.py`、`workers.py`、`ipc.py`、`signals.py`、`tests/test_sync.py` | 车间=进程、机器=线程；管道/队列/信号量/共享内存四种 IPC；能看出空转与阻塞 | 不改乙的调度算法 |
| **丁** | 守护进程、文件锁、内存/磁盘类比 | `brickforge/daemon.py`、`persist.py`、`warehouse.py`、`shipping.py`、`tests/test_store.py`、`tests/test_daemon.py` | 独立守护进程**真保活重启**；布局文件锁；货架三种适应；出货 SSTF | 不许用前端定时器冒充 Daemon |

公共契约只有一份：`POST /api/simulate` 返回的 JSON。乙写 `lab.compare` / `lab.gantt`，丙写 `lab.sync` / `lab.signals`，丁写 `lab.warehouse` / `lab.shipping`，甲只读这些字段画实验台。`brickforge/kernel.py` 是胶水，改字段先在组里说一声。

交叉验收：甲查乙的数字是不是真变；乙查丙能不能看出阻塞；丙查丁是不是独立进程；丁查甲的 JSON 内核读不读得懂。

## 目录

- `app/` 甲 · 积木画布与实验台
  - `app/js/bench.js` 实验台：调 `/api/simulate` 并渲染六个页签
- `brickforge/scheduler.py` 乙 · 调度
- `brickforge/sync.py` `workers.py` `ipc.py` `signals.py` 丙 · 同步 / IPC / 信号
- `brickforge/warehouse.py` `shipping.py` `daemon.py` `persist.py` 丁 · 仓储 / 出货 / 守护
- `tests/` 各人自己的冒烟脚本
- `handbook/` 课设手册

## HTTP 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/layout` | 读布局 |
| POST | `/api/layout` | 存布局 |
| POST | `/api/simulate` | 跑一遍仿真，返回全部算法结果 |
| POST | `/api/stop` | 急停：宣布收工，再给每个车间进程发 STOP 信号 |
| GET | `/api/procs` | 被守护进程登记表 + 信号能力 |
| GET | `/api/board` | 共享内存看板快照 |
| GET | `/api/status` | 守护进程状态 + 上次仿真结果 |
| POST | `/api/run` | **开长跑班次**：车间进程常驻，守护进程才真有活干 |
| GET | `/api/run` | 长跑班次视图：守护中 / 已失联 / 心跳 / 已拉起 + 日志 |
| POST | `/api/run/stop` | 收工：停班次并清理登记（收工后守护进程不再拉起） |
| POST | `/api/kill` | **硬杀一个车间进程（模拟崩溃）**，用来现场验证守护进程会把它拉起来 |
| POST | `/api/reset` | 恢复示例工厂 |

## 守护进程怎么演示（课件第 7—12 页那一项）

守护进程的职责是"确保被指定进程的运行"。但 `/api/simulate` 是**一锤子买卖**：
车间进程只活几十毫秒就跑完注销，守护进程每 2 秒才巡视一次，等它睁眼车间
早就正常下班了 —— 于是 `data/daemon.log` 里永远是"守护=0"，看着像摆设。

**长跑班次**解决这个问题：车间进程常驻、持续消化工单，守护进程于是真的有
守护对象；杀掉其中一个，2 秒内就会被按登记信息拉起来。

界面上点：底部**「守护」页签** → 「开始长跑生产」→ 表格里出现"运行中"的车间
进程 → 点某个车间的「杀掉它」→ 两秒后同一行会变成新的 pid、`已拉起` +1，
右侧 `daemon.log` 出现：

```text
13:35:57 测谎：硬杀车间 s1 pid=33260（TerminateProcess，模拟崩溃），等守护进程拉起
13:35:58 车间 s1 进程 pid=33260 已失联，拉起新进程 pid=600（第 1 次）—— 离线运行：不参与上下游管道
13:35:58 巡视 守护=2 异常退出=1 心跳过期=无 累计拉起=1
```

**两个必须讲清楚的点（答辩会被问）：**

1. **必须"硬杀"**。如果发 `SIGTERM`，车间进程的处理程序会接住、正常注销，
   守护进程只会认为它下班了，永远不拉起。所以 `kill()` 在 POSIX 上用
   **SIGKILL**（不可捕获）、Windows 上用 **TerminateProcess**。这一点在
   Windows 上验证通过**不代表** Linux 上成立 —— Windows 的 `SIGTERM`
   恰好就等于 `TerminateProcess`。
2. **被拉起的车间是"离线"的**。管道端点和跨进程信号量只在其创建者的进程树内
   有效，守护进程没法把仍在运行的上下游手里那些旧端点换掉。所以拉起的新进程
   照常消化工单、写共享内存看板、也能被再次杀死/拉起，但**不参与上下游管道
   投递**。要真正做到"拉起后拓扑仍通"，得把每条边换成 `Manager` 里的命名
   队列并把地址写进 `run.json`（约半天工作量），本版本没做。

命令行等价操作：

```text
curl -X POST http://127.0.0.1:8787/api/run    -d "{\"shop_seconds\":120}"
curl -X POST http://127.0.0.1:8787/api/kill   -d "{}"
curl        http://127.0.0.1:8787/api/run
curl -X POST http://127.0.0.1:8787/api/run/stop
```

平台差异（答辩要讲）：POSIX 上守护进程走完整 7 步（fork/setsid/umask/chdir/
关 fd/dup2/业务），能真正脱离终端；Windows 没有 fork/setsid，只能"独立进程"。
另外守护进程自己也被反向守护：服务被强杀（关控制台/任务管理器）时，它会在
十几秒内发现主人心跳停了并主动退出，不留孤儿。

## 自测

要加 `-m`（直接 `python tests/xxx.py` 会因为路径问题报 `ModuleNotFoundError`）：

```text
python -m tests.test_sched       乙 · 六算法对照
python -m tests.test_store       丁 · 仓储 + SSTF
python -m tests.test_osextra     丁 · 银行家 / 分页 / SCAN
python -m tests.test_sync        丙 · 队列 + 有界缓冲
python -m tests.test_ipc_queue   IPC 冒烟
python -m tests.test_primitives  信号 / 共享内存 / 管道 / 进程登记
python -m tests.test_kernel      全链路
python -m tests.test_daemon      守护进程真保活（杀进程看是否被拉起）
python -m tests.test_run_guard   **长跑 + 保活**：断言新 pid 真的活着、拉起次数 +1、日志有记录
```

端到端（要占 8787 端口，适合答辩前跑一遍）：

```text
python tests/test_api_guard.py
```

## 演示脚本（答辩用）

1. **换调度算法数字就变**：底部「运行仿真」→「参数」→ 把处理机调度从"优先级（静态）"改成"RR"→ 应用并重跑。
   「调度」页看平均等待从 3.33 变成 6.0、上下文切换从 2 变成 6。
2. **看阻塞**：把货架容量改成 `8`（只有 1 个信号量槽）→ 重跑，「同步」页库位阻塞次数从 0 变正数。
3. **看守护进程保活**：底部「守护」页 →「开始长跑生产」→ 点某车间的「杀掉它」，
   两秒后它被拉起、`已拉起` +1、右侧日志留证。命令行可用 `python -m tests.test_run_guard`。
4. **看四种 IPC**：「同步」页一屏内同时看到管道明细、队列、信号量槽、共享内存看板。
5. **看服务被强杀不留孤儿**：直接结束服务进程（模拟关控制台），十几秒内守护进程
   自己发现主人没了并退出，`data/daemon.log` 会写"主人 pid=… 已退出"。
