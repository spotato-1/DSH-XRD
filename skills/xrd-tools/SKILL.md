---
name: xrd-tools
description: dsh-xrd 全部 12 个 XRD 工具的参数契约、产物与已知坑（工具手册）。工作流 skill 只写"何时调用哪一步"，参数细节与排障查这里。覆盖 xrd_data_io（厂商 raw/两列/fxye/dat 互转 + **切峰有门控(confirm+reason)** + FWHM/Caglioti + **谢乐公式尺寸 scherrer(未扣仪器展宽=表观值、不做应变分离)** + FP 对齐）、xrd_cif_edit（读/文本级与结构级编辑/build/**build_orbit 按 Wyckoff 轨道专业化构建**/**validate 手写CIF体检**/fetch MP·COD/**polymorphs 多晶型**）、cif_to_pcr（官方转换器）、xrd_phase_screen（残余峰 → 候选相理论谱打分，**两级漏斗宽进**）、xrd_refine_auto（插件自带无头 fp2k 驱动，AutoFP 已移除）、xrd_refine_manual（py 引擎，**背景默认6阶、必须最后拟合、bg_scan 迭代导出逐阶曲线**；**判读包 baseline_rwp / fit_status.flags / r_plateau**）、xrd_refine_gsas（GSAS 阶梯，**逐相开关 phases[] / entry / rollback / handoff**）、**xrd_refine_multiphase（≥3 相 AI 可编辑代码钩子，硬门控≥3 相；1–2 相走 xrd_refine_gsas）**、xrd_gsas_ops（全参数/约束/换相派生/**import_background**/auto_background/**review 审查**）、xrd_verify_fp2k、xrd_export_cif（统一输出端口 **五源** + 科学判断）、xrd_finalize（交付包）。触发词：工具用法、参数、报错、排障、坑、xrd_data_io、谢乐公式、Scherrer、晶粒尺寸、相干长度、峰宽、仪器展宽、xrd_gsas_ops、xrd_export_cif、xrd_cif_edit、xrd_phase_screen、xrd_finalize、xrd_refine_manual、背景导入、背景阶数、切峰门控、多晶型、result 源、多相精修、三相、xrd_refine_multiphase、逐相开关、handoff、交接、回滚、rollback、entry。
whenToUse: 需要某个 xrd 工具的具体参数/产物/已知坑，或工具调用报错需要排障时
---

# dsh-xrd 工具手册（12 工具）

> **分工**：本文件只讲「怎么用」与「坑」；「何时用哪一步」见 `xrd-workflow` / `xrd-preflight` / `xrd-refinement`。
> 所有工具都返回结构化 JSON；**任何数值字段缺失时是"省略键"而不是 null**（这是宿主要求，不是 bug）。

## 0. 总表

| 工具 | 一句话 | 关键输入 | 主要产物 |
| :-- | :-- | :-- | :-- |
| `xrd_data_io` | 数据 I/O 与预处理 | `actions[]`（head/read/convert/cut/fwhm/**scherrer**/align） | 两列 / `.fxye` / `.dat`、FWHM+Caglioti、谢乐尺寸 D |
| `xrd_cif_edit` | 读/改/生成/取数 CIF（关键拟合方法） | `actions[]`（read/edit_text/edit_structure/build/**build_orbit**/**validate**/symmetrize/fetch_cod/fetch_mp/**polymorphs**） | CIF 文件 + 结构表 + `validations[]` |
| `cif_to_pcr` | 官方 `CIFs_to_PCR.exe` 直转 | `cif`（+`out_dir`） | `.pcr` |
| `xrd_phase_screen` | 残余峰 → 候选相打分（定相） | `actions[]`（residuals/peaks/fetch/screen） | 排序表 + 残余峰表 |
| `xrd_refine_auto` | **无头 fp2k 驱动**（AutoFP 已移除；两个名字同一实现） | `pcr`, `ladder`, `cycles` | 精修后 pcr、`.out/.sum/.prf`、逐步 Rwp 轨迹（`ladder`） |
| `xrd_refine_manual` | py 引擎（有界 LM） | `data`, `cif`/`atoms`, `vary`, `occ`, `extras`, `second`, `coord_vary`, `bond_targets`, `bg_scan` | result.json（含 structure）+ components.json |
| `xrd_refine_gsas` | GSAS-II 阶梯（快速） | `gpx` 或 `data`+`instprm`+`cif`；`refine[]`；**`phases[]` 逐相开关**、`entry`、`rollback`、`handoff` | `.gpx` / `.lst` + `ladder[]`（含 `gsas_error`） |
| `xrd_refine_multiphase` | **≥3 相**精修（AI 可编辑钩子） | `gpx` 或 `data`+`instprm`+`cif`×N（**≥3 相，硬门控**）；`init_engine:true` 先取模板 | `.gpx` + `ladder[]` + 可编辑引擎副本 |
| `xrd_gsas_ops` | GSAS 全参数/约束/工程级操作 | `actions[]`（见 §5） | `.gpx` + 全参数表 + **review 审查块** |
| `xrd_verify_fp2k` | fp2k 交叉验证 | `pcr`（dat 同目录） | 密度/晶胞/Rwp/BraggR |
| `xrd_export_cif` | **统一输出端口** + 科学判断 | `gpx` / **`result`** / `components` / `data` / `cif`（五源） | 图 + GSAS 工程副本 + CIF + verdict |
| `xrd_finalize` | GSAS 交付包 | `gpx` | 干净 gpx + lst + CIF + 图 + report.md |

## 1. `xrd_data_io`（数据 I/O）

- **格式自动识别**：Rigaku RAW/txt（`;RAW4.00`/`[RawHeader]` → `[Data]` 段 `2theta, count`）、GSAS FXY（`BANK`，厘度自动还原）、任意两列文本（容错跳表头/注释）。
- actions：
  - `head` 嗅格式 → `read` 报点数/范围/步长 → `convert`（`to: twocol|fxye|dat`，`pad_lines` 用于 FullProf 固定跳 6 行注释）
  - `cut`：**数据级切峰**（GSAS 脚本 mask 无效，只能在这里切）；`auto_al:true` 用常见 Al 箔峰位（26.2–26.9 / 38.3–38.8 / 44.5–45.0 / 64.8–65.6 / 77.9–78.7）
  - **⛔ `cut` 有门控（用户 2026-09-18）**：切峰真实删除数据点，所以**必须在对话中被用户明确要求 + 声明用途**：
    调用要带 `confirm:true` 与 `reason:"<用途>"`，否则**工具直接拒绝**。允许的用途只有
    ①极片的 Al 箔/集流体峰、②被无关伪影主导的区段；**禁止预防性清理**。`reason` 会记在返回里。
  - `fwhm`：给窗口列表测半高宽；**≥3 个峰时同时解 Caglioti U/V/W**（可作为峰形初值）
  - **`scherrer`（谢乐公式，用户 2026-09-21 要求加入）**：`{type:"scherrer",file,peaks:[[lo,hi],...],
    wavelength:1.54056,K:0.9,inst_broadening:<度>,caglioti:{U,V,W},inst_mode:"gaussian|lorentzian"}`
    → 每窗口给 `D_A`/`D_nm` + `d_spacing` + `beta_rad` + **`assumptions` 假设回声块**。
    口径要点（**转述数字时必须一起转述**）：
    - **不扣仪器展宽 ⇒ `corrected:false`，那是"表观值"**。扣法：给 `inst_broadening`（度）或
      `caglioti:{U,V,W}`（可与上一条 `fwhm` 的解串起来用）；`gaussian`= √(β_obs²−β_inst²)、
      `lorentzian`= β_obs−β_inst；**过扣会报 `over_subtracted`**（而不是静默给 0）。
    - **D 是相干长度，不必然是晶粒尺寸**：需要无应变、单分散、纯尺寸展宽才成立。
      **微应变分离（Williamson-Hall / size-strain）不做** —— 这是**有意留的边界**，要应变走 GSAS-II 的 size/strain。
    - **Kα2 不剥离**（未分辨双线会让 FWHM 偏大、D 偏小），λ 与 K 都会在返回里回声。
    - 对**非晶宽包**，D 只是那个宽包的**表观相干长度**，不是晶粒尺寸 —— 报告里不许含糊。
    - 自带边界：本引擎只做尺寸，不做**择优取向 / 仪器轮廓反卷积**。
  - `align`：写 FullProf `.dat`（fp2k 固定跳 6 行注释 → 数据要对齐）
  - **`header`（位置式导头，新增）**：把**数据开始之前的全部原文**连行号一起吐出来（附 `data_start_line`）。
    边界＝**首段连续 ≥3 行、每行 ≥2 列数值**；列名行（如 RAW 的 `Angle, PSD,`）不算数据 ⇒ 被正确留在头里。
    实测 Rigaku RAW：**141 行头 / 数据自第 142 行**；纯文本头 1 行；纯数据 0 行。
    **它是原始证据口，不做任何解释** —— 尤其**不在这里判 λ、不猜靶材**（判读规则见 xrd-preflight）。
  - **`evidence`（多文件证据）**：一次抱回多个文件的头/统计，用于"哪个文件是哪台机器测的"这类对账。
- 坑：Rigaku 的 `[Data]` 在百行之后，格式嗅探窗口要宽（已内置 250 行）。
- 坑：文件是 **UTF-8** 中文表头时，本工具与引擎都按 UTF-8 读（`errors='replace'`）—— 曾因平台默认
  GBK 崩在 `UnicodeDecodeError: 'gbk' codec can't decode byte 0x80`（2026-10-03 修复）。

## 2. `xrd_cif_edit`（关键拟合方法）

- actions：`read`（结构表）/ `edit_text`（**保留原对称块**的行级编辑）/ `edit_structure`（pymatgen 重对称化）/ `build`（从空间群+晶胞+位点造 CIF）/ **`build_orbit`** / **`validate`** / `symmetrize` / `fetch_cod` / `fetch_mp` / **`polymorphs`**。
- **★ `build_orbit {spacegroup, cell, sites:[{element,x,y,z,occ}], symprec}`（库/文献都查不到时的专业化构建）**：
  每个位点只给**一个 Wyckoff 代表点**，由空间群自身轨道展开其余等价位置，输出**非对称单元式 CIF**
  （代表点 + 完整对称操作块 + 引号包裹的空间群名）。返回 `plan[]`，每个位点带
  **`wyckoff_letter` / `multiplicity` / `on_special_position`** —— 拿它与文献对照即可发现抄错。
  实测（某立方倍半氧化物，只给 8b / 24d / 48e 三个代表点）：建出 80 原子胞、Wyckoff 标注正确、
  `cif_to_pcr` 的占位审计 pass，**结构数值等价于已知良好精修（Rwp 7.00% vs 参考 7.04%）**。
  空间群符号各种拼法都能解析（`R -3 m` / `R-3m` / `P21/c`，由 gemmi 解析后用 pymatgen 展开轨道）。
- **★ `validate {cif, declared_spacegroup, contact_limit}`（手写 CIF 送精修前的体检）**：
  在**原始文本层**核对（不依赖 CIF 解析——解析器会用声明的对称性**静默展开**位点，从而掩盖错误）：
  逐位点轨道大小、展开胞原子数（与声明群生成数对比）、**展开后最小原子间距**。返回
  `verdict: pass|warn` + `per_site[]` + `checks{expanded_cell{min_contact_A,...}}`。
  典型坏法：特殊位置坐标写偏 → 解析器**默默造出多余原子**（实测 6c 写成 x=0.04 → 39 个原子、
  **180 对 <1 Å 重叠、最近 0.115 Å**），`warn` 即此信号。
  **边界**：只保证"文件与自身声明的对称性自洽"，**不判断化学正确性**（层间距写错的文件一样自洽），
  结构对错只能靠 R 值裁决。
- 引擎脚本 `engine/cif_edit.py` 是**固定工具但可 AI 编辑**（与手动引擎同思路）：材料特殊时直接改这两个
  动作扩展，别另写一次性脚本；保持 plan-JSON 进 / result-JSON 出的协议。
- **★ `polymorphs {formula|chemsys, max, per_polymorph}`（高缺陷/掺杂晶体必用）**：
  按 **(化学式, 空间群)** 分组，每组返回**最稳定的一个代表 CIF** —— 即"多种不同晶型"而不是四十个重复。
  实测 LiFePO4 从 70 个候选分出 **26 种结构类型**（含橄榄石 `Pnma`）。返回每组的
  `{id, space_group, formula, path, n_in_group}`。**这一步是用户要求的必须项**（"这个是要确定的"）；
  候选不做理论打分裁定，交给阶段 4 的 `xrd_refine_auto`（fp2k）实跑 R 值裁决。
- 改动键（edit_* 共用）：`replace_element` / `rename_sites` / `set_occ` / `set_site_composition` / `set_coord` / `set_cell` / `drop_sites` / `add_sites`。
- **不做任何校验**：化学上荒谬的候选也要能写出来，交给拟合判定。
- 坑：
  - **坐标精度会改变 Wyckoff 判定**：`0.3333` → 12 个位点（6h），精确 `1/3` → 4 个（4f）。build 时给足精度。
  - `fetch_cod` 的 CIF 下载地址是 `/cod/<id>.cif`（旧分层形式已 404）。
  - `fetch_mp` 需要 Materials Project 凭据：环境变量 `MP_API_KEY`，或 `~/.pmgrc.yaml`（见 README「API 配置」）。

## 3. `cif_to_pcr`

- **有 CIF 就一律走它**，不要手写/拼装 pcr 结构段（用户规则）。
- **⚠ 输出不能盲目信任 —— 占位必须看审计**（2026-09-18 实测推翻旧说法"输出即信任、不做 occ 归一"）：
  官方 `CIFs_to_PCR.exe` **不保证保留 CIF 的位点占位**。实测 `demo_phase.cif`
  （CIF: Mn 0.62 / Ni 0.33 / Mo 0.05 / Na 0.203、0.2058 / O 1.0）转出的 pcr 是
  **Mn 0.05167 / Ni 0.0275 / Mo 0.00417 / Na 0.01692、0.01715 / O 0.16667** ——
  **阳离子 ÷12、O ÷6**，晶胞严重欠占据 → 计算强度≈0 → AutoFP 报 **Rp/Rwp ≈ 100%**，
  而转换本身"成功"、不报错。
  **工具现在每次转换后自动审计**：返回值的 `occupancy_check` 给逐位点
  `cif_occ / pcr_occ / factor` 与 `verdict`（pass ≤2% / warn ≤6% / fail）。
  **`verdict=fail` 时不要拿这个 pcr 去精修**，先对账占位（改 CIF 重转，或改用手动引擎）。
- 坑：exe 把 `.pcr` 写在**输入 CIF 所在目录**，所以工具会先把 CIF 复制到 `out_dir` 再转换 → pcr 落在 `out_dir`。
  stderr 的 `forrtl: severe (24) ... CONIN$` 是它等 Enter 的无害产物。
- **每次调用约 110 s 且必以交互式 "Press <enter> to finish" 结束**（实测 4 次全部 110.5–110.6 s）；
  **附带预检**：缺空间群/缺原子/缺晶胞的 CIF 会被快速拒绝（否则要白等满 110 s 才报
  `No Space Group/No Symmetry information`）；**空间群名未加引号**也会告警 —— 无引号的
  `I a -3` 会在第一个空格被截断成 `I`（＝ITA 2 号空间群 P-1），**占位会整片算错**。
- **转换器的两处输出缺陷已由 `xrd_refine_auto` 的驱动自动修正**（相位名含点号、背景面板为
  Chebyshev），所以「转换成功」现在确实可以接着精修了。
- **λ 写入（新增，默认关闭）**：`wavelength`（单值＝单色 Kα1）或 `wavelength_doublet:[k1,k2,ratio]`。
  给了就在转换后改写 pcr 的 `! Lambda1 …` **值行**（其余字段原样），**回显 改前→改后**；
  不传 ⇒ pcr 与原来**逐字节相同**。单色写 `0.000000 0.00000` 进 Lam2/Ratio 槽（FullProf 认作无 Kα2）。

## 4. `xrd_phase_screen`（定相）

- actions：`residuals`（从收敛 gpx 提 obs−calc 残余峰）/ `peaks`（原始谱拾峰）/ `fetch`（MP·COD 取候选）/ `screen`（候选 CIF 理论谱打分）。
- 打分：pymatgen 每条理论峰（rel ≥ `min_rel`）匹配 `tol` 内的残余峰，贡献 `rel × (残余高/最大残余)`。
- **判读**：排名是**假设证据**，不是结论 —— 理论峰匹配好 **≠** 两相精修能收敛（实测最佳候选正是发散那个）。
- **★ 两级漏斗（用户 2026-09-18）**：本工具负责**宽进**（打分筛候选）；**严出**由
  **fp2k 实跑**完成（候选逐个 `cif_to_pcr` → `xrd_refine_auto` → 比 R 值）。打分只能筛，**裁决必须实跑**。

## 5. 精修引擎

### ★ λ（波长）契约 —— 四个写入入口（2026-10-03 新增，**全部默认关闭**）

- **两个入参，四处同名**：`wavelength`（数字，**单色** Kα1，Å）或 `wavelength_doublet:[kα1,kα2,ratio]`
  （**双线**）。出现在：`xrd_refine_manual`、`cif_to_pcr`、`xrd_refine_auto`、`xrd_refine_gsas`。
- **默认关闭是红线**：**不传 ⇒ 现有输入逐字节不动**（pcr / gpx / plan 全都不写 λ，走各阶段自己的默认值）。
  传了才写，且**必须回显 改前→改后**（pcr 值行、gpx 的 `Lam`/`Lam1`/`Lam2` 槽位、引擎日志 `wavelength: …`）。
- **判源归 AI，工具不猜**：工具**从不**推断 λ、不猜靶材、不做优先级仲裁 —— 这些在对话里由 AI 定，
  定完**显式传值**。`header` / `evidence` 只给原文证据，不给结论。
- **单色 vs 双线不是"选择题"**：**实验室 X 光管 ⇒ 双线**（有 Kα2），**同步辐射 ⇒ 单线**（无 Kα2）。
  只有数据与用户输入**都没说**时才问，且要声明"不问就用引擎默认"。
- **查表**（`engine/wavelength.py`，值为 GSAS-II 同源：Cu 1.54051/1.54433、Mo 0.70926/0.713543、
  Co 1.78892/1.79278、Fe 1.93597/1.93991、Cr 2.28962/2.29351、Ag 0.559363/0.563775…）；
  **表外值（如同步辐射）由用户给，填进去必须注明"表外假设"**。
- **各阶段不必口径一致**：允许 A 阶段用引擎默认、B 阶段显式给 —— 但每次都要留下事实记录（见 xrd-workflow）。
- ⚠ 用 `xrd_gsas_ops` 的 `set_hist {section:'Instrument Parameters'}` **设不了 λ**（它只设精修标志
  `instrument[p][2]=True`，值在 `[1]`）；要改 λ 用 `xrd_refine_gsas` 的 `wavelength`。

### `xrd_refine_auto`（插件自带无头 fp2k 驱动）★
- **2026-09-20 起不再走 AutoFP**：`engine/fp2k_driver.py` 直接驱动 FullProf 的 `fp2k.exe`
  （plan JSON 进 / result JSON 出，与 `gsas_engine.py` 同构）。**AutoFP 的 GUI 与包装层已从插件移除**，
  只从 FullProf 安装取 `fp2k.exe` 一个文件（`$DSH_FP2K_ROOT` / `fp2k_root`（xrd.local.json））。
- **为什么换**：AutoFP 的 pcr 解析器（diffpy/pyfullprof）读不了官方 CIF 转换器的产出，会在
  `pcrfilereader.py:749` 崩，让工具误报 "did not report Rwp"——而 `fp2k.exe` 单跑 0.3 s 就能读。
  **旧名 `autofp_refine` 已彻底移除**（0.3.0 接口清理）：AutoFP 的结构在插件里不再有任何残留，只保留 `xrd_refine_auto` 一个名字。
- **精修策略 = AutoFP 源码的移植**（不是另发明）：参数顺序
  `scale → zero → simple background → cell a,b,c → W → complex background → V,U → Asym → Y,X
   → Atom x y z → Pref,Bov → Biso-Atom → GausS,1G → Occ-Atom → …`；
  规则 = 一次放开一组 → 跑 → **Rwp 改善才留下，否则回滚**；目标 `MIN=Rwp`；每组内可分多级累加。
- **参数**：`pcr`（**强度数据文件必须与 pcr 主名一致、同目录**——见下方数据配对条）、`cycles`（默认 20）、
  `ladder`（默认 true＝跑完整阶梯；false＝只跑一次基线）。
- **λ**：`wavelength` / `wavelength_doublet`（见上方 λ 契约）。给了就**在跑前**改写 λ 值行并回显；
  **注意改的是驱动自己的工作副本**（`.fp2k-work-*` 里 fp2k 真正精修的那个 pcr），**调用方传进来的 pcr 不动**；
  不传则完全不碰。结果 JSON 里回读在 `wavelength` 字段。
- **★ 数据配对（实测红线，2026-09-21 更正）**：fp2k **按 pcr 的主名**（FullProf 的 job code）
  去找强度文件——**`! Files => DAT-file: …` / `!File names of data(patterns) files` 那一行是注释，
  不生效**。实测对照：正文声明 `Y2O3.dat` 且该文件**就在同目录**，只把 pcr 改名成 `probe.pcr` →
  报 `Error opening file CODEFIL.DAT…with CODEFIL=probe`、**0 条 R 值行**；原名 `Y2O3.pcr` → 正常收敛 81.1。
  ⇒ **数据文件必须与 pcr 主名一致**；**不要**靠改写那行注释去修配对（不起作用）。
  配对不上时 fp2k **只打 banner 就静默停**，容易被误判成"结构错"。
  *（旧口径"fp2k 读正文声明的数据文件名"来自一次**被同目录野文件污染**的实验，已作废。）*
- **返回**：`ok`、`rwp`、`rwp_mean`、`refined_pcr`、`out_files`、**`ladder`（每组被接受的步骤及 Rwp = 判读包）**、
  `cycles`、`error?`、`log_tail`。**Rwp 直接正则取自 `.out`**，不依赖 `par.txt`。
- ⚠ **两个 R 值，两种用途（0.5.1 起，必须分清）**：
  - **`rwp_mean` = 决策值**。是三次重复的**均值**，用来**在候选 CIF 之间选**、以及判断**是否够格直接进 GSAS-II**。
    它属于"那一组运行"，不属于任何单个文件。
  - **`rwp` = 流转值**。属于**交付出去的那份 pcr**：是那一轮从该状态测出来的数。
    进 GSAS 之前整条流程是 **R 值驱动**的，所以**进入下一步的数字必须属于进入下一步的文件** —— 就是它。
  - **`refined_pcr` 是"起点状态"**：该状态精修后得到 `rwp` 那个数。**不是** fp2k 写回后的那份。
    fp2k 报的是**它评估过的**参数集的 Rwp，而它留在 pcr 里的是**下一次更新**（尚未评估）——
    所以对写回文件再精修**得不到**报出的那个数（实测：起点 → 16.0，写回 → 17.5，同一份数据）。
    写回那份**作为带标签的备份**留在产物目录（`*_end.pcr`，含已精修的参数值），**不进流程**。
  - **交付不再"恢复 best 状态并复测"**：那等于要求 fp2k 从已被精修过的状态收敛回同一个解，它做不到。
    （**"快照"一词在本插件有三种含义，别混**：①§1.2 的**回档快照** `snapshot`（`.gpx`）；
    ②这里说的 **best 状态文件**；③"Rwp 是**瞬态值**"那种被边界框住的值。）
    现在每一步都把**起点状态**缓存下来（`*_start.pcr`），交付时**直接拷贝**，报告与文件按构造同源。
- **驱动顺带修掉转换器输出的两处致命缺陷**（否则 fp2k 打不开文件）：
  ①**相位名**：转换器把 CIF 文件名连点号写进去（`Y2O3.cif`）⇒ fp2k 报
   `FindFMT: invalid character in a numeric field` 并中断 ⇒ 去扩展名；
  ②**背景面板**：Chebyshev（24 系数、8 行×6）的系数**对精修 code 不可见** ⇒ 改写为多项式，
   并校正 Job 行的 `Npr`/`Nba`（**紧凑 Job 行不能按列名映射**，否则误改 `Nex` ⇒ fp2k 静默停）。
- **旧红线作废**：~~禁用 complex background 组~~、~~回写 pcr 列粘连~~ —— 那些源于 diffpy 层，
  现在不经过它；背景改为**分级放开**（先 1-2 项 → W → 其余项）。

### `xrd_refine_manual`（py 引擎，有界 LM）
- `vary` 列参数名；**背景项自动扩展**：只要 `vary` 里出现任一 `bgN`，`bg0..bg(n_cheb-1)` 全部进精修（避免"提高 n_cheb 却白跑"）。
- `occ`：`{位点标签: {value,min,max,vary}}` 占位乘子（天然 0–1，GSAS 会爆到 1.45 而它不会）。
- `extras`：经验宽峰 `[{center,H,amp,eta}]`；`second`：第二相 `[{cif,k,scale,Y,k_min,k_max,Y_min,Y_max,vary_*}]`。
- `coord_vary`：`{"N1":["z"]}` 精修选定坐标（**默认关**，低分辨数据上会漂）；`bond_targets`：键长**软约束**（作为额外残差并入 LS，`Rwp` 口径不被污染）。
- **λ**：`wavelength`（单色，走单线公式）或 `wavelength_doublet:[k1,k2,ratio]`（Kα1/Kα2 双线加权）。
  **不传＝引擎默认 Cu 双线**（`1.54056/1.54439/0.497`），且日志标 `[engine default]`；传了标 `[explicit]`。
  单色时日志为 `wavelength: kalpha1=… (monochromatic) [explicit]`。**同一数据换成单色算 Rwp 会变差是正常的**
  （谱里含 Kα2 却按单线建模）——实测 Y₂O₃：默认 41.53 % / 显式单色 44.04 %。
- 产物：`result.json`（metrics + 每参数 value/stderr/vary + **`structure` 块** + **`background` 块**）、
  `components.json`（yobs/ycalc/bg/main/extra）。
  `structure` 块是**手动结果接入 GSAS 的通道**（`xrd_export_cif` 的 `result` 源据此出 CIF）。
- **★ 背景机制（用户 2026-09-18 重新声明）**：
  - **默认 6 阶、不迭代**：`background.n_cheb` 默认 6；GSAS 的 AUTO background 默认同为 6 阶。
  - **背景永远是最后一个参数族**：引擎分三阶段 —— ①其他参数 + 起始 6 阶 → ②**冻结其他参数**、
    只逐阶迭代背景 → ③选定阶数后联合收尾。
  - **迭代是条件升级，默认关闭**：只有**其他条件均优化、仍达不到目标**时才传 `bg_scan:true`
    （或 `{"enabled":true,"orders":[...],"stop_tol":0.05}`）。步长 2、上限 20、逐级探测。
  - **每次迭代导出该阶的背景曲线** `bg_nNN.txt`（两列 2theta/background）→ 直接导入 GSAS
    （`xrd_gsas_ops` 的 `import_background`）。
  - **R 值边界效应以本引擎 Rwp 为判据**（返回 `boundary_r_basis` / `boundary_n_cheb`），
    **不看 GSAS wR**；越过边界的更高阶不参与选曲线。实测：默认 6 阶 10.76% → 逐阶到 20 阶 15.31%（纯背景）
    → 联合收尾 **9.53%**。
  - **★ 判读包 `health_flags`（每次返回，是判据的一部分）**：R 值之外必须一起看的健康度 ——
    **触界检测**（参数落在边界 1% 以内 → "effectively PINNED：值由约束设定，不是测出来的"）+
    **esd 健康度**（协方差未计算 / 无 stderr / stderr=0 / stderr÷|值|>0.5 → 参数未被数据确定）。
    实测：一次看似正常的拟合里 **U、W、eta 三个峰形参数全部顶边界**，此前结果中完全看不出来。
    注意 stderr 是"全有或全无"：撞 `max_nfev` 时协方差整体不计算 → 只报**一条全局 flag**，不逐参数刷屏。
  - **★ 新增三件（2026-09-20，回答"这个 R 值能不能信"）**：
    - **`baseline_rwp`**：**全部参数还在起点时**测得的 Rwp。没有它，"Rwp = 38.9" 与"Rwp = 94.6"
      看起来一样；有了它才知道拟合**究竟动了多少**（实测同一组数据：基线 94.56 → 拟合后 38.91）。
    - **`fit_status`**：`{success, nfev, hit_max_nfev, n_at_bound, covariance_available, flags[]}`。
      **`flags` 非空 ⇒ 这个 R 值不是极小值，不得当结果上报**：
      未收敛 / 撞 `max_nfev`（Rwp 是**瞬态值**，不是极小值）/ 参数顶界（值被边界框住）。实测一次返回三条：
      "未收敛"+"撞 max_nfev=8000"+"2 个参数顶界(eta, scale)"。
    - **`r_plateau`**：单次拟合是否已到**本引擎的 R 值洼地**（`is_plateau` / `dof` / `overfit_risk`）。
      判据＝优化器干净收敛 + 无参数顶界 + `nvar/npts < 0.2`；**阶梯的洼地不看它**，
      看 `xrd_refine_auto` 返回的 `ladder` 逐步轨迹。
  - **JSON 卫生（引擎已强制）**：结果文件用 `allow_nan=False` 写出，非有限值递归转 `null` ——
    因为 lmfit 的无界上下限是 ±inf，**`Infinity` 不是合法 JSON**，一旦写进结果文件，
    工具层的 `JSON.parse` 会整包抛错，表现成"引擎没有产出结果"（实测踩过一次）。
- 坑：`data` 必须是对象 `{"file": ...}`；CIF 位点标签取自 `_atom_site_label`（回退元素符号）；
  **无序占位位点**（如 `Na0.72 Ni0.33`）旧版处理会崩，现已按位点组成构造，`bond_targets`/`coord_vary` 在这类结构上可正常用。

### `xrd_refine_gsas` / `xrd_gsas_ops`（GSAS-II）— ⛔ 硬门控
> 用户规则：GSAS **仅作最终优化**，必须在 AutoFP 或手动引擎已收敛之后才可调用（不是第一引擎，也不是 AutoFP 失败的替代重试）。门槛在 skill 层，工具本身不硬拦。

- `xrd_refine_gsas`：`refine:["background","inst","cell","profile","atoms","scale"]` 阶梯；`n_back`、`inst`、`atoms_map`、`size_only`、`cell2`、`scale2_init`、`open2`（第二相 scale/cell **后置**开放，绝不与主相 cell 同一步）。
  - **λ**：`wavelength` / `wavelength_doublet`。**两条路径都直写** `Instrument Parameters` 的
    `Lam`/`Lam1`/`Lam2` 值槽（`[1]`）——**新建工程**是直方图建好后立刻写，**续跑已有 gpx** 是写给它的各个直方图；
    **不把 λ 当参数交给 GSAS**（`add_powder_histogram` 没有这个关键字；0.9.0 曾这么传，结果**每个新建工程都崩**）。
    两条路径都回显改前→改后；单色仪器文件（只有 `Lam`）收到双线请求时只写 Kα1，被丢弃的 Kα2 会写进引擎日志。
  - **★ `phases:[{...},{...}]` 逐相开关（0.8.10，下标＝`cif`/`cif2` 的相序）**：每相可给 `cell / scale / size / mustrain / hstrain / pref / atoms`，其中 `mustrain` 可写 `"isotropic"|"uniaxial"|"generalized"`。
    **这是"主相冻结、只放第二相"的教程式配方**（例：主相关晶胞/尺寸/微应变，第二相只放相分数＋单一各向同性微应变）。**不传 `phases` ⇒ 退回 0.8.9 老行为**（每族一个全局步，`cell2`/`size_only`/`scale2_init`/`open2` 照旧）。
  - **★ `entry` / `rollback` / `rollback_tol`（0.8.10）**：`entry:"hold"`（续跑工程或带 `handoff` 时的默认）＝先用一步验证来态，再**一次只放一族**，只保留让 wR 变好的步；`"direct"`＝0.8.9 老式一次全放（无回滚）。`rollback` 默认跟随 entry，`rollback_tol` 是 wR 容差。
    **回滚是从"工作文件的拷贝"恢复**：GSAS 的 `refine()` 基于文件（`GSASIIstrMain.Refine(self.filename)` ＋ 自动保存），而 `save(别的路径)` 会改写 `self.filename`——用 `save()` 做快照会被下一次精修自己覆盖（实测每一次"回滚"都恢复了本要撤销的状态）。**交付的是"最佳接受态"，不是最后一步**。
  - **★ `handoff:"<手动引擎 result.json>"`（0.8.10，Ⅱ→Ⅲ 交接）**：把有界手动引擎已配好的双相解**播种**进 GSAS-II 再精修。只传三样：第二相相比例（`s2_<i>_scale / scale` → 该相 `Scale`）、主相晶胞（`structure.lattice`）、仪器 `U/V/W/X/Y/Zero`（**带单位换算**：U/V/W **×1e4**（GSAS 是厘度²）、X/Y ×100、**Zero ×1**（GSAS 的 Zero 是度；按厘度传实测令 wR 从 25.9 崩到 74.5））。
    不传并写明原因：Size/Mustrain（另一套参数化）、背景（用 `xrd_gsas_ops` 的 `import_background`）、原子坐标/占位（以 CIF 为准）。**两条防线**：①某相被手动引擎拟合成≈0 → 告警"该相在 GSAS 里将不可见"；②仪器值与工程现有一致值差 **>10 倍 → 拒绝覆盖**（实测照传会把 wR 从 25.5% 弄到 71.8%，等于种子毁掉标定值）。
  - **结果新增字段**：`entry`、`per_phase`、`ladder[{step,label,wR_before,wR_after,accepted,rolled_back,gsas_error?,cell_change_pct?}]`、`handoff{transferred,not_transferred,source_rwp,warnings}`。
    **`gsas_error` 是 GSAS 自己报的失败**（如 `Invalid cell metric tensor for phase #1`）——**失败步一律 reject＋回滚，绝不成为 best**；`warning` 只收"交付态被污染"的问题（失败步被保留／回滚失败／best 态恢复失败），失败但已回滚的步只进 `ladder`、不翻转 `ok`。
    **回滚模式下若一步都没被接受**，`summary` 会明写"交付工程即来态"，别把 `ok:true` 读成"请求的族已精修"。
- `xrd_gsas_ops` actions：
  - 参数：`set_atoms`（flags/Uiso/occ/坐标）、`set_hist`（任意 GSAS section）、`set_cell`、`scale`、`open_uiso`、**`set_adp_mode`**、`fix_uiso`、`refine`、`save`、`snapshot`
  - **★ `set_adp_mode {mode:"isotropic|anisotropic", atoms:["all"|标签], open_uiso?:true}`（用户 2026-09-22 要求补的接口）**：
    **决定精修哪个热参**。GSAS-II 只认**原子上的模式标记**（`atom[cia]`='I'/'A'），**不看 refine flags** ——
    标记为 `A` 时它精修**六个 Uij**，而 `Uiso` 列只是未动的初值；于是 `uiso_clamp` / `fix_uiso` /
    `export_cif(uiso=physical)` / 老的 Uiso 审查**全在看一个没被精修的量**（实测：六个 β 在动、Uiso 列是陈值、负 β 无人发现）。
    `isotropic` 会把**六个 Uij 清零**（防止陈旧的负 β 在切回 `A` 时复活）；`open_uiso` 默认 true，顺带补上 `U` flag
    （切了模式却什么也不精修，是另一种坑）。返回 `adp_mode.atoms[{label,from,to}]`。
    对应地，`review` 现在**报每个原子的 `adp` 模式与 `adp.isotropic/anisotropic` 计数**，各向异性原子按 **U11/U22/U33 对角**检查。
  - 约束：`equiv_uiso`（`add_EquivConstr` 等值 Uiso）、`uiso_clamp`（循环钳位到 ≥min 并摘 U flag）、`clear_constraints`、`split_site`（K 亚位分裂）
  - **工程级**：`replace_phase`（用干净 CIF 重建相、保留直方图/背景）、`add_phase`、`new_from`（派生工程 + 注入新数据）、`set_site_composition`（多元素共占 → 追加同行坐标的原子行）
  - **★ 背景**：`import_background {source:".../bg_nNN.txt", n_points:20, vary:false}` —— 把手动引擎导出的
    背景曲线导入为 GSAS **原生 `lin interpolate`** 背景（等间距 20 个插值点），`vary:true` 才让它继续精修。
    `auto_background` **默认只做单次 6 阶**（`scanned:[6]`）；`iterate:true` 或显式 `coeffs` 才是升级迭代；
    `isolate`（默认 true）扫描期只留背景开关。
  - **⛔ 背景必须最后**：`auto_background` 之后若还有任何改参数的动作 → **工具直接报错拒绝**；
    `import_background` 只有 `vary:false` 时才允许其后继续精修（固定背景≠拟合背景）。
  - **★ `review` 合理性审查块**（每次返回，且**已渲染进返回文本**——以前只在 JSON 里、模型看不到）：`verdict` = pass/warn/fail，
    `flags` 列负 Uiso / **各向异性原子的负 U11/U22/U33** / Uiso 过大或归零 / 占位越界 / wR≥50% 发散；
    `adp` 给各向同性 vs 各向异性原子数与说明。**`fail` 时不得当作完成结构报告**。
    `info` 级（如"这个原子是各向异性"）不计入 verdict，但要看一眼是不是你想要的模式。
  - 全参数表返回：每相 cell/cell_vary + 每原子 flags/occ/uiso/mult + 各直方图 instrument/background（含**背景类型与系数**）+ wR
- 坑：
  - **GSAS 的无界 LS 在弱反射第二相会发散**（实测 scale 被推到 1e13）→ 改"固定 scale2 单点交叉验证"或改用手动引擎（有界）。
  - 口径不同：引擎 Rwp（Poisson）≠ GSAS wR ≠ fp2k conventional（**报告不必声明口径**，
    但自己判断时不可直接比较）。

### `xrd_refine_multiphase`（≥3 相，AI 可编辑代码钩子）— ⛔ 硬门控 ≥3 相
> 用户裁定（2026-10-02）：**多相钩子做成独立新工具**——"单一工具功能过多 AI 会错误操作"。
> **为什么要有它**：`xrd_refine_gsas` 的阶梯是**写死的族序**，三相及以上往往需要"某些参数只给某一相、或两相共享一个 profile、或中途加相"这类编排，固定阶梯表达不了。

- **门控（工具层＋引擎内双保险）**：相数 **< 3 → 直接拒绝**，让你走 `xrd_refine_gsas`（1–2 相）。引擎里的 `MIN_PHASES = 3` 是**运行期检查**，改副本也绕不过。
- **第二入口的用法**：①先 `init_engine:true` → 工具把自带模板 `engine/gsas_multiphase_template.py` 复制成 `<out_dir>/gsas_multiphase_custom.py` 返回（**不跑**）；②读它、改标了 `# == EDITABLE ==` 的编排段（`build_steps()` 里加你自己的步骤）；③再调本工具并传 `engine:"<那份副本>"` 执行。
- **硬要求（用户定）**：**进 GSAS-II 之前必须先用 `xrd_refine_manual` 把最多量的两个相配好**，把它的 result JSON 用 `handoff` 传进来。**不带 `handoff` 的新建工程会被拒绝**，理由就是"先修好两个最多的相"。
- 其余与 `xrd_refine_gsas` 同源：`entry="hold"` ＋ 逐步 accept/rollback ＋ **失败步（`gsas_error`）一律回滚** ＋ 交付最佳接受态；1–2 相别用它。

## 6. `xrd_verify_fp2k`（交叉验证）

- 用途：化学式/密度哨兵、晶胞、Rwp、Bragg R；多相 `.sum` 只解析 phase 1（per-phase 用 GSAS）。
- **⚠ Rwp 的可读性取决于喂进去的 pcr（用户 2026-09-22 追问后写明）**：本工具**只跑单次 fp2k、不精修**，
  所以 **pcr 本身是收敛过的 fp2k pcr 时 Rwp/BraggR 才有意义**；**`cif_to_pcr` 造的 pcr 带名义 profile 参数**，
  此时 Rwp 只是"没精修过"的写照（实测 61.1 / BraggR 94.7），**唯一可用的是密度与晶胞**。
  交付后补做这一步时**只取这两个哨兵**，Rwp 必须注明"单次未精修"，且**这一步不是交付必含项**。
- fp2k 的**原子 esd 不可信** → 发表级 esd 走 GSAS-II。

## 7. `xrd_export_cif`（统一输出端口 + 科学判断）★

- **五源**（任选其一）：
  - `gpx`（GSAS 工程，推荐）→ 图 + **GSAS 工程副本** + CIF（structured / official）+ 科学判断
  - **★ `result`（手动引擎 `result.json`）→ 精修后 CIF（含空间群检测）+ 拟合图 + 科学判断** ——
    这是**手动引擎接进 GSAS 三件套的通道**（手动收敛 → result 源出 CIF → 送 GSAS/交付）
  - `components`（手动引擎 `components.json`）→ 图 + 残差诊断（rms、最大残差点）
  - `data`（原始两列谱，含 **无法做精修**的样品）→ 图 + `no_fit_model` 提示
  - `cif`（裸结构文件）→ 校验
- 科学判断：pymatgen 复解 CIF → 空间群、最近邻键长统计、`<min_distance` 接触、occ 越界、可选 `bond_expect` 窗口；`verdict = pass|warn|fail|skipped`。
- **红线（用户）**：图/CIF/异常值**永远先写出来**；`sanity:false` 完全跳过校验；`anomalies` 列出负/超大 Uiso、occ 越界、坐标出胞、晶胞不合理、scale 极端、wR 偏高 —— 这些"不科学值"是排障证据，由**人手工精修**解决。
- ⚠ **红线之外的交付约束（用户 2026-09-21）**：**"工具照常输出"不等于"可以交付"** ——
  **正常模式（非极限模式）下最终交付的精修结构必须严格遵守物理限制**：不得带**负 Uiso、occ 越界、
  非物理热参**进入交付物。分工：`xrd_export_cif` 照写并标 `anomalies` / `verdict` →
  **AI 据此在交付前修掉**（改约束、钳位、换模型）；修不掉只能走极限模式**显式声明**且**不替换交付**。
- 坑：`bond_expect` 对部分占据位点用元素级匹配（键名形如 `Mn:0.62, Ni:0.33-O`，写 `"Mn-O"` 即可）。

## 8. `xrd_finalize`（交付包）
- 输出 `<out_dir>/<sample>_final/`：干净 gpx（清已删相残留反射表）+ lst + 每相 CIF（官方 `export_CIF`）+ 防遮挡效果图 + `report.md` + manifest。
- 交付卫生：默认清 `Constraints` 三桶（ranId 垃圾）、**清后不 refine**（保住收敛 wR 缓存）、打包后**重开校验**；报告含写入所用 GSAS 版本（旧版读新版 gpx 会报 "not a current GSAS-II .gpx"）。
- 发散守卫：最终 wR ≥ 50% → 告警"不得作为最终结果交付"。
- `clean_constraints:false` 只报告不清。

## 9. 跨工具契约（通用坑）

1. **null vs 默认值**：wrapper 对未设置的可选数值传 null；引擎按"null = 未设置"处理。若自己写引擎，别用裸 `dict.get(k, default)`。
2. **路径**：`out_dir` 默认会话工作区；官方转换器/导出器有"把产物写在输入同目录"的习惯，工具层已处理。
3. **口径**：引擎 Rwp（Poisson）≠ GSAS wR ≠ AutoFP Rwp ≠ fp2k conventional，跨引擎数字不可直接比较
   —— 但**报告不必声明口径**（用户 2026-09-18 明确"口径标准不需要声明"），自己判断时不混用即可。
4. **产物去向**：GSAS 工程的 `.gpx` 是 pickle，禁改名/文本编辑/被更旧 GSAS 打开。
5. 排障顺序：先看返回的 `log_tail`/`warning`，再用**列目录能力**核查产物目录；`xrd_*` 工具都保留中间产物便于追溯。
6. **能力 vs 工具（贯穿本手册与工作流，用户 2026-09-18 要求）**：工作流**只声明需要什么能力，不绑定具体工具**。
   - `xrd_*` / `cif_to_pcr` 是本插件自带接口 —— 这是工作流的作用对象，**不可替换**；
   - 凡涉及**宿主能力**的 —— 视觉/读图、绘图、读文件、写文件、目标跟踪、列目录、跑脚本 ——
     一律按**能力**表述，具体工具名只作"例如"的候选实现；
   - 换 agent / 换环境时只要有等价能力即可跑通；**缺能力时明说，不得假装完成**。
   实例：V1–V4 四处读图判定（见 `xrd-workflow` §4）只要求"能看到一张本地图片" —— agent 自带多模态就
   直接读，没有原生视觉再走宿主视觉桥（例如 `modlens_read_image` / dsh-office 的 `office_read_image`，
   仅作可选实现），两者都没有则明说缺视觉能力。本手册与工作流**不**把任何宿主工具写成前置依赖。
