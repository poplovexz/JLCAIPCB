# JLCAIPCB

[English](README.md)

JLCAIPCB 是一套面向 Codex、由规格驱动的 KiCad 生产级 PCB 流程。它把小白用户描述的使用场景转换为受控工程流程，覆盖需求确认、模块架构、物料选择、Spec Freeze、原理图生成、封装与引脚映射、PCB 布局、布线候选、严格本地验证，以及嘉立创/JLCDFM 下单证据。

本仓库包含：

- `skills/kicad-production-pcb/` 中完整的 Codex SKILL；
- 确定性 Python 门禁、策略、参考文档和 golden benchmark；
- `hooks/` 中的项目级生产前流程钩子；
- 用于要求 Codex 执行钩子的 `AGENTS.md` 模板；
- 英文和简体中文安装说明。

## 它解决什么问题

JLCAIPCB 是流程控制器和证据系统。它以 `specs.yaml` 为唯一事实来源，并拒绝过期、不完整或混合版本的下游产物。它不会宣称 ERC/DRC 等于功能正确，也不会替代 KiCad、Freerouting、实验室测试或制造商审核。

## 用户要求如何变成 PCB

项目从用户用自然语言描述“这块板要做什么”开始，Codex 通过受门禁约束的流程把要求转换为 PCB：

1. 与用户确认功能、供电、接口、关键器件、尺寸、制造目标和安全约束。
2. 把确认后的要求写入机器可读的 `specs.yaml`，形成模块架构、功率/电流预算和选料约束。
3. 在生成硬件文件前选择并锁定真实器件、符号、封装、引脚映射和供应商证据。
4. 对批准的 Spec 执行冻结，再生成真实 KiCad 原理图和 PCB，而不是把 AI 示意图当成设计源。
5. 执行叠层、阻抗、布局、布线、过孔、背钻、ERC 和 DRC 门禁；证据不完整或已过期就停止流程。
6. 按目标阶段导出并核验 Gerber、钻孔、BOM、贴片坐标、发布清单和板厂审核证据。

当前流程支持由策略控制的 2–32 偶数铜层，并自动回归验证 6–16 层 PCB 生成和 DSN 导出。详细物理叠层、阻抗证据、盲孔/埋孔/微孔、背钻输出和板厂能力必须通过验证，才能声明进入生产或可下单阶段。

## 快速安装

```bash
git clone https://github.com/poplovexz/JLCAIPCB.git
cd JLCAIPCB
./install.sh
```

安装后重启 Codex，然后输入：

```text
使用 $kicad-production-pcb，根据我的使用场景开始 PCB 设计。
```

如果要把 SKILL 和流程钩子安装到某个项目：

```bash
./install.sh --project /你的/KiCad项目路径
```

在现有项目启用钩子前，请先阅读[简体中文安装说明](docs/INSTALL.zh-CN.md)。

## 十个核心阶段

1. 小白需求收集与确认。
2. 模块级架构。
3. 供应链优先的候选物料与锁料。
4. 本地事务性 Spec Freeze 和 PCB Build Brief。
5. 真实 KiCad 原理图与 strict ERC。
6. 符号、封装、焊盘、极性和贴片映射。
7. 约束驱动的 PCB 布局。
8. 事务性布线候选与 strict DRC。
9. 干净的本地生产验证和精确发布清单。
10. 与发布文件哈希绑定的嘉立创/JLCDFM 外部证据。

实物上电、功能测试和实验室验证发生在制板之后，不属于上述十个制板前阶段。

## 环境要求

- 支持文件系统 SKILL 的 OpenAI Codex；
- Python 3.10 或更高版本；
- PyYAML；
- 与项目 Spec 要求一致的 KiCad CLI；
- 钩子需要 Bash 和 `flock`；
- 安装文档中说明的项目侧 KiCad 生成与导出适配脚本。

## 安全要求

不要把 GitHub Token、供应商账号、密码、验证码、短信代码或支付信息写入 Spec、提示词、证据文件、Git remote 或 Shell 历史。网页登录和付款必须由用户明确操作。

## 许可证

MIT，参见 [LICENSE](LICENSE)。
