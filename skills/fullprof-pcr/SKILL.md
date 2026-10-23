---
name: fullprof-pcr
description: FullProf/fp2k Rietveld 精修的 .pcr 控制文件与排障速查。**从 CIF 生成 pcr 一律走 cif_to_pcr 工具(官方 CIFs_to_PCR.exe 直转),禁止手写/拼装结构段**;**输出占位按 FullProf 惯例归一:pcr_occ = CIF_occ × 位点多重度 ÷ 一般位多重度**(2026-09-18 实测两例证实;工具每次自动做归一化占位审计 `occupancy_check`,verdict=fail 才是真不一致)。**报 Rp/Rwp≈100% 通常不是占位问题,而是数据文件对不上**:fp2k **按 pcr 的主名**(job code)去找强度文件,`! Datafile` / `! Files => DAT-file:` 那一行是**注释、不生效**(2026-09-21 实测更正:正文声明 `Y2O3.dat` 且该文件就在同目录,只把 pcr 改名成 `probe.pcr` → `CODEFIL=probe`、0 条 R 值行;旧口径来自被野文件污染的对照实验,已作废),所以**数据文件必须与 pcr 主名一致**。**2026-09-20 起精修由插件自带的 engine/fp2k_driver.py 直接驱动 fp2k.exe(不经任何包装层,只取 fp2k.exe 一个文件)**,该驱动会修掉转换器输出里让 fp2k 打不开的两处缺陷(相位名含点号、Chebyshev 背景面板),并把自动精修策略(参数顺序+逐步放开/回滚)内置。无 CIF 或微调时才用模板逐行改。含 new-style/old-style 行结构、原子 5-code 规则、Excluded/Nex、scale 初值、常见 fp2k 解析报错对照。随附 3 份验证模板。触发词:精修、Rietveld、FullProf、fp2k、pcr、CIF转pcr、cif_to_pcr、fp2k 报错、FindFMT、Rwp 异常、占位精修、占位审计、occupancy_check、相位名、背景面板、Nex。
whenToUse: 生成/修改 FullProf .pcr、CIF 起步精修(cif_to_pcr 直转)、用 fp2k 驱动精修、pcr 报错排障(FindFMT/integer field/line749/Rwp 异常/NaN)、sampleA/PBA 类结构时
---

# FullProf .pcr 生成与 fp2k 精修速查

## 0. ★ CIF→PCR 首选入口(用户规则 2026-09,强制)
> 有结构 CIF 时,**一律用 `cif_to_pcr` 工具**(包装官方 FullProf `CIFs_to_PCR.exe`)
> 生成 .pcr,**禁止手写/从模板拼装结构段**。官方转换自动解析空间群/晶胞/原子/
> site 占位(按 FullProf 惯例归一,见下),输出为 fp2k 可读格式——**信任官方输出,不做额外 occ 改写**
> (用户决策;如需手调再走 §0.1 模板路径)。
>
> **没有 CIF、库与文献也查不到时** → 走 `xrd-workflow` **§7 的兜底流程**：
> `xrd_cif_edit` 的 `build_orbit`（按 Wyckoff 代表点构造）+ `validate`（原始文本层体检）
> → `cif_to_pcr` → fp2k 打分裁决。**不要凭坐标手敲 CIF**。

- **调用**:`cif_to_pcr {cif: <绝对路径>}` → 产出同目录同名 .pcr(可 out_dir 指定)。
- 工具定位:`C:\Program Files (x86)\FullProfAPP\resources\fpsuite\windows\CIFs_to_PCR.exe`
  (FullProfApp 套件自带,与 fp2k.exe 同目录;env `DSH_FULLPROF_CIF2PCR` 可覆盖)。
- 退出码 24 = 交互提示产物,只要 .pcr 生成即成功(工具按文件存在判定)。
- **占位约定(2026-09-18 实测两例证实,纠正旧记录)**:转换器写的是
  **`pcr_occ = CIF_occ × 位点多重度 ÷ 一般位多重度`** ——
  Fm-3m K0.80→0.03333(×8/192)、C1.0→0.125(×24/192);
  P 6₃/mmc Mn0.62→0.05167(×2/24)、O1.0→0.16667(×4/24),比值与公式**完全吻合**。
  所以 **pcr 占位远小于 CIF 值是正常的**,不是缺陷;工具内置的 `occupancy_check`
  也已经按这个公式判定(pass/warn/fail/unknown)。
- ⚠ **fp2k 报 Rp/Rwp≈100%(或完全不出 Rwp)时先查数据文件,不要先怀疑占位** ——
  但**根因是"pcr 主名",不是"正文声明的名字"**(2026-09-21 更正,旧结论来自一次**被同目录
  野文件污染**的实验,已作废):
  * fp2k **按 pcr 的主名**(FullProf 的 job code)去找强度文件;`! Files => DAT-file: …` /
    `!File names of data(patterns) files` 那一行是**注释,不生效**;
  * **实测对照**:正文声明 `Y2O3.dat` 且该文件**就在同目录**,只把 pcr 改名成 `probe.pcr` →
    fp2k 报 `Error opening file CODEFIL.DAT(UXD,ACQ,CSV,UDF,JCP,XRDML) with CODEFIL=probe`、
    **0 条 R 值行**;原名 `Y2O3.pcr` → 正常收敛 **81.1**;
  * 所以:**数据文件名必须与 pcr 主名一致**(多 pattern 工程只有存在**非注释**的 pattern-file
    名列表时才另说);**不要**通过改写那行注释来"修配对",它不起作用;
  * 声明名不存在时 fp2k **只打 banner 就静默停**(`.out` 里没有 R 值行),极易误判成"结构错"。
- **仍适用的红线**(转换产物也一样):纯 ASCII、Mat/Pcr∈{1,2}(自动模式)、数据文件放在 pcr 同目录、
  角度范围与 .dat 对齐、code 唯一。这些是 fp2k 解析硬要求,与来源无关。
- ⚠ **第二种"不出 Rwp"的原因(2026-09-19 实测,与数据配对无关)**:`CIFs_to_PCR.exe` 直出的
  pcr 背景段是 **Chebyshev 多项式(24 系数,8 行×6)**,而 **autoFP 的 pcr 解析器读不了它** ——
  `diffpy/pyfullprof/pcrfilereader.py` 第 749 行 `bakval = toFloat(words[1])` 抛
  `IndexError: list index out of range`,于是 `r.params` 为 None,工具报
  **"refinement did not report Rwp"(exit 0/1)**。注意此时 **fp2k 其实跑完了**(`.out` 里有
  `Conventional Rietveld Rp,Rwp,Re and Chi2`,通常是未精修的 ~79%),只是驱动层拿不到数。
  **一眼判别**:`.fp2k-work-*` 目录里**没有 `par.txt`** ⇒ 撞上这条;有 `par.txt` 才是真收敛。
  **✅ 已解决(2026-09-20):这条不再是问题** —— 插件已**不再经过 AutoFP 的包装层**,
  `xrd_refine_auto` 改由自带的 `engine/fp2k_driver.py` **直接驱动 fp2k.exe**
  (只从 FullProf 安装取 `fp2k.exe` 一个文件),并在驱动内部顺手修掉转换器输出里会
  让 fp2k 打不开文件的两处缺陷:**相位名行**(转换器把 CIF 文件名连同点号写进去 ⇒
  `FindFMT: invalid character in a numeric field` 中断)与**Chebyshev 背景面板**
  (其系数对精修 code 不可见 ⇒ 改写为多项式,并校正 Job 行的 Npr/Nba)。
  **仍要记住的旧判据**:`.fp2k-work-*` 里没有 `par.txt`、或 `.out` 只有 banner 没有
  R 值行,都说明"**精修根本没跑**",不要读成"结构/占位不对"。

## 0.1 无 CIF / 微调路径(保留原模板法)
- 没有 CIF(如从文献手建结构)或只需微调已生成 pcr 时,才用下方模板逐行改;
  有 CIF 一律回 §0 走 cif_to_pcr。

## 0.2 最重要的工作方法(先读)
1. **绝不凭记忆手写整份 pcr**。先 `copy` 本 skill 模板目录里一份与目标同代的模板,
   只改:数据文件名、角度范围(Thmin/Step/Thmax)、相段原子、Profile 数值。
   `templates/` 三份模板全部经 fp2k 实测能跑通:
   | 模板 | 格式 | 数据类型 | 结构 |
   |---|---|---|---|
   | `templates/Y2O3_xray.pcr` | **new-style** | X-ray Cu | Ia-3,3 原子,Y2O3 |
   | `templates/pba_working.pcr` | **new-style** | X-ray Cu | Fm-3m,5 原子,PBA(sampleA 实测) |
   | `templates/pbso4a_neutron.pcr` | **old-style** | 中子 D1A | Pnma,5 原子,PbSO4 |

   > **资源位置（0.8.6）**：上表模板与 `xrd-workflow` 的 `refs/` **随插件包一起发布**，但宿主若只把
   > `SKILL.md` 装进 skill 根目录，同目录的 `templates/`、`refs/` 可能**没被复制**。此时不要判断成
   > "模板不存在"——它们仍在插件自己的 `skills/` 目录里（`<dsh-xrd 插件根>/skills/fullprof-pcr/templates/*.pcr`、
   > `<dsh-xrd 插件根>/skills/xrd-workflow/refs/*`），按插件根路径直接读即可；维护者用
   > `sync-skills.mjs` 把整棵 `skills/` 树镜像到两个 skill 根。
2. **改 pcr 后逐行数核对**:增删任何一行都会让 fp2k 整段错位,报错行号是"假"的
   (实际错在前面的某个块)。对照模板逐段 diff,不要信报错行号直接改那行。
3. 改完先用**裸 fp2k** 冒烟(见 §6),确认能读、能出 cycle 1,再谈精修质量。

## 1. 环境事实
- AutoFP v1.7b15 源码:放在 **`$DSH_FP2K_ROOT`**(含 `shautofp.py`、内置 `fp2k.exe`);
  也可写进插件目录的 `xrd.local.json` 的 `fp2k_root`。位置解析见插件 `lib/config.js`。
- Python:插件自己解析解释器——`DSH_XRD_PY`,否则 `xrd.local.json` 的 `python`,
  否则自动探测 `%LOCALAPPDATA%\Programs\Python\Python3*`。**只认这两个键**;
  引擎起不来时错误里会直接给出"用的是哪个解释器、缺哪个包"。
  Windows 上 PATH 里的 `python` 常是 Store 占位 stub,**不要用**,显式给出路径。
- `future` 1.0.0 必须已装;**PyQt4 不需要**(命令行不 import;GUI 才要,3.13 装不了)。
- 命令行跑法(cwd **必须 = autofp 根**):`python shautofp.py -c N -a xxx.pcr`
  (-c 0=自动轮数,-a=自动选参);或裸 `fp2k.exe xxx`(在 pcr 所在目录)。
- **别在 autofp git 仓库根跑 shautofp.py**:subauto.py 会 copytree 整个 pcr 目录备份,
  含 .git → PermissionError。pcr 放根下子目录,或独立目录 + 裸 fp2k。
- **AutoFP 自动模式已修复可跑**(2026-09-06):fpoutputfileparsers 按 RELIABILITY 段
  切块+正则兜底、com.py 哑队列降级、subrun 去 shell=True。官方 Y2O3 `-c 1 -a`
  自动选参 37 步收敛 Rwp=7.24 无崩溃无噪音。详见 MAINTENANCE.md AutoFP 章节。

### 1.1 AutoFP 自动模式(shautofp -a)特有的 pcr 前置(与裸 fp2k 不同!)
- **pcr 必须纯 ASCII**:AutoFP 用 GBK 读 pcr,中文注释(如"不覆写")→ gbk decode 崩。
- **Mat/Pcr 行必须 =1(或 2)**:AutoFP 的 pcrfilereader 只接受 Pcr∈{1,2}
  (它靠 fp2k 回写 pcr 读回参数);我们手动链的 Pcr=0 在自动模式会被拒。
- **Pcr=1 → fp2k 每次精修后回写覆改 pcr**(收敛参数写回,原输入丢失);
  需保留原始输入请先备份,或探索 Pcr=2(写 .new 不覆写)的行为差异。
- **occ 用 ×192 归一写法**(公式与换算表见 §4 occ 小节:occ=位点多重度/一般位 192,
  24e C=0.125、8c K×0.73=0.0304…),否则密度错一半(1.046 vs 2.088)、化学含量失真。
  裸 fp2k 手写链也建议归一(密度/化学含量才物理正确)。
- 自动模式跑完会把精修结果**回写进传入的 pcr**(先备份原件);
  偶见回写含 `*********` 溢出占位(收敛后二次读回警告),结果仍以 .out/par.txt 为准。
- 决策看末轮 Rwp:从 `.out` 解析最后一块 R 值行。
  ⚠ **但"不受 pcr 回写影响"这句要分成两半读(2026-09-21 实测订正)**:
  ①**读数**确实不受影响 —— Rwp 来自 `.out`,不看 pcr;
  ②**归属**受影响 —— fp2k 报的是**它评估过的那套参数**的 Rwp,而它留在 pcr 里的是**下一次更新
  (尚未评估)**。所以**回写文件不携带它报出的那个数**:同一份数据,从**起点状态**再精修得
  **16.0**(正是记录值),从**回写文件**再精修得 **17.5**。⇒ **要交付"那份 R 所属的状态",就交
  起点状态**;写回文件只作带标签的备份(它含已精修的参数值)。
- **code 撞车警示**:AutoFP 自动编号偶发 scale 与 W 共用同一 code(被约束联动,
  精修异常)。手动给 code 时确认每参数唯一;自动模式若结果怪异,检查回写 pcr 的
  code 是否重复。

## 2. 两种 pcr 格式(先判断你手上的模板是哪代)

### 判别
| 特征 | new-style | old-style |
|---|---|---|
| 头部 | `NPATT 1 1` + `W_PAT` + `!Nph Dum Ias Nre Cry Opt Aut` | `!Job Npr Nph Nba Nex Nsc Nor Dum Iwg Ilo Ias Res Ste Nre Cry Uni Cor Opt Aut`(19 字段单行) |
| Job 行 | 14 字段:`Job Npr Nba Nex Nsc Nor Iwg Ilo Res Ste Uni Cor Anm Int` | 19 字段(见上) |
| 相段标题 | 纯名称行(如 `SampleA KFeMn PBA`) | 名称+ROTAX 晶胞行(如 `PbSO4 ROTAX ...`) |
| 典型模板 | Y2O3_xray / pba_working | pbso4a_neutron |

**同一份 pcr 必须整份同一代,禁止混用两代行序。**

### new-style 逐行规范(以 templates/pba_working.pcr 为准,85 行)
下表行号 = 该模板的真实行号。改自模板时保持"结构行"(注释/空行)不变,只改数值。
```
L1  COMM <自由标题>            ← 第一行,无前导空格
L2  ! Current global Chi2 ...  ← 可留 0.0
L3  NPATT <N> <N>              ← 图案数;固定 "1 1" 单图案
L4  W_PAT <权重>               ← 固定 1.000
L5  !Nph Dum Ias Nre Cry Opt Aut
L6  1 0 0 0 0 0 1              ← Nph=相数(必须=下面实际相段数)
L7  !Job Npr Nba Nex Nsc Nor Iwg Ilo Res Ste Uni Cor Anm Int
L8  0 5 <Nba> <Nex> 0 1 0 0 0 0 0 0 0 0  ← Nba=0(多项式背景);Nex 见 §3
L9  !
L10 !File names of data(patterns) files
L11 <dat 文件名>                ← .dat 必须与 pcr 同目录;名字自由
L12 !
L13 !Mat Pcr NLI Rpa Sym Sho
L14 0 1 0 0 0 0
L15 !Ipr Ppl Ioc Ls1 Ls2 Ls3 Prf Ins Hkl Fou Ana
L16 0 0 1 0 4 0 3 10 0 0 0     ← Prf 位=3(Pseudo-Voigt)
L17 !
L18 ! Lambda1 Lambda2 Ratio Bkpos Wdt Cthm muR AsyLim Rpolarz 2nd-muR
L19 <λ1> <λ2> <ratio> <Bkpos> <Wdt> ...   ← X-ray Cu:1.540560 1.544390 0.5;中子:λ=λ2 同值 ratio=1.0
L20 !
L21 !NCY Eps R_at R_an R_pr R_gl
L22 <周期数> 0.10 1.00 1.00 1.00 1.00
L23 !Thmin Step Thmax PSD Sent0
L24 <Thmin> <Step> <Thmax> 0.000 0.000   ← 必须与 .dat 实际首末角一致(§5)
L25-26 两个空行(保持模板原样;Nex=0 无排除块,见 §3)
L27 <N> !Number of refined parameters  ← N 必须=实际分配的非零 code 去重总数(§4)
L28 !
L29 !Zero Code SyCos Code SySin Code Lambda Code MORE
L30 <zero> <code> ...             ← code=0 固定;>0 精修(编号见 §4)
L31 !Background coefficients/codes (Polynomial of 6th degree)
L32 <6 个背景值>                 ← ★6 值,不是 7!(官方模板全 6 值;7 值 fp2k 也容错但非标准)
L33 <6 个背景 code>              ← 每值对应 code;0=固定
L34 !---…(分隔线)
L35 !Data for PHASE number: 1 ...
L36 !---…(分隔线)
L37 <相名称>                     ← 纯文字行
L38 !
L39 !Nat Dis Ang Jbt Isy Str Furth ATZ Nvk More
L40 <Nat> 0 0 0 0 0 0 <ATZ> 0 0  ← Nat=不对称单元原子数(必须=下面原子块数)
L41 !Contributions (0/1) ...
L42 1
L43 !Irf Npr Jtyp Nsp_Ref Ph_Shift
L44 0 5 0 0 0                    ← Npr=5:profile 函数第 5 种(勿改 7)
L45 !Pr1 Pr2 Pr3 Brind. Rmua Rmub Rmuc
L46 0.000 0.000 1.000 1.000 1.000 1.000 1.000
L47 !
L48 <空间群符号>                ← 如 F m -3 m / I a -3
L49 !Atom Typ X Y Z Biso Occ In Fin N_t Spc /Codes
L50 !beta11 beta22 beta33 beta12 beta13 beta23 /Codes
[原子块 ×Nat,每个原子固定 4 行,见 §4]
L51 原子行1: <Label> <Typ> <X> <Y> <Z> <Biso> <Occ> 0 0 2 0
L52 5-code 行1: <Xcode> <Ycode> <Zcode> <Biso_code> <Occ_code>  ← ★Biso code 是第 4 个!
L53 beta 值行1: 6 个 0
L54 beta code行1: 6 个 0
L55-58 原子2 ...(每原子 4 行,直至 L70 最后一个原子)
L71 !-------> Profile Parameters for Pattern # 1
L72 !Scale Shape1 Bov Str1 Str2 Str3 Strain-Model
L73 <scale> 0.00001 0.00000 ...    ← scale 初值量级见 §5
L74 <11> 0.000 ...                ← 下行第 1 个=11 表示"精修该相 scale"(惯例)
L75 !U V W X Y GauSiz LorSiz Size-Model
L76 <U> <V> <W> <X> <Y> 0 0 0
L77 <Uc> <Vc> <Wc> <Xc> 0 ...      ← 要精修就给不同正数(§4)
L78 !a b c alpha beta gamma
L79 <a> <b> <c> 90 90 90
L80 <a_code> <b_code> <c_code> 0 0 0  ← 立方:三 code 同值(如 6 6 6)=约束等长
L81 !Pref1 Pref2 Asy1 Asy2 Asy3 Asy4
L82 1.00000 0 0 0 0 0
L83 0 0 0 0 0 0
L84 !2Th1/TOF1 2Th2/TOF2 Pattern to plot
L85 <2Th1> <2Th2> 1
```

## 3. Excluded regions 与 Nex 的一致性(极易踩的解析错)
- Job 行(上面 L8)第 4 个字段 = **Nex**(排除区间段数)。
- **Nex=0 → 全文件禁止出现 "! Excluded regions" 块**,连标题行都不能有。
  否则 fp2k 会把排除区间行(如 `1.00 4.00`)当后续数值行读 → 整段错位,
  报 `FindFMT: integer field found real` 或 `invalid character ... line N`
  (N 指向错位后位置,不是真正问题行)。
- Nex=N → "! Excluded regions" 标题后必须有**恰好 N 行**区间,每行两个整数
  (写 `1 4`、`66 70`,不要写 `1.00 4.00`)。
- Y2O3 模板:Nex=2(带 2 行);sampleA 模板:Nex=0(无块);pbso4a:Nex=2(带 2 行)。
  从模板改 Nex 时,块的有无/行数必须同步增减。
- 数据范围外可以不设排除区(Nex=0),最省事——sampleA 就是 Nex=0 跑通。

## 4. 原子块与精修 code 编号(严格规则)
### 每个原子固定 4 行
1. 原子行:10 个字段 `Label Typ X Y Z Biso Occ In Fin N_t Spc`。
   Label 建议短(≤4 字符),Typ=元素符号(大写首字母)。
2. **5-code 行**:5 个 code,顺序 **X Y Z Biso Occ**。坐标不精修→前 3 个写 0;
   Biso 要精修→**第 4 个**写正数;**Occ 是第 5 个**。把 Biso code 误写进 beta 行
   → fp2k 当成各向异性 β11 精修 → NaN。
3. beta 值行:6 个 0(占位,各向异性未用)。
4. beta code 行:6 个 0。
### code 编号全局规则
- 0 = 固定不精修。非 0 = 该参数参与精修,数值=参数编号。
- **同编号 = 被约束为同一参数**(联动)。不同参数必须不同编号。
- 常规分配:zero=1,scale=11(惯例),U=2,V=3,W=4,X=5,a 系=6,原子 Biso=7,8,9,10,11…
- L30 的 "Number of refined parameters" = 非零 code 的去重总数(如用了 1,2,3,4,5,6,7,8,9,10,11 → 写 11;但若 11 只是 scale 且不重复,则 = 不同编号个数)。
- **先收敛后放开**:初次跑只给 scale(11)+zero(1)+背景几个 code,其它全 0;
  Rwp 降到合理后再逐个加峰形 U/V/W/X、Biso、晶胞 a。一次全开极易发散(NaN)。

### 无序占位写法(Fm-3m 4b 位 Fe/Mn 共享)
- Fe2 occ 0.3 与 Mn1 occ 0.7 各写一个原子块,坐标相同(0.5 0.5 0.5),
  各自 occ 分别填——多重度由空间群自动展开,FullProf 正确识别。
- Fm-3m 常见位点:4a=(0,0,0) / 4b=(½,½,½) / 8c=(¼,¼,¼) / 24e=(x,0,0)。
- C/N 桥在 24e:C 的 x≈0.19、N 的 x≈0.30(PBA 典型;从 CIF 抄)。

### occ 占位率写法(官方转换输出 vs 手动模板)
- **两条路径的 occ 约定完全一致**(2026-09-18 实测纠正):`occ = 位点多重度 ÷ 空间群一般位多重度`
  × 该位化学占位率。**`cif_to_pcr` 的官方转换产物就是这个约定**
  (Fm-3m K0.80→0.03333=0.80×8/192;C1.0→0.125=24/192;
  P 6₃/mmc Mn0.62→0.05167=0.62×2/24、O1.0→0.16667=4/24)。
- **手动模板路径(无 CIF/微调时)**:同一公式自己写。Fm-3m 一般位=192;Ia-3=48
  (Y2O3 Y1@8b occ=0.16667=8/48)。
  | 位点 | 多重度 | 满占 occ | 例 |
  |---|---|---|---|
  | 4a / 4b | 4 | 0.02083 | 4a 满占=0.02083;4b×0.3=0.00625 |
  | 8c | 8 | 0.04167 | 8c×0.73=0.03042 |
  | 24e | 24 | 0.125 | 24e 满占=0.125 |
- 检验:`fp2k` sum 的 density 合理(如 PBA 含 K ≈2.0-2.1 g/cm³)即 occ 正确;
  **注意区分两种"看起来不对"**:occ 远小于 CIF 值是**正常归一**;
  而化学含量差一半或密度明显离谱才是真错(手动写时漏除一般位)。
  自动对账直接看 `cif_to_pcr` 的 `occupancy_check`(verdict=fail 才是真不一致)。

## 5. 数值初值量级(决定 Rwp 是否荒谬)
- **scale**:台式 XRD(Cu、计数几百~几千)量级 **~1e-9~1e-8**。Y2O3 模板的 1e-4
  是同步辐射归一,照抄必炸(Rwp 数百~数千%)。中子可到 ~1 量级(pbso4a=1.3869)。
- **Rwp 荒谬(几百~几千%)第一嫌疑 = scale 初值差数量级**,不是结构错。
- **fp2k 峰形 U/V/W/X 无约束自由精修必然发散**(pcr 内峰形参数无边界机制,
  初值不当 FWHM²<0 → 全谱奇异矩阵/NaN;U=0.727"崩盘"即此)。
  正确做法:①峰形初值量级必须对(XRD ~0.001-0.02,不是 0.7);②一次只放开一个;
  ③需要多参数带界精修时改用带约束的 python 引擎(lmfit 等),不要指望裸 fp2k 收敛。
- 修法:先固定除 scale/zero/背景外一切,确认 cycle1 Rwp 落到 ~50~100%,
  再逐步放开。峰位命中(见下)即证明结构对,只差 scale/峰形。
- .dat 对齐:pcr 的 Thmin/Step/Thmax 必须覆盖 .dat 实际范围。
  sampleA:dat 10.0000→70.0011 共 2802 点 → pcr `10.0000 0.021429 70.0011`。
- 结构自检:fp2k 输出 .prf 里 Ycal 峰位命中实测(如 17.43°=Fm-3m(200)、24.7°(220))
  即结构正确。

## 6. 运行行为与调试纪律
- **裸 fp2k 不覆写 .pcr**;发散(Chi2 恶化>1.2×)写 `.new` 并提示
  "Current parameters not saved... saved in a file of extension NEW"。
- **AutoFP(shautofp.py)会覆写 pcr**(push/pop 管理),并生成 backup_* / rwp*.txt。
- 调试:用裸 fp2k(`fp2k.exe xxx`,cwd=pcr 目录)最干净;每次从同一份模板重跑,
  不依赖上一次输出。改代码前先 `copy` 备份一份模板。
- 冒烟判据:能读到 "Reading control file *.PCR ... OK / Generating reflections"、
  出现 "CYCLE: 1" 与 R-Factors,即 pcr 语法通过;解析错(§7)才是语法问题。

## 7. 常见报错对照表
| 报错 | 根因 | 修法 |
|---|---|---|
| fp2k `FindFMT: integer field found real`(报 `1.00 4.00` 类行) | **Nex=0 却写了 Excluded regions 块**,区间行被当数值读 | 删整块(连标题);或 Nex 改成实际段数(§3) |
| fp2k `FindFMT: invalid character in a numeric field`(报相位标题/原子行) | 前面某块多/少一行(背景行数、排除区、空行)错位 | 与模板逐行 diff;优先查 Excluded 块与背景 6 值 |
| fp2k 报错行号指向"标题/原子名"行 | 行号是错位后的假象 | 别改那行;回查它前面的块行数(§2 模板对照) |
| `pcrfilereader.py line 749 list index out of range` | Job 行 **Nba≥2** 被当"用户定义背景点数"逐行读 | Job 行 Nba=0(§2 L8) |
| `out file check error code = -32` | fp2k 输出无 Rwp(没跑起来) | 查前面解析错;通常是同一 pcr 问题 |
| Rwp 数百~数千% | scale 初值差数量级 | scale 改 1e-9~1e-8(XRD);先只修 scale/zero |
| NaN(cycle 后) | Biso code 误放 beta 行 / 参数全开发散 | code 放 5-code 行第 4 位;固定 Biso 收敛再放开 |
| fp2k `Error opening file` | cwd 不在 pcr 目录 | cd 到 pcr 目录再跑 |
| shautofp 备份 PermissionError(.git) | 在 git 仓库根跑 | pcr 放子目录/独立目录+裸 fp2k |
| `cif2pcr` / PyQt4 import 失败 | AutoFP 源码 cif2pcr 是 UI 壳,无转换逻辑 | 别绕 PyQt4;用 `cif_to_pcr`(官方 CIFs_to_PCR.exe,§0) |

## 8. 模板目录用法
- `templates/Y2O3_xray.pcr`(new-style,X-ray):简单氧化物,最干净的 new-style 参照。
- `templates/pba_working.pcr`(new-style,X-ray,PBA Fm-3m):**目标结构直接改这个**。
- `templates/pbso4a_neutron.pcr`(old-style,中子):old-style 参照(带 ROTAX 相标题、
  19 字段 Job 行、Pr1-Pr3 行等,与 new-style 不同,勿混用)。
- 用哪个模板,整份结构就照哪个;只动数据名/角度/原子/Profile 数值行。
