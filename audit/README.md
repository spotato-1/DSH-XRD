# xrd-plugin-checks — dsh-xrd 自审工具集

改完 `dsh-xrd` 后的**标准验证动作**都在这里。全部是"真调用"脚本：导入插件工具入口、
用最小 mock ctx `apply()`、拿到工具后真的 `execute()`，再对返回值做断言——不是读代码猜结论。

本目录**随发布包一起分发**（包内路径 `audit/`），所以它不能依赖任何人的机器布局：
所有路径由 `_lib.mjs` 从脚本自身位置推导，或用环境变量指定。维护者专用的脚本
（`sync.mjs` / `sync-skills.mjs` / `three-copy-check.mjs`）**不进包**——前两者会写 dsh 的
profile/home，后者要比较"源码/profile/包"三处，只在开发布局里成立；收到的包不该有这些能力。

工具本身不需要重启 dsh；被它验证的**插件代码改动**需要重启后才生效。

---

## 一、顺序（改完插件就该这么走）

```powershell
# 1) 同步：源码 → profile 运行时副本（整树镜像 + SHA256 比对，两边必须一致）
node <仓库>/workspace/xrd-plugin-checks/sync.mjs
node <仓库>/workspace/xrd-plugin-checks/sync-skills.mjs
#   可用 --src/--dst/--home 或 DSH_XRD_SRC / DSH_XRD_PROFILE / DSH_HOME 指定位置

# 2) 打包后核验"三处一致"（这是三处的权威定义）
node <仓库>/workspace/xrd-plugin-checks/three-copy-check.mjs
#   ①源码 dsh-xrd ②profile 运行时副本 ③打包出的 xrd 插件（dist/dsh-xrd-<版本>）
#   规则：①==② 逐字节；③==sanitise(①)；脱敏是唯一合法差异。**注意 deploy-pack 不放 xrd 插件**
#   （它是迁移工作环境用的，只带 office/modlens/tools）

# 3) 重启 dsh（只有代码改动需要；技能正文改动不需要）

# 3) 逻辑链路与引擎完整性审计（改了大量输入/输出项之后必跑）
node audit/integrity-check.mjs              # 默认审"本模块所在的那棵树"（包内=审包自己）
node audit/integrity-check.mjs --root <dir> # 审别的树：解压出来的发布件、开发源码树…
node audit/integrity-check.mjs --profile    # 审 dsh 真正加载的那份（需 DSH_XRD_PROFILE）

# 4) 一次跑完自足部分
node audit/verify-all.mjs

# 5) 或逐个跑
node audit/verify-package.mjs           # 发布树内容（工具公告、CHANGELOG 口径、fp2k、审计模块自身）
node audit/verify-cif-edit.mjs          # xrd_cif_edit 各动作 + validate
node audit/verify-e2e.mjs               # build_orbit → cif_to_pcr → 精修链
node audit/verify-autofp-replaced.mjs   # xrd_refine_auto 走自带 fp2k 驱动
node audit/verify-fp2k-check.mjs        # xrd_verify_fp2k 共享 fp2kRoot() 探测
node audit/verify-gsas-ops.mjs          # gsas_ops 两相构建 + 混合位占据（需真工程）
node audit/verify-manual-fitstatus.mjs  # 手动引擎判读包（数分钟）
node audit/verify-manual-mapping.mjs    # 承接上一步的 result.json（没有则 SKIP）
node audit/verify-y2o3.mjs              # 真数据端到端；再跑 struct-check.mjs
```

`verify-y2o3.mjs` 写出 `.work/y2o3/Y2O3_orbit.pcr`，`struct-check.mjs` 消费它——**顺序反了会 SKIP**。
`verify-manual-mapping.mjs` 吃 `verify-manual-fitstatus.mjs` 写出的 `.work/manual/result.json`，
单独跑会 SKIP——它本来就是把工具层映射从数分钟精修里隔离出来的排查工具。
`verify-package.mjs` 在打包**之后**跑，需要 `--root <发布目录>`。

## 二、每个脚本在验什么

| 脚本 | 验证对象 | 关键断言 |
|---|---|---|
| `three-copy-check.mjs` | **三处一致**（维护者工具，不进包） | ①源码==②profile **逐字节**（含 `package.json` 版本号）；③包==**sanitise(①)**，脱敏表**从 `build-xrd-release.mjs` 读取而非复制**（复制过一次，泄漏审计实测抓到：真实样品名被写进包）；差异超出脱敏就**打印具体行**并 FAIL；缺构建脚本 exit=2 大声失败 |
| `integrity-check.mjs` | **逻辑链路 + 引擎完整性** | 六节：①工具装配 ②每个被声明的引擎存在且**可被 `ast.parse` 解析**（故意不用 `py_compile`——它会写 `__pycache__`，沙箱拒绝写权限时会把 10 个完好引擎误报为编译失败）③**动作词表双向核对**（工具公告的动作引擎都实现了 / 引擎实现的动作工具都公布了）④输出 schema 闭合 ⑤宿主 JSON-schema 关键字子集 ⑥六条跨工具链路 + fp2k 驱动入口。**0 FAIL 才算链路完整** |
| `verify-package.mjs` | **发布树内容**（只读，不执行） | 公告了四个新动作；CHANGELOG 带证据分级、删掉"全部已修复"式总括声明、显式点名未验证项、保留历史版本段；`fp2k`/`fp2k.exe` 都在；`xrd.local.json` 未泄漏；**审计模块自身已随包且不含维护者路径** |
| `verify-cif-edit.mjs` | `xrd_cif_edit` | `build_orbit` 只给 Wyckoff 代表点也能补全（K→3a、Mn→3b、O→6c）；`validate` 对好/坏 CIF 分别 pass/warn |
| `verify-e2e.mjs` | 端到端 | `build_orbit` 产出的 CIF 能进 `cif_to_pcr` 官方转换器，落成可精修的 pcr |
| `verify-autofp-replaced.mjs` | `xrd_refine_auto` 接口替换 | 11 个工具；别名 `autofp_refine` 已消失；参数 `pcr/ladder/cycles/out_dir`；旧参数 `path/autoselect`、旧字段 `par_summary/ladder_summary` 不得残留；真跑出 `0 < Rwp < 100` 且产物落在 `.fp2k-work-*` |
| `verify-fp2k-check.mjs` | `xrd_verify_fp2k` | 去重后仍能用共享 `fp2kRoot()` 探到 fp2k 并跑出 Rwp/density/cell |
| `verify-gsas-ops.mjs` | `xrd_gsas_ops` 的两相/混位动作 **＋ 真两步骤精修（0.8.6）** | 四个此前**只在引擎里存在、帮助文本从未公布**的动作，用真收敛工程跑通：`add_phase` 出 2 相且**都挂到同一 histogram**（**回读 gpx 确认**）、`set_site_composition` 把混合位写成同坐标额外原子行。位点标签是从工程里读出来的，不靠猜。**0.8.6 增补**：同一工程上跑一次真 `refine:['background','cell']`（用**出厂的 90 s 心跳窗口**，不是监督套件为 stall 用例压到 2.5 s 的那个），断言 `ok`、数值 `wR`、第 2 步之后的 cell、以及 tick 里的 `gsas refine step 1/2` 与 `self_assigned:true`。真跑断言放在这里，是因为那个 2.5 s 窗口会把合法精修判成停滞杀掉（实测：干净目录同一次调用 9.3 s 成功） |
| `verify-manual-fitstatus.mjs` | `xrd_refine_manual` 判读包 | `baseline_rwp` / `fit_status.success` / `r_plateau.is_plateau` 三个字段真的穿过工具层（`output.schema` 是 `additionalProperties:false`，未声明字段会整条结果被拒） |
| `verify-manual-mapping.mjs` | 工具层映射（隔离出来） | 直接吃上一步引擎写的 `result.json`，断言 `metrics.baseline_Rwp`、`fit_status.flags`、`r_plateau` 存在且形状正确 |
| `verify-result-freshness.mjs` | **P0（0.8.3）结果新鲜度** | 只有本轮写出的结果才可能被上报：`readFreshResult` 的每条拒绝分支（缺失／过期／`run_id` 不符／不可解析）、一次真实 `data_io`（plan 带 `run_id`、引擎回写、返回值不带），以及**在同一夹具上复现旧 bug 的负对照**；`--root` 指定插件树 |
| `verify-proc-supervision.mjs` | **P1/P2（0.8.3）＋0.8.4／0.8.5／0.8.6／0.8.7 阻断项回归** | 按后端分策略（手动／GSAS＝心跳，fp2k＝固定）；**慢但在推进的运行必须活过名义超时**；**tick 停掉必须按 `heartbeat-lost` 杀掉**；优雅停止＝引擎自己退出并留下结果；宿主 `subprocess` 分支与回落；fp2k 的 NaN 判死。**0.8.4**：abort 必须真终止、`init_engine` 复制件必须可 import、`let useService` 回落不抛。**0.8.5**：调用前已取消不得抛 TDZ、保险丝必须**高于名义预算**、`stopped` 必须是已声明字段、`xrd_verify_fp2k` 在 `.sum` 只有横幅无 R 值时必须失败、旧证据轮转 `.prev`、清扫必须校验镜像路径。**0.8.6**：fp2k 的 Rwp 必须取**末次**出现（§19 反解夹具自己的 `.out`/`.sum`：4 条 Rietveld 行／8 条 `Rwp:` 行，末条 81.1 而非首条 83.6）、引擎 `ok:false` 不得被包装成成功、不存在的显式 plan 必须报错、批处理失败索引必须**各不相同**且未知 action 计失败、`partial`＝"有动作成功"、真 fp2k 被自己的 `.xrd-stop` 停成 exit 97、9 个长引擎都必须有 checkpoint、`EngineStopped` 必须继承 `BaseException`。**0.8.7（共 181 项，§23）**：**宿主 `done` 永不落地时必须有界返回**（报告原样探针；~32 s 返回且 `exitObserved:false`）、**调用前已取消必须 0 ms 返回**、**真 progress tick 不得被普通 tick 吞**、**skill 镜像四态**（整树复制含 refs/templates、幂等、用户改过的保留、我们自己写的升级、无记录的历史安装升级并留 `.user-backup`）、**引擎结果必须是严格 JSON**（NaN→null 且 JS 能 parse）、**pcr 里的 `../` 名字不得在 workDir 外落盘**、**score-only 必须只跑一次 fp2k** 且 `ok:true` 带 Rwp。窗口用 `DSH_XRD_HEARTBEAT_STALL_MS`／`DSH_XRD_STOP_GRACE_MS` 缩短 —— 这个 2.5 s 窗口只适合静态与假引擎用例，**真 GSAS 精修的断言在 `verify-gsas-ops.mjs`** |
| `py/ctrl-fp2k-orphan.py` | fp2k 孤儿事故的**可判别对照** | 无防护→孙进程存活；有防护→随驱动死亡；清扫只杀无父者**且镜像路径必须是我们自己跑的那个二进制**（0.8.5 增 C2：同名但路径不同的孤儿必须被 `skipped_unverified` 放过而不是杀掉）。需 `DSH_XRD_SRC` 与可写的 `DSH_XRD_CTRL_WORK` |
| `verify-y2o3.mjs` | 真数据端到端 | 只用 Wyckoff 代表点重建 Y2O3 → 转换 → 自带 fp2k 驱动精修，链子必须走完 |
| `struct-check.mjs` | 结构正确性归因 | 保留已知良好的 pcr，只换原子块；Rwp 若维持参考水平，说明 CIF 来源结构没问题、此前的坏 Rwp 只是 pcr 格式/读取问题 |
| `py/check_reader.py` | pyfullprof 读取器 | 秒级判定某个 pcr 能否被 AutoFP 的读取器加载。**它是"这个读取器读不了转换器产出"这一结论的廉价证据**——拿项目自带参考 pcr 喂它也照样挂。需 `DSH_AUTOFP_DIR` |

## 三、故意不进包、需要自己提供的输入

包里**不下载任何东西**。三类输入因携带实测数据或第三方二进制而不随包，缺了会明确 **SKIP**
（而不是 FAIL）——"没跑"绝不会被当成"通过"。

| 输入 | 被谁需要 | 怎么给 |
|---|---|---|
| 收敛的 GSAS-II 工程（`.gpx`，至少一个 histogram） | `verify-gsas-ops.mjs` | `DSH_XRD_GPX=<路径>` |
| 参考 Y2O3 工程（`Y2O3.pcr` + `Y2O3.dat`） | `verify-y2o3.mjs`、`struct-check.mjs` | `DSH_XRD_Y2O3_REF=<目录>` |
| GSAS-II 本体 | `verify-gsas-ops.mjs` | `DSH_GSAS_ROOT=<目录>`，或插件旁 `xrd.local.json` 的 `gsas_root` |
| 官方 `CIFs_to_PCR.exe`（FullProfApp） | `verify-e2e.mjs` | `DSH_FULLPROF_CIF2PCR=<exe>`，或它公开的默认安装路径 |
| AutoFP 源码（pyfullprof） | `py/check_reader.py` | `DSH_AUTOFP_DIR=<目录>` |

## 四、环境变量

| 变量 | 含义 |
|---|---|
| `DSH_XRD_ROOT` | 要审的插件树；等同 `--root <dir>`。默认=本模块所在的那棵树 |
| `DSH_XRD_PROFILE` | dsh 的运行时副本（`.../node_modules/dsh-xrd`），用于审"真正加载的那份" |
| `DSH_XRD_PYTHON` | Python 3 解释器；不设则自动探测 |
| `DSH_XRD_GPX` / `DSH_XRD_Y2O3_REF` | 上表的外部输入 |
| `DSH_GSAS_ROOT` / `DSH_FP2K_ROOT` / `DSH_FULLPROF_CIF2PCR` / `DSH_AUTOFP_DIR` | 第三方程序位置 |

## 五、产物与注意

- 脚本把临时产物写在 `audit/.work/`（每次跑前自清），可随时整目录删除；**它不进包**。
- 除 `.work/` 外，脚本**只读**：不碰 `dsh-xrd` 源码，也不改被审的那棵树。
- 输出统一为 `PASS` / `FAIL` / `SKIP` 行 + 末行 `FAIL n | WARN n | SKIP n` + 结论；有 FAIL 则退出码非 0。
  **SKIP 不是通过**：带 SKIP 的运行会在末行明确标出，别把"没有 FAIL"读成"全都验过了"。

## 六、它已经抓到过的两类真问题

正是这个模块存在的理由，值得记住：

1. **`ladder:false` 仍然跑完整个阶梯**——`or 1` 把调用方的 `0` 当假值变成了 `1`。带 bug 时
   "快速给这个结构打个分"要花约 19 次 fp2k（18.6 s），实际只需 1 次（0.4 s）。
2. **实现了却从未公布的动作**——`add_phase` / `replace_phase` / `new_from` / `set_site_composition`
   在 `engine/gsas_ops.py` 里函数齐全，工具自身帮助文本一字未提，等于**不可达**（其中 `add_phase`
   正是两相建模的入口）。找出它们的是词表核对的**反向**那一半。
