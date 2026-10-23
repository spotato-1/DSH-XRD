# DSH-XRD

[![版本](https://img.shields.io/badge/version-0.9.1-2563eb)](./CHANGELOG.md)
[![许可证](https://img.shields.io/badge/license-GPL--3.0-16a34a)](./LICENSE)
[![DSH 插件](https://img.shields.io/badge/DeepSeek%20Harness-plugin-111827)](./package.json)

[English](./README.md) | **简体中文**

DSH-XRD 是面向 **DeepSeek Harness（DSH）** 的 AI 原生粉末 X 射线衍射与 Rietveld 精修插件。项目将 12 个原生工具、6 个工作流 skill、FullProf、有界 Python 引擎和 GSAS-II 连接成一条完整链路，可从原始衍射数据一直推进到精修结构和标准化科研交付物。

插件面向低人工干预、高自动化程度和可靠输出设计。AI 可以检查输入、识别物相、获取或重建晶体结构、自主选择精修路线、执行分阶段优化、回溯竞争假设、接续人类未完成工程，并生成最终 CIF、拟合图、报告和工程文件。

- **当前版本：** 0.9.1
- **工具接口：** [`INTERFACE.md`](./INTERFACE.md)
- **版本历史：** [`CHANGELOG.md`](./CHANGELOG.md)
- **开源协议：** GNU GPL v3
- **内置运行时：** Windows 与 Linux FullProf `fp2k`，附来源与哈希记录


## 核心优势

### XRD 全流程覆盖

DSH-XRD 在一个插件中连接完整工作链：输入格式检查、厂商文件头证据、波长处理、数据转换、峰宽分析、CIF 获取与重建、物相筛查、分阶段 Rietveld 精修、物理审查、交叉验证和面向科研交付的结果打包。

### 自动精修

内置工作流 skill 使 AI 能够自主组织并执行精修路线。AI 可以选择起始结构、比较候选模型、分阶段释放参数、保留改善状态、回滚恶化步骤，并在保留完整轨迹的同时推进到最终结果打包。

### 三引擎协同

- **FullProf `fp2k`：** 用于快速结构评分、基线拟合和第一阶段自动精修阶梯。
- **有界 Python/lmfit 引擎：** 为宽峰、选定坐标、键长软约束、有界参数和两相 warm start 提供 AI 可编辑数值空间。
- **GSAS-II：** 用于最终约束精修、逐相控制、晶体学约束和标准化工程交付。

显式 handoff 会在阶段间传递选定的晶胞、相比例和峰形参数。

### 结构搜索与重建

DSH-XRD 可从 Materials Project 与 COD 获取结构，比较不同多晶型，编辑位点组成与占位，根据空间群和 Wyckoff 信息构建新 CIF，生成完整对称轨道，并在精修前验证重建模型。

### 缺陷与复杂样品处理

工作流覆盖混合位点、空位假设、位点分裂、占位守恒、ADP 模式、杂相、宽峰、高缺陷材料，以及三相及以上工程。

### 长程自主任务

按样品保存状态文件、追加式日志、进度心跳、运行标识、检查点、结果新鲜度、回滚与周期审查，使 AI 能够跨多轮、跨会话持续处理复杂精修任务。

### 接续人类工程

插件可以继续现有 FullProf PCR 与 GSAS-II GPX 工程。研究者可提供部分精修工程、在过程中人工介入，再通过 `继续`、`交回`、`接着`、`恢复`、`continue` 或 `resume` 将控制权交还 AI。

### 完整科研交付物

收敛工程可打包为干净 GPX、精修日志、逐相 CIF、拟合图、残差面板、反射刻度、hkl 标注、Markdown 报告、指标和 manifest。

### 随包自审计

仓库内置完整性检查、回归套件、真实工具入口测试、负面对照、测试夹具、schema 检查、引擎语法检查和跨工具链验证。

## 工作流

六个内置 skill 引导 AI 通过五个相互连接的阶段：

```text
1. 材料识别
      |
2. 数据初筛与波长证据
      |
3. 定相与竞争结构
      |
4. 精修
   4.1 建立并验证 CIF 候选
   4.2 用 fp2k 或有界 LM 到达数值洼地
   4.3 完成 GSAS-II 优化与物理审查
      |
5. 导出、交叉验证与报告
```

随着新证据出现，各阶段可以回退和重复。三相及以上项目通过建立在稳定两相 handoff 上的专用多相编排路线处理。

## 12 个原生工具

| 工具 | 职责 |
|---|---|
| `xrd_data_io` | 读取、检查、转换、切除、对齐数据，计算 FWHM/Caglioti 项与表观 Scherrer 尺寸 |
| `xrd_cif_edit` | 读取、编辑、构建、验证、对称化、获取并枚举结构候选 |
| `cif_to_pcr` | 使用官方 FullProf 转换器生成 PCR，并审计归一占位 |
| `xrd_phase_screen` | 根据原始峰或残余峰排序候选物相 |
| `xrd_refine_auto` | 运行无头 FullProf `fp2k` 自动精修阶梯 |
| `xrd_refine_manual` | 运行有界、AI 可编辑的 Python/lmfit 引擎 |
| `xrd_refine_gsas` | 使用 handoff 与回滚执行一相或两相 GSAS-II 最终精修 |
| `xrd_refine_multiphase` | 编排三相及以上 GSAS-II 工程 |
| `xrd_gsas_ops` | 控制 GSAS-II 参数、约束、占位、ADP、晶胞、相标度与物相 |
| `xrd_export_cif` | 从支持的工程或结果源导出、检查结构并生成拟合图 |
| `xrd_finalize` | 将收敛的 GSAS-II 工程打包为完整交付集 |
| `xrd_verify_fp2k` | 用 FullProf 交叉验证 PCR，并报告 R 因子、密度与晶胞信息 |

完整参数、默认值和返回 schema 见 [`INTERFACE.md`](./INTERFACE.md)。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `lib/` | DSH 工具注册、schema、进程编排、依赖解析与审查逻辑 |
| `engine/` | 无头 Python 引擎与共享进程守护模块 |
| `skills/` | 六个工作流和工具使用 skill |
| `audit/` | 完整性检查、回归套件、对照和夹具 |
| `third_party/fullprof/` | 内置 FullProf 二进制、GPL-3 文本、哈希与来源说明 |
| `INTERFACE.md` | 工具接口契约 |
| `CHANGELOG.md` | 版本历史与验证说明 |
| `cordis.patch.yml` | DSH bundle 注册补丁 |

## 环境要求

### 基础环境

- 具备相应插件 API 的 DeepSeek Harness；本版本针对 DSH desktop 0.1.0-rc.5 构建和测试
- DSH 宿主提供的 Node.js
- 推荐 64 位 CPython 3.13
- 与已测试 GSAS-II 二进制组合配套的 NumPy 2.x

Python 依赖：

```bash
python -m pip install numpy scipy PyCifRW pymatgen gemmi lmfit
```

### 晶体学程序

| 程序 | 主要职责 | 分发方式 |
|---|---|---|
| FullProf `fp2k` | 自动精修与独立验证 | 内置 Windows 与 Linux 版本 |
| FullProfApp `CIFs_to_PCR.exe` | 官方 CIF 转 PCR | 单独安装 |
| GSAS-II | 最终精修、约束、导出与打包 | 单独安装 |
| Materials Project API | 结构检索 | 可选凭据 |
| COD | 结构检索 | 无密钥直接访问 |

## 人工安装

1. 将本仓库放入活动 DSH profile，常见位置为 `~/.dsh/profiles/web/node_modules/dsh-xrd`。
2. 在 profile dependencies 中加入 `"dsh-xrd": "file:/absolute/path/to/dsh-xrd"`。
3. 在 `dsh.profile.bundles` 中加入 `"dsh-xrd"`。
4. 复制 [`xrd.local.example.json`](./xrd.local.example.json) 为 `xrd.local.json`，填写可用程序路径。
5. Linux 下运行 `chmod +x third_party/fullprof/fp2k`。
6. 运行 `node audit/integrity-check.mjs`。
7. 重启 DSH 并确认工具和 skill 目录。

## 配置

`xrd.local.json` 用于记录插件调用的程序：

```json
{
  "python": "C:/Python313/python.exe",
  "gsas_root": "D:/GSAS-II",
  "fp2k_root": "D:/FullProf",
  "cif2pcr_exe": "C:/Program Files (x86)/FullProfAPP/resources/fpsuite/windows/CIFs_to_PCR.exe"
}
```

优先级为：显式工具参数、环境变量、`xrd.local.json`、约定位置探测。

## 启动 XRD 项目

为每个样品建立一个工作区子目录，放入原始衍射数据，以及可用的组成、合成路线、辅助表征、文献、候选 CIF、PCR 或 GPX 工程。

通用请求：

```text
<路径>中保存了<材料>的 XRD 数据。使用完整 DSH-XRD 工作流检查输入、确定物相模型、执行自动精修、完成最终 GSAS-II 阶段，并打包全部结果。
```

复杂或高缺陷样品请求：

```text
<路径>中保存了<材料>的 XRD 数据。该样品可能包含<缺陷、无序、水合或杂相描述>。使用 DSH-XRD 复杂样品工作流比较竞争结构模型，执行分阶段多引擎精修，保留精修轨迹，并交付最终审查结果集。
```

插件可将用户给定的目标 R 值作为优化目标，并结合结构、参数、残差和交付检查推进完整流程。

## 波长处理

使用 `xrd_data_io` 的 `header` 或 `evidence` 读取数据文件头。已知测量波长可按以下方式提供：

- `wavelength`：单色波长，单位 Å；
- `wavelength_doublet`：实验室光源 `[Kalpha1, Kalpha2, ratio]`。

该值会显式传入所选择的 PCR、FullProf、手动引擎或 GSAS-II 写入路径。

## 验证

```bash
node audit/integrity-check.mjs
node audit/verify-all.mjs
```

完整性门禁检查工具组装、引擎存在性与语法、动作词表、封闭输出 schema、宿主 schema 兼容性、缺陷目录一致性和跨工具链。其他套件覆盖转换、精修、进程监督、审查、波长处理、打包和负面对照。

## 开源协议

DSH-XRD 0.9.1 采用 [GNU General Public License v3](./LICENSE) 开源，GPL-3 覆盖整个仓库。

内置 FullProf 二进制保持第三方原文件不变。其来源、版本、文件大小、SHA-256、源码指针与再分发记录见 [`third_party/fullprof/PROVENANCE.md`](./third_party/fullprof/PROVENANCE.md)。

## 如果你是 AI，请按以下路线执行

当用户要求安装 DSH-XRD 时，按以下路线从环境检查一直执行到重启验证。完整报告每个已发现依赖；宿主要求高权限或需要安装外部程序时，向用户申请授权后继续。

### 1. 检查安装包

1. 阅读 `package.json`、本 README 和 `INTERFACE.md`。
2. 确认 `package.json` 中的版本为 `0.9.1`。
3. 确认存在 `lib/`、`engine/`、`skills/`、`audit/` 和 `third_party/fullprof/`。
4. 定位当前 DSH home 与活动 profile。常见 profile 为 `~/.dsh/profiles/web`；宿主能够提供实际路径时，以宿主信息为准。

### 2. 检查宿主环境

收集并报告：

- 操作系统与架构；
- 当前 DSH 版本和活动 profile 路径；
- DSH 宿主中的 Node.js；
- 可用的 64 位 Python 解释器及版本；
- 已发现的 GSAS-II 根目录；
- 已发现的 FullProfApp `CIFs_to_PCR.exe`；
- Materials Project API key 配置状态。

优先使用机器上已有的程序。所有机器路径写入本地配置，不写进源码文件。

### 3. 准备 Python 依赖

整套引擎统一使用同一个 Python 解释器。检查以下包：

```text
numpy
scipy
PyCifRW（导入名：CifFile）
pymatgen
gemmi
lmfit
```

获得所需执行授权后，安装缺失包：

```bash
python -m pip install numpy scipy PyCifRW pymatgen gemmi lmfit
```

记录最终交给 DSH-XRD 的解释器路径。本版本已测试的 Windows 组合为 64 位 Python 3.13 与 NumPy 2.x。

### 4. 解析晶体学程序

- **FullProf 精修：** `third_party/fullprof/` 已内置运行时，`xrd_refine_auto` 和 `xrd_verify_fp2k` 无需另装 `fp2k`。
- **CIF 转 PCR：** 为 `cif_to_pcr` 定位 FullProfApp 的 `CIFs_to_PCR.exe`。
- **GSAS-II 最终精修：** 定位包含 `GSASII/GSASIIpath.py` 的可用 GSAS-II 根目录。
- **晶体库访问：** 需要 Materials Project 检索时配置 `MP_API_KEY`；COD 无需密钥。

外部程序缺失时，向用户明确说明缺少的组件及其对应能力；用户提供或安装后继续同一路线。

### 5. 将插件安装到 DSH

将本仓库放置或链接到活动 profile 的插件目录，常见位置为：

```text
~/.dsh/profiles/web/node_modules/dsh-xrd
```

把以下内容合并进 profile 已有的 `package.json`：

```json
{
  "dependencies": {
    "dsh-xrd": "file:/absolute/path/to/dsh-xrd"
  },
  "dsh": {
    "profile": {
      "bundles": ["dsh-xrd"]
    }
  }
}
```

保留 profile 中已有的依赖、bundle、provider 和其他设置。

### 6. 生成本地配置

复制 `xrd.local.example.json` 为 `xrd.local.json`，填入本机已经发现的路径：

```json
{
  "python": "C:/Python313/python.exe",
  "gsas_root": "D:/GSAS-II",
  "fp2k_root": "D:/FullProf",
  "cif2pcr_exe": "C:/Program Files (x86)/FullProfAPP/resources/fpsuite/windows/CIFs_to_PCR.exe"
}
```

使用内置 FullProf 时会自动发现运行时，因此可以省略 `fp2k_root`。

对应环境变量：

- `DSH_XRD_PY`
- `DSH_GSAS_ROOT`
- `DSH_FP2K_ROOT`
- `DSH_FULLPROF_CIF2PCR`
- `DSH_XRD_CONFIG`
- `MP_API_KEY`

### 7. 设置 Linux 可执行权限

Linux 下为内置引擎设置可执行位：

```bash
chmod +x third_party/fullprof/fp2k
```

### 8. 执行安装检查

先运行静态完整性门禁：

```bash
node audit/integrity-check.mjs
```

确认组装出 12 个工具且结果为 `FAIL 0`。随后根据本机已经安装的外部程序运行完整审计：

```bash
node audit/verify-all.mjs
```

审计会把当前不可用的外部输入标为 `SKIP`，便于在最终报告中展示本机实际覆盖范围。

### 9. 重启并验证 DSH

重启 DSH，使 JavaScript 插件进入运行态。在新会话中：

1. 确认 12 个 XRD 工具全部注册；
2. 确认 6 个内置 skill 可见；
3. 调用一次轻量数据或 CIF 检查工具；
4. 汇报最终 Python、GSAS-II、FullProfApp、内置 FullProf 与 Materials Project 配置。

### 10. 提交安装报告

报告以下内容：

- 安装目录；
- 被修改的 DSH profile；
- 插件版本；
- Python 解释器与依赖包；
- 外部程序解析路径；
- 完整性审计结果；
- 已注册工具和 skill 数量；
- 仍等待用户提供的软件或凭据。

用户可以用下面的提示词启动 AI 自主安装：

```text
将本仓库中的 DSH-XRD 0.9.1 安装到我当前活动的 DeepSeek Harness profile。如果你是 AI，请从 README.md 的“如果你是 AI”路线开始，依次完成环境检查、依赖配置、插件安装、审计、重启验证，并在需要高权限或安装外部程序时向我申请授权。最后给出完整安装报告。
```

