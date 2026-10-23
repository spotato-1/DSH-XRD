---
name: xrd-multiphase
description: ≥3 相 XRD 精修的编排工作流(阶段 4 的 GSAS-II 腿,与两相分工明确)。**硬门控:相数 ≥3 才用它**(1–2 相走 `xrd_refine_gsas`,工具层与引擎内双重拒绝)。**设计顺序(用户 2026-10-02 定):先用 `xrd_refine_manual` 把最多量的两个相配好,再用 `handoff` 把相比例/主相晶胞/仪器参数交接进 GSAS-II**——不带 handoff 的新建工程会被拒绝。工具 `xrd_refine_multiphase` 是**第二入口/AI 可编辑代码钩子**:`init_engine:true` 取模板 → 改标了 `# == EDITABLE ==` 的 `build_steps()` 编排段(固定阶梯表达不了的编排:某些参数只给某一相、两相共享 profile、中途加相) → 传 `engine:"<副本>"` 执行。含逐相开关 `phases[]`、`entry="hold"` 逐步放开、失败步/变差步自动回滚、交付最佳接受态、`ladder`/`gsas_error`/`handoff.warnings` 判读、以及"第二相太弱会被吞掉"的实测判据。触发词:多相精修、三相、四相、多相 Rietveld、xrd_refine_multiphase、AI 可编辑钩子、engine 副本、build_steps、逐相编排、第二相、相分数、handoff、交接、混合相、杂质相、多相样品。
whenToUse: 样品含 3 相及以上(含杂质相)需要在 GSAS-II 里做多相精修编排时;或用户要求"多相钩子/自定义多相步骤"
---

# 多相 XRD 精修（阶段 4 · GSAS-II 腿 · ≥3 相）

> **分工**：本文件只讲"**多相怎么编排**"；工具参数与坑 → `xrd-tools` §5；两相(2 相)的完整流程 → `xrd-refinement` §4.5.3；
> 什么阶段做什么 → `xrd-workflow`；红线与极限模式 → `xrd-workflow` §6.5。

## 0. 硬门控：几个相走哪条路

| 相数 | 走哪条 | 说明 |
| :-- | :-- | :-- |
| 1 相 | `xrd_refine_gsas`（单相阶梯） | 主相精修，交付恒为 GSAS wR 口径 |
| **2 相** | **`xrd_refine_gsas` ＋ `handoff` ＋ `phases[]`** | 见 `xrd-refinement` §4.5.3；**不要**用多相工具 |
| **≥3 相** | **本文件：`xrd_refine_multiphase`** | 工具层相数 <3 直接拒绝；引擎内 `MIN_PHASES = 3` 是**运行期检查**，改副本也绕不过 |

**为什么必须是独立工具（用户 2026-10-02 裁定，原话）**："**单一工具功能过多AI会错误操作**" ——
固定阶梯的 `xrd_refine_gsas` 已经背了逐相开关 + entry/回滚 + handoff，再把"任意编排"塞进去只会让模型选错参数。
所以多相做成**新工具 + 可编辑代码钩子**（工具 11→12）。

## 1. 设计顺序：**先把最多量的两个相配好**（硬要求，用户定）

> 用户原话（2026-10-02）："**如果先使用手动引擎调配好双相再进入 gsas-II 呢，实际上设计顺序是这样的。**"
> 以及立此 skill 时的要求："**多相 skill，要求在导入 GSAS-II 时先把两个最多的相拿手动引擎修好。**"

**为什么**：GSAS-II 的阶梯**不会**帮你从零 bootstrap 一个多相解——相分数这类解此前**根本没有传递入口**，
GSAS 只能从 1:1 重新开始，把差值全部吸收进背景与峰形（实测：一次全放把两相工程从 wR 30.42% 打到 77.19%，
晶胞被拉成 7.29/6.70/13.52）。**真缺口是 Ⅱ→Ⅲ 的交接契约**，不是 GSAS 的能力。

**流程**：
1. **Ⅱ 上解**：`xrd_refine_manual` 带 `second:[{cif:...}, {cif:...}]`，把**最多量的两个相一起配到位**
   （有界 LM：相分数/峰形/晶胞都在界内）。**必须真的收敛** —— 看 `metrics.Rwp` 与 `fit_status.flags`；
   **某相被拟合成 ≈0 就是没配好**，回上一步重配，不要带着它进 GSAS。
2. **交接**：调 `xrd_refine_multiphase` 并传 `handoff:"<上一步 result.json>"`。
   引擎播种第二相相比例、主相晶胞、仪器 `U/V/W/X/Y/Zero`（**带单位换算**），
   并在 `handoff.transferred / not_transferred / warnings` 里**逐条说明传了什么、没传什么、为什么**。
   **不带 `handoff` 的新建工程会被拒绝**，理由就是这一节。
3. **两条防线（会挡下坏种子，不要绕过）**：
   - 某相被手动引擎拟合成 ≈0 → **告警**"该相在 GSAS 里将不可见"（照传不会报错，但那个"多相"就名存实亡）；
   - 仪器值与工程现有一致值差 **>10 倍 → 拒绝覆盖**（实测照传把 wR 从 25.5% 弄到 71.8%，等于种子毁掉标定值）。
4. **Ⅲ 收尾**：跑完照旧走 `xrd_gsas_ops`（约束/背景）→ `xrd_export_cif` / `xrd_finalize`。

## 2. 两步用法（取模板 → 改 → 跑）

```
① 取模板（不跑）：
   xrd_refine_multiphase { init_engine: true, out_dir: "<工作目录>" }
   → 返回 engine_template = "<out_dir>/gsas_multiphase_custom.py"

② 读它、改编排段（见 §3 契约），然后执行：
   xrd_refine_multiphase {
     engine: "<那份副本>",
     gpx: "<已有工程>"            // 或 data + instprm + cif + cif2 + cif3...
     handoff: "<手动引擎 result.json>",
     refine: ["background","inst","cell","scale","profile"],
     phases: [ {...}, {...}, {...} ],   // 逐相开关，下标＝相序
     entry: "hold", n_back: 10, inst: ["U","V","W","X","Y","Zero"],
     max_cycles: 8, uiso_floor: {enabled:true, min:0.005, max_rounds:4},
     out_dir: "<工作目录>"
   }
```

## 3. 模板契约（改了也必须守）

`engine/gsas_multiphase_template.py` 是**自包含**模板（不 import 同级模块，复制出去即可运行）：

- **`# == EDITABLE ==`** 标出的才是给你改的：**`build_steps(gpx, plan)`** 里的编排（加/减步骤、给某相单独开某一族、
  两相共享一个 profile、中途加相等），以及 `MIN_PHASES`（**别调小**，它是硬门控）。
- **不许动**：plan JSON 进 / result JSON 出的 I/O 协议与结果字段名（`ok/phases_count/histograms/phases/ladder/handoff/warning`），
  工具层要按这些名字解析；**门控判断本身也不要删**（改副本绕门控是自欺，且会让 1–2 相误入多相路径）。
- 引擎是**按次调用**的（不需要重启宿主），但**副本放在工作目录、属宿主"外来脚本"⇒ 调用时可能要求执行批准**，这是设计如此。

## 4. 判读与红线

- **`ladder[]` 是主判据**：`step/label/wR_before/wR_after/accepted/rolled_back`，另有
  `gsas_error`（**GSAS 自己报的失败**，如 `Invalid cell metric tensor for phase #1`）
  与 `cell_change_pct`（该步里动了 >2% 的晶胞参数）。
  **失败步一律 reject＋回滚、绝不成为 best**；`warning` 只收"交付态被污染"（失败步被保留／回滚失败／best 态恢复失败）。
- **交付的是"最佳接受态"**，不是最后一步；`entry="hold"` 下**若一步都没被接受**，`summary` 会明写"交付工程即来态"
  —— 别把 `ok:true` 读成"请求的族已精修"。
- **失败步 ≠ 该重试**：`gsas_error` 说明**该族在这份数据上不可测**（典型：微量相的晶胞/微应变），
  如实写进报告；反复用同一配方重试是无效劳动（`xrd-workflow` §6 的失败四分类与极限模式在那里）。
- **第二相太弱的多相样品不要硬做**（用户 2026-10-02 裁决，实测依据）：S3（CuCr2O4+CuO，7 K 帧）里 CuO 的
  积分强度只有主相的约 **1%**（sumInt 1230 vs 82811），相分数会被精修吞掉、峰形/晶胞本质不可测 ——
  我方最好 **22.19%** vs 官方 **13.859%**。**先看各相强度占比与独立反射数**，弱到会被吞掉就如实判"该样品不适用"。
- 其余红线（R 值口径不可跨引擎比较、非物理手段绝不进交付、极限数值只进"极限说明"一节、GSAS 输出必须过 `review` 块）
  与 `xrd-refinement` / `xrd-workflow` 完全一致，本文件不重复。

## 5. 常见错误

| 现象 | 原因 | 处置 |
| :-- | :-- | :-- |
| 工具报"相数 <3，请用 xrd_refine_gsas" | 真的只有 1–2 相 | 换 `xrd_refine_gsas`（＋`phases[]`/`handoff`） |
| 报"不带 handoff 的新建工程被拒绝" | 没先用手动引擎配两个最多的相 | 回 §1 第 1 步 |
| `handoff.warnings` 说某相被拟合成 ≈0 | 手动解里该相塌了 | 重配手动解；否则这个"多相"没有意义 |
| `handoff.warnings` 说仪器值被拒绝覆盖 | 手动峰形跑飞（差 >10 倍） | 重配手动峰形，或接受工程原有标定值 |
| `ladder` 里某相全部 rejected＋rolled_back | 该相的族在这份数据上不可测 | 如实报告，别再放开 |
