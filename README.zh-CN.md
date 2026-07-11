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
