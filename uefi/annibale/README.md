# annibale 维护副本

这里是 annibale 平台专属的 Mu-Silicium 覆盖文件副本，按 `files/<相对路径>` 存放，与 `uefi/files/`（redwood 移植的导出目录）分开维护：`uefi/export.sh` 重写 `uefi/files/` 时不读取本目录，也不要对 annibale 工作树运行 `export.sh` 或 redwood 的 `apply.sh`。

`BASE` 记录本副本对应的 Mu-Silicium 提交 `87416d23ecde1a2be3a5b0b00b8c914071e802e1`。该提交是本机受控 annibale 基础树上的本地历史提交，未确认可从公开 upstream fetch；它不表示通用 apply 流程已支持 annibale，也支撑不了全新复现声明（U7 的 BASE 复现仍未解决）。

应用方式是显式覆盖，靠人工按步骤执行：先确认目标 Mu-Silicium 工作树 HEAD 等于 `BASE`，再逐个比较目标文件与下表基线哈希；一致才把 `files/` 下的文件按相同相对路径复制到目标树，不一致先停下核对归属。

| 文件 | 基线哈希（应用前目标文件应等于此值） | 目标路径（相对 Mu-Silicium） |
|---|---|---|
| MemoryMapLib.c | `7516ffe8c689a127ff8cfdb8f1ce31d3df17d327a218c394f9a2df34f245a8bb` | `Platforms/Xiaomi/annibalePkg/Library/MemoryMapLib/MemoryMapLib.c` |

副本内容是在内存映射表中新增 `XNU_Payload` 保留区间 `[0xF0000000, 0xF3800000)`（`AddMem, SYS_MEM, SYS_MEM_CAP, Reserv, WRITE_BACK_XN`），在 PEI 阶段就把 BootShim 复制 payload 的窗口排除出 DXE 可分配范围；区间终点受 DTB `trust_ui_vm_region` 静态保留起点约束。当前副本内容哈希为 `1d94d9b2abec170cd5560475e7b57e59f049796c60183993c8b4e98e6f6bcee6`。

XnuLoader.c/h 继续走 `uefi/files/` 的既有 overlay，Mu 与 kernel 两份的字节一致性由 `tools/test-xnu-payload-terminal.py` 检查；本副本与 Mu 文件的字节一致性、保留区间与 DTB 静态范围的契约由 `tools/test-annibale-payload-contract.py` 检查。本副本只覆盖表内文件，不能独立重建完整 annibalePkg，完整导入与基线固定暂列本范围之外。
