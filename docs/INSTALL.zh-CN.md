# 安装与 Codex 集成

## 1. 安装个人 Codex SKILL

默认安装器会把 SKILL 复制到 `${CODEX_HOME:-$HOME/.codex}/skills/kicad-production-pcb`：

```bash
./install.sh
```

安装后重启 Codex，让技能列表重新加载。验证命令：

```bash
test -f "${CODEX_HOME:-$HOME/.codex}/skills/kicad-production-pcb/SKILL.md"
python3 skills/kicad-production-pcb/scripts/run_golden_benchmark.py
```

如果目标位置已有内容不同的同名 SKILL，安装器默认拒绝覆盖。确认差异后才能使用 `--force`。

## 2. 安装到 KiCad 项目

```bash
./install.sh --project /你的/项目路径
```

将安装：

```text
<项目>/codex-skills/kicad-production-pcb/
<项目>/hooks/pre-final-pcb-flow.sh
<项目>/templates/AGENTS.jlcaipcb.md
```

安装器不会覆盖项目已有的 `AGENTS.md`。检查路径和命令后，把 `templates/AGENTS.jlcaipcb.md` 中需要的规则合并到项目 `AGENTS.md`。

## 3. 钩子契约

`hooks/pre-final-pcb-flow.sh` 是 Codex 在声明生产包或可下单之前调用的项目工作流门禁，不是未经说明的 Codex CLI 原生生命周期事件。

目标项目需要提供以下适配脚本：

```text
scripts/generate_project.py
scripts/run_flow.py
scripts/kicad_check.py
scripts/final_gate.py
scripts/package_jlcpcb.py             # manufacturing.target 为 jlcpcb 时
scripts/jlcpcb_gate.py                # manufacturing.target 为 jlcpcb 时
scripts/release_jlcpcb.py             # 配置 release 时
```

SKILL 提供通用阶段门禁；项目负责 KiCad 生成和制造商专项适配，因为这些实现依赖项目 schema 和输出契约。

显式运行钩子：

```bash
hooks/pre-final-pcb-flow.sh specs/<project>.yaml --require-checks --require-fab
```

需要使用个人安装的 SKILL 时：

```bash
KICAD_PRODUCTION_PCB_SKILL_SCRIPTS="$HOME/.codex/skills/kicad-production-pcb/scripts" \
  hooks/pre-final-pcb-flow.sh specs/<project>.yaml --require-checks --require-fab
```

## 4. 推荐项目结构

```text
AGENTS.md
specs/<project>.yaml
scripts/
projects/<project>/
artifacts/
codex-skills/kicad-production-pcb/
hooks/pre-final-pcb-flow.sh
```

路径、阈值、制造规则、命令和项目名称必须放在 Spec 或 policy 中，不要在共享门禁脚本中硬编码具体板卡数据。

## 5. 在 Codex 中启动

新项目：

```text
使用 $kicad-production-pcb。我是小白，只从需求收集阶段开始。我的使用场景是：……
```

已有项目：

```text
使用 $kicad-production-pcb，检查当前 Spec 和证据，报告当前阶段，只从第一个失败门禁继续。
```

需求确认、模块架构、物料选择和 Spec Freeze 没有闭环前，Codex 不得进入 KiCad 生成。

## 6. 更新

```bash
git pull --ff-only
./install.sh --force
./install.sh --project /你的/项目路径 --force
```

每次更新后都要重新运行 golden benchmark 和目标项目 strict 流程。

## 7. 嘉立创网页 DFM

浏览器 MCP 已经登录时可以直接继续。登录、验证码、扫码、短信、2FA、账号选择、付款和提交订单必须由用户明确操作。导入的网页证据必须绑定当前发布文件的精确哈希指纹。
