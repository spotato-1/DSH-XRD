---
name: xrd-plot-export
description: 把已经精修完的工程**直接导出成两套 CSV**，交给第三方画图工具（Origin / Excel / Igor / matplotlib 自己写脚本）出 Rietveld 图——**本 skill 只导数据，不在对话里画图，也不驱动 Origin**。**存在理由（用户 2026-11-06 定）**：GSAS-II 各版本/各安装的**导出支持度不一样**，且**手动引擎兜底时根本没有 GSAS 工程可导、使用者拿不到实际数据**，所以要自带一个只读数组的导出器、且必须覆盖非 gpx 来源。两套即：① `<sample>_rietveld.csv` = 整条谱的 `2Theta,Iobs,Icalc,Iback,Idiff`（原始数据 / 拟合总谱 / 背景 / 残差）；② `<sample>_reflections.csv` = 峰位棒图数据 `Phase,h,k,l,Mult,d_spacing,2Theta,Icorr`。附带 `_stats_summary.csv`（一行一个 histogram 的 Rwp/Rp/Rb/Rexp/gof/点数）。支持三种来源（GSAS-II `.gpx`、手动引擎 `components.json`、非晶 hump 工作区）、**两相按相拆分成每相一份**、非晶按带分列（`Band1..N`）、同一样品不同输入分开存放。含列语义与精度、必做校验、以及踩过的 7 个坑。触发词：导出CSV、画图数据、两套CSV、Origin画图、第三方画图、Rietveld图数据、Iobs Icalc、残差数据、ref位置、reflections、峰位棒图、峰位刻度、按相拆分、非晶分带、stats汇总、把精修结果导成表格、手动引擎导数据、GSAS导不出来。
whenToUse: 用户要把精修结果交给 Origin/Excel/自己写脚本画图，或明确要"CSV/表格/数据文件"而不是要一张图时；也用于批量重新导出整套样品的绘图数据。
---

# XRD 绘图数据导出（两套 CSV）

## 0. 这个技能为什么存在（先读，决定你该用哪条来源）

用户 2026-11-06 的原话：

> "这个技能本质上其实是因为 gsas-II 有各种版本导出支持度不一样，此外还存在手动引擎兜底的情况
> 这时使用者无法导出实际数据，故增加了这个技能。"

两条根因，直接决定实现方式：

1. **GSAS-II 的导出能力随版本/安装而变** —— 同一套菜单/脚本在别的机器上不一定有、不一定一样。
   所以**不能把"让 GSAS-II 自己导出"当成方案**，本技能自带导出器，只从工程里**读数组**，不依赖任何
   GUI 导出功能、也不写回工程。
2. **手动引擎兜底时压根没有 GSAS 工程** —— 精修是在 `xrd_manual` 的 py 引擎里跑完的，GSAS 那边
   没有 `.gpx`，用户**拿不到实际数据**。所以三种来源里，`components.json` 与非晶 hump 工作区
   **不是"优先级更低的兜底"，而是这个技能存在的理由**：结果在哪，就从哪读。

早先那次"让 agent 直接操纵 Origin 出图被叫停"只是触发场景；**真正的理由是不让数据的可获得性
取决于某个软件的版本**。所以：**只要结果里有数组，就必须能导出成表**。

## 1. 什么时候用
- 用户要"能直接画图的数据"，或要把一批样品交给外部绘图工具。
- 已经有**收敛的**精修结果（`.gpx` / 引擎 `components.json` / 非晶 hump 拟合），只需要把数组落成表。
- 需要**重出一批**（例如换了样品集合、要按相拆分、要加 stats 汇总）。

不适用：还在精修中、结果未收敛（先把 R 值洼地走完再导）；用户其实想要一张现成的图（那是出图，不是导出）。

## 2. 脚本与命令

脚本就在本 skill 同目录：`scripts/plot_export.py`（**用全局 python**，不要在工作区建局部环境；
GSAS-II 根部解析顺序：`DSH_GSAS_ROOT` → 插件根的 `xrd.local.json` 里 `gsas_root` → 该解释器已能
`import GSASII`；三者都没有就报错并提示设 `DSH_GSAS_ROOT`，**脚本里不写死任何机器路径**。
**只有 gpx 来源才需要 GSAS-II 装得上**，另外两条来源不 import GSAS，机器上没装也能导）。

**先问一句"结果到底存在哪个目录"再选命令**，不是按偏好排序：

| 精修是怎么跑的 | 结果在哪 | 用哪条 |
|---|---|---|
| GSAS-II 阶梯（`xrd_refine_gsas` / `multiphase` / 人工 GSAS 工程） | `<…>.gpx` | ① 或 ② |
| **手动引擎兜底（`xrd_refine_manual`）** | `result_*.json` + `components.json`（同目录） | ③ |
| 非晶 hump 拟合 | `work/hump_model.json` + `work/W_twocol.dat` | ④ |

```bash
# ① 单个 gpx
python scripts/plot_export.py --src  <sample>/GPX/<sample>_final.gpx --out <导出目录> [--split-phases]
# ② 一整个目录（每种样品一个子目录、下含 GPX/ 的布局会按样品名命名）
python scripts/plot_export.py --src <报告根> --out <导出目录> --recursive [--split-phases]
# ③ 手动引擎结果（传 result.json 会自动去找同目录 components.json）
python scripts/plot_export.py --components <.../components.json> --out <导出目录> --name <样品名>
# ④ 非晶 hump 拟合工作区（hump_model.json + W_twocol.dat）
python scripts/plot_export.py --amorphous-work <.../Amorphous/work> --out <导出目录> --name Amorphous
```

命名规则：源路径形如 `<…>/<样品>/GPX/<样品>_final.gpx` 时，**样品名取祖父目录名**（分支都一样），
所以单文件与整目录导出得到的文件名一致，重导不会产生 `<样品>_final_…` 这种错名。

⚠ **不要因为"没有 gpx"就告诉用户导不出来**——那正是本技能要解决的情形：手动引擎跑完就有
`components.json`，直接走 ③。反过来，**gpx 打不开时先报告 GSAS-II 版本/安装情况，再退到 ③④**，
不要让"GSAS 导出不了"变成"数据拿不到"。

## 3. 两套 CSV 的契约（改之前先读完这段）

### 3.1 `<sample>_rietveld.csv` — 整条谱
| 列 | 含义 | 精度 |
|---|---|---|
| `2Theta` | 角度 | **5 位小数，三条来源分支统一**（实验室 0.02° 步长够；同步辐射 0.01° 以下也不发毛刺） |
| `Iobs` | **原始观测**强度（不是扣过背景的） | 2 位 |
| `Icalc` | **拟合总谱**（含背景） | 2 位 |
| `Iback` | 背景曲线 | 2 位 |
| `Idiff` | **残差 = Iobs − Icalc**（不是 Iobs − Iback） | 2 位 |
| `Band1..N` | **仅非晶**：`背景 + 该带单独`，见 §5 | 2 位 |

多 histogram 的工程：每个 histogram 一份，文件名加 `_<hist名>` 后缀，stats 用**同一个带后缀的名字**。

### 3.2 `<sample>_reflections.csv` — 峰位棒图
单相 7 列：`Phase,h,k,l,Mult,d_spacing,2Theta,Icorr`（`Phase` 在首列，值就是 GSAS 里的相名）。
- `d_spacing` 6 位、`2Theta` 5 位、`Icorr` 3 位。
- **`Icorr` 直接取 GSAS-II 的 `Icorr`**（已做多重性/LP 校正），它是棒高的依据；**绝不要自己用 Iobs 反推**。

两相及以上：默认仍是一份文件，用 `Phase` 列区分。**每个相要单独一组**时加 `--split-phases`，
额外产出 `<sample>_reflections_<相名>.csv`，列是 `h,k,l,Mult,d_spacing,2Theta,Icorr`
（**去掉 `Phase` 列，保留 hkl**——没有 hkl 的峰位表无法与任何东西对账）。
相名里的空格/点/斜杠会被换成 `_`（`第二相 杂质` → `第二相_杂质`），否则文件名会坏。
> 用户原话："双相材料为什么只要一个 ref，不是应该每一个不同相都有一组嘛" → 默认给合并表**同时**给按相拆分，
> 让每相能单独上色、单独一行刻度。
> ⚠ **历史口径不统一**：最初那批拆分文件是两次一次性脚本产出的，同一批里既有 7 列也有 3 列的文件，
> 相名还被手工映射成过 `Fe2O3`/`Fe3O4`。本技能统一为**7 列 + GSAS 原相名**，所以**重导的拆分文件不会与那批
> 3 列文件逐字节相同**（合并文件仍然相同）。要友好相名就自己改名，别让脚本猜。

### 3.3 `_stats_summary.csv` — 汇总
`Sample,Rwp_wR_pct,Rp_pct,Rb_pct,Rexp_wRmin_pct,gof,n_observations`，**一行一个 histogram**。
`gof = wR / wRmin`。重导**同名样品会替换该行**而不是追加（脚本已实现），所以反复导不会出现重复行。
非晶另有 `<sample>_stats.csv`（`metric,value`，且**必须写明这不是 Rietveld、Rwp 不可与晶体样品横比**）。

## 4. 两相 / 多相
- 先看 `Phase` 列有几个值；两相样品若两相的棒图混在一份里，用户无法分别上色 → 加 `--split-phases`。
- 拆分文件的**行数与合并文件里该相的行数一致**，可用来互验。
- 代理结构（例如用 R-3m 代理 P2 相）在相名里**照实写**，不要改成"看起来更对"的名字；口径问题写进交付说明。

## 5. 非晶（不是 Rietveld）
- `Band<i>` = `背景 + 第 i 个伪 Voigt 分量`（**叠起来正好等于 Icalc**）。这样 stacked 图可以直接画，
  不要只输出裸分量，否则叠图与拟合对不上。
- 另出 `<sample>_components.csv`：`Band,center_2th,FWHM,eta,amp,area,d_A,Lc_nm`。
- 非晶宽包**不能**当布拉格峰，也不套谢乐公式；`Lc_nm` 只能是**表观相干长度**，脚本会把这条限定
  写进 `<sample>_stats.csv` 的 `NOTE` 行（"APPARENT … not a crystallite size … no strain separation"），
  不要只写在报告里。
- 非晶的 Rwp 与晶体样品的 wR **不可横比**，交付说明里必须写。

## 6. 分批与命名（用户提过两次）
- "每个数据导出一个 CSV" → 一样品一套，不要把所有样品塞进一个文件。
- "与先前的相同测试数据分开标注" → 不同来源的批（例如工作区早期结果 vs 标准测试集结果）**放不同子目录**，
  `_stats_summary.csv` 各归各的，不合并成一张大表。
- "均为同一样品，但是不同输入" → 同一样品的不同输入/不同引擎结果，用**带后缀的样品名**区分（`<sample>_engine2` 等），
  不要覆盖同名文件。

## 7. 必做校验（做完才能交付）
1. `n_observations` 与 `_rietveld.csv` 数据行数相等（`行数 − 1`）；
2. 抽看首行/末行：`2Theta` 单调、`Iobs` 是原始计数（不是 0/1 归一化）、`Idiff = Iobs − Icalc` 抽查两三行能对上；
3. `reflections` 的 `2Theta` 随 `d_spacing` 单调反向；
4. `_stats_summary.csv` 的 wR 与工程/报告里的一致（**数字必须实测，不许凭记忆填**）；
5. 若同一批曾导过：把新文件与旧文件**归一化行尾后逐字节比对**（脚本输出只有 `\n`，早期批次是 `\r\n`，
   内容应完全相同）。比对通过是"重导没走偏"的最强证据。

## 8. 踩过的坑
1. **大数据点必须走 CSV**：同步辐射一份谱 4.5 万点，走 JSON/工具返回值会被体积和超时卡死，落地成文件就没问题。
2. **GSAS 的数组是掩码数组**：直接格式化会得到 `--` 或抛异常，必须先把掩码位置换成 NaN、再把非有限值写成**空单元格**
   （写 `nan`/`inf` 文字，绘图软件会当成解析错误）。
3. **多 histogram 不加后缀会互相覆盖**：一个工程里有多个 histogram 时按历史名后缀区分。
4. **`.gpx` 只读**：导出绝不修改工程文件（不做 refinement、不写回参数）。
5. **手动引擎的键名不统一**：`ycalc`/`yc`、`bg`/`ybkg` 都出现过，脚本已兼容；自己写脚本时要兼容。
6. **路径含中文没问题，stdout 编码会花**（`DSH-XRD绘图` 在控制台里可能显示成乱码），这不影响落盘，
   不要因为控制台乱码就怀疑文件坏了——用读文件的方式核对。
7. `2Theta` 只写 3 位小数会把 0.01° 步长**量化成阶梯**，看着像数据坏了；实验室数据也建议 5 位。

## 9. 交付说明要写什么
每个样品一行：样品名 → 用了哪个工程/来源 → 相数与相名 → 点数 → wR（以及它属于哪个口径）→ 产出文件名。
若某一批是**扫描点中最优的那个**（例如引擎多档扫描取最小 Rwp），必须写明"这是扫描点 X，不是最终交付结果"。
