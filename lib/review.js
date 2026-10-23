// lib/review.js — D19/D20 "复杂情况审查" 的**机械层**（切片 1，2026-09-26）
//
// 设计依据（用户定论，逐条对应 0.8.0-设计讨论.md D19.7/D19.9/D19.11/D19.15/D19.17）：
//   · 计数器**只数 GSAS 轮**（"GSAS-II 才是最终交付单元"）——一次 xrd_refine_gsas 调用 = 一轮；
//   · 机械栏（本模块写）与判断栏（AI 写）**分开文件**：review_mechanical.json / review_judgment.md；
//   · 等级 = **同一代号连续出现次数**（换代号从头算；收到 A 全部清零）；**等级 1 不注入**；
//   · **代号只做计数统计，等级才进对话** ⇒ 注入文本里**不得出现 A/B/C/D/E 字母**；
//   · **E（判断栏落后 ≥2 个 GSAS 轮）由插件机械判定**，不派生子代理（用户裁定六）；
//   · **冻结只对 B/C/D 且 level>=3**，E 永不冻结；冻结是**软门**（不改工具行为）；
//   · **人为接管 ⇒ 检测器关闭且不自动重启**，仅用户明说"继续/交回"才恢复并重置计数；
//   · 子代理**失败或超时 ⇒ 注入"检测失效"**；迟到 ⇒ 补送，主 agent idle 则改 followup。
//
// 本模块**不做**：派生/调用子代理（切片 2）、接管检测（切片 3）。它只负责记账、判 E、
// 组织注入文本、把文本送进主对话——全部是确定性逻辑，因此可用桩 agent 做代码层验证。
import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

export const MECH_FILE = 'review_mechanical.json'
export const JUDGMENT_FILE = 'review_judgment.md'

/** 每多少个 GSAS 轮派生一次审查（用户："GSAS-II 每第三次被启动时"）。 */
export const REVIEW_EVERY = 3
/** E 判据：判断栏落后于 GSAS 轮次达到这个数即提示（用户裁定四）。 */
export const E_LAG_THRESHOLD = 2
/** E 提示频次：首次 lag=2 一次，其后每再 +2 各一次（避免每轮都灌）。 */
export const E_REPEAT_STEP = 2
/** 审查子代理的默认超时预算（用户裁定六：60 s → 120 s）。 */
export const REVIEW_TIMEOUT_MS = 120_000

/** 代号语义（模板 1 的判据汇总）。E 不在子代理判据里——它由本模块机械判定。 */
export const CODE_MEANING = Object.freeze({
  A: '无问题',
  B: '线路问题（方向锚定）',
  C: '记忆丢失（用户输入或假设未被跟进）',
  D: '未触发复杂情况检查表',
  E: 'AI 未及时更新（判断栏落后 ≥2 个 GSAS 轮）',
})

const SUCCESS = 'OK'

// ────────────────────────────── 注入文本（模板 2/3） ──────────────────────────────
// 铁律：**文本里不出现 A/B/C/D/E 字母**（代号只做计数）。等级用 "n/3" 表示。
// ── 注入行的语言（用户 2026-09-27 定） ────────────────────────────────────────
// 注入行**中英双语**：两块各带语言标记，让 AI 一眼看出这是同一条内容的两块，
// 而不是两条独立指令（否则它会当成两件事各做一次）。标签只出现一次，保持可 grep 的锚点。
// 理由（用户原话）："两种语言也可以同时降低信息差，减少AI思考的时间"。
// 边界：**只做注入进对话的行**；§6 检查表正文与审查子代理提示词是纯给 AI 读的，保持中文不动。
export const LANG_MARK = Object.freeze({ zh: 'language-zh-', en: 'language-en-' })

/** 双语块：可选标签行 + 中文块 + 英文块。英文是意译，不是逐字直译。 */
function bi(zh, en, tag = null) {
  return (tag ? tag + '\n' : '') + `${LANG_MARK.zh} ${zh}\n${LANG_MARK.en} ${en}`
}

export const TEXT = Object.freeze({
  // 模板 3：每次精修后附（用户裁定三.4）
  statusReminder: bi(
    '机械栏已由插件更新（不要手改）。请更新判断栏文件：'
    + '① 前半段更新人工输入窗口里用户说过的话（原话，不转述）；'
    + '② 后半段更新：当前最佳物理解 / 未解决假设（逐条）/ branch 列表（含被冻结的）/ 当前冻结状态。'
    + '只写判断栏，不写机械栏。',
    'The plugin has already updated the mechanical ledger; do not edit it. Update the judgement file instead: '
    + '(1) first half = what the user actually said, in their own words; '
    + '(2) second half = current best physical solution, each unresolved hypothesis, '
    + 'the branch list (frozen branches included) and the current freeze state. '
    + 'Write the judgement file only.',
    '[状态表 · STATUS]'),
  // E（插件机械判定，永不冻结）
  eReminder: bi(
    '判断栏落后于精修引擎调用 ≥2 次。请立即更新判断栏；不冻结、不影响继续。',
    'The judgement file now trails the refinement engine by two or more calls. '
    + 'Update it now — no freeze, nothing else is blocked.',
    '[复杂情况审查 2/2 · REVIEW 2/2]'),
  eCap: bi(
    '判断栏持续未更新。请立即补写。不冻结——未及时更新不等于方向错误。',
    'The judgement file is still not being updated. Write it now. '
    + 'No freeze — falling behind is not the same as heading the wrong way.',
    '[复杂情况审查 · 封顶 / REVIEW · CAP]'),
  // 专用标记
  // (0.9.1) `invalid` was REMOVED from here: one sentence covered "skipped", a thrown
  // error and a real timeout, which is exactly how the 2026-10-05 TypeError (`signal`
  // missing) passed for a timeout for two versions while the budget never started.
  // invalidText() below builds the three cases separately — do not reintroduce it.
  released: bi(
    '解除冻结：已读检查表且已给出新路线。',
    'Freeze lifted — the checklist has been read and a new route has been laid out.',
    '[复杂情况审查 · REVIEW]'),
  detectorOff: bi(
    '检测器关闭：检测到对话被人为接管。',
    'Detector off — this conversation has been taken over by hand.',
    '[复杂情况审查 · REVIEW]'),
  detectorOn: bi(
    '检测器已恢复：计数已重置，从零开始重新计数。',
    'Detector back on — every counter is reset and counting starts over from zero.',
    '[复杂情况审查 · REVIEW]'),
  // B/C/D 注入文本（切片 2 使用；同样不含字母）
  b2: bi(
    '疑似方向锚定：反复同一路线压 R。保留竞争假设与重要分支，不要只沿当前路线继续。',
    'Suspected directional anchoring — the same route keeps being squeezed. '
    + 'Keep the rival hypotheses and the important branches alive; '
    + 'do not simply carry on down the current one.',
    '[复杂情况审查 2/3 · REVIEW 2/3]'),
  b3: bi(
    '冻结当前思路（软门）：停止沿现有路线惯性优化。此前对失败原因、缺陷类型与最优方向的判断'
    + '均视为暂时假设，不是既定事实。请按下面 §6 从多个竞争解释重评；冻结粒度由你决定。',
    'Freeze the current line of attack (soft gate): stop optimising along the existing route out of habit. '
    + 'Whatever has been concluded about why it fails, which defect is present and where the best direction lies '
    + 'is now provisional, not settled. Work through §6 below and re-weigh several competing explanations; '
    + 'you decide how far the freeze reaches.',
    '[复杂情况审查 3/3 · REVIEW 3/3]'),
  c2: bi(
    '关键状态可能已丢失：用户输入或未解决假设未被后续轮次跟进。'
    + '请先重读判断栏（含用户原话）与最近 journal，再继续。',
    'Key state may have been lost — something the user said, or an unresolved hypothesis, '
    + 'has not been carried into later rounds. Re-read the judgement file (the user\'s own words included) '
    + 'and the recent journal before going on.',
    '[复杂情况审查 2/3 · REVIEW 2/3]'),
  c3: bi(
    '冻结当前思路（软门）：先重建状态——逐条确认未解决假设的处置与用户输入的落实，'
    + '再决定是否继续。下面附 §6，按它重新评估。',
    'Freeze the current line of attack (soft gate). Rebuild the state first: go through each unresolved '
    + 'hypothesis and each point the user made, confirm how it was handled, and only then decide whether '
    + 'to continue. §6 follows; re-assess against it.',
    '[复杂情况审查 3/3 · REVIEW 3/3]'),
  d2: bi(
    '已达到应触发复杂情况检查表的条件却未调用。请现在就按下面 §6 排查。',
    'The conditions for the complex-case checklist have been met and it was never invoked. '
    + 'Work through §6 below now.',
    '[复杂情况审查 2/3 · REVIEW 2/3]'),
  d3: bi(
    '冻结当前思路（软门）：必须重新调用 §6 复杂情况检查表逐类排查后再继续优化。',
    'Freeze the current line of attack (soft gate). Re-run the §6 checklist class by class, '
    + 'and only then resume optimising.',
    '[复杂情况审查 3/3 · REVIEW 3/3]'),
  extremeHint: bi(
    '提示：若目标本身不可达，§6.5 极限模式是既有出口。',
    'Note — if the target itself is unreachable, §6.5 limit mode is the exit that already exists.'),
})

/**
 * 注入消息的 `source.summary` 标签（折叠行 + 持久日志上显示的那一行）。
 *
 * **为什么另写而不是截正文**：双语之后正文前 120 字符全是中文，截出来是个被切断的
 * 中文半句，标签就废了。这里每条给一个**专门写的双语短标签**。
 *
 * 上限说明（读宿主源码）：`dsh-llm` 的 `CONTEXT_SUMMARY_MAX_CHARS = 120` 是 `notice`
 * 摘要的**文档上限**，`boundContextSummary()` 是**截断 + 省略号**、不是拒绝；而它只被
 * `dsh-subagent` / `dsh-tool-jobs` / `dsh-tool-goal` 各自调用，**注入这条路径上宿主不做
 * 强制**。用户 2026-09-27 要求"适当放宽"，故取 200；但下面每一条实际都 ≤120，
 * 仍落在宿主文档上限内——放宽只是给将来的长标签留余量。
 */
export const SUMMARY_MAX_CHARS = 200
export const LABEL = Object.freeze({
  statusReminder: '[状态表 · STATUS] mechanical ledger updated · 机械栏已更新',
  eReminder: '[REVIEW 2/2] judgement file behind · 判断栏落后',
  eCap: '[REVIEW · CAP] judgement file still stale · 判断栏仍未更新',
  invalid: '[REVIEW] unavailable · 检测失效',
  released: '[REVIEW] freeze lifted · 解除冻结',
  detectorOff: '[REVIEW] detector off · 检测器关闭',
  detectorOn: '[REVIEW] detector on · 检测器恢复',
  b2: '[REVIEW 2/3] directional anchoring · 疑似方向锚定',
  b3: '[REVIEW 3/3] soft freeze · 软冻结当前思路',
  c2: '[REVIEW 2/3] key state may be lost · 关键状态可能已丢失',
  c3: '[REVIEW 3/3] soft freeze · 软冻结，先重建状态',
  d2: '[REVIEW 2/3] checklist never invoked · 未触发检查表',
  d3: '[REVIEW 3/3] soft freeze · 软冻结，须重跑检查表',
  extremeHint: '[REVIEW] limit mode is the existing exit · 极限模式是既有出口',
})

/**
 * "检测失效"注入文本 —— **失败来源必须能分辨**（2026-10-05 诊断）。
 *
 * `skipped`（子代理服务/提供方不可用）、真超时、以及**其它异常**过去共用一句
 * "子代理未在预算内返回"，于是"缺 `signal` 导致的 TypeError"被当成 120 s 超时排查了两版，
 * 而那个预算**根本没有开始计时**（失败发生在 15–18 ms）。文案不是诊断的替代品：
 * 三类各自说真话，标签保持不变（grep 锚点）。
 */
export function invalidText(r = {}, timeoutMs = REVIEW_TIMEOUT_MS) {
  const clip = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, 160)
  const skipped = r && r.skipped ? clip(r.skipped) : ''
  const err = r && r.error ? clip(r.error) : ''
  const tag = '[复杂情况审查 · REVIEW]'
  if (skipped && !err) {
    return bi(
      `检测未执行（子代理不可用：${skipped}；计数未变，可继续）`,
      `Review not run — the reviewer subagent is unavailable (${skipped}). `
      + 'The counters are unchanged; carry on.',
      tag)
  }
  const timedOut = !!err && /timed out|did not START/i.test(err)
  const zh = timedOut
    ? `子代理超时（预算 ${timeoutMs} ms）`
    : `子代理报错：${err || '未返回任何输出'}`
  const en = timedOut
    ? `the reviewer subagent timed out (budget ${timeoutMs} ms)`
    : `the reviewer subagent failed: ${err || 'it returned no output'}`
  return bi(
    `检测失效（${zh}；计数未变，可继续）`,
    `Review unavailable — ${en}. The counters are unchanged; carry on.`,
    tag)
}

// ────────────────────────────── 机械栏读写（严格 JSON，D7） ──────────────────────────────/** 空机械栏（字段即 D19.16 的 schema）。 */
export function emptyState(sample = null) {
  return {
    _schema: 'dsh-xrd/review-mechanical@1',
    sample,
    gsas_round_count: 0,
    review_due_every: REVIEW_EVERY,
    last_review_at_round: 0,
    rounds: [],                 // [{round, wR, ok, ts}]
    last_code: null,
    streaks: { B: 0, C: 0, D: 0 },
    levels: { B: 0, C: 0, D: 0 },
    judgment_updated_at_round: 0,
    judgment_mtime_ms: 0,
    e_last_injected_at_lag: 0,
    review_history: [],         // [{at_round, code, level, injected, ts}]
    frozen: { active: false, since_round: null, released_at_round: null, code: null },
    human_takeover: false,
    detector_enabled: true,
    last_user_seq: 0,
    takeover_history: [],
  }
}

// The mechanical state is keyed by DIRECTORY + SAMPLE (0.8.6). The artifact directory
// often defaults to the session workspace, so several samples written there shared one
// counter and one freeze state — one sample's review round could freeze another's line of
// attack. The state itself carries `sample`, so saveState needs no extra argument.
const safeKey = (v) => String(v || '').replace(/[^\w.-]+/g, '_').slice(0, 60)
function mechPath(dir, sample = null) {
  const k = safeKey(sample)
  return join(dir, k ? MECH_FILE.replace(/\.json$/, `_${k}.json`) : MECH_FILE)
}
export function judgmentPath(dir) { return join(dir, JUDGMENT_FILE) }

/**
 * 读机械栏。缺失 ⇒ 新建空状态；**损坏 ⇒ 明确报错而不静默重写**
 * （状态文件被写成非法 JSON 是已被记录过的真实事故，见 0.8.0 设计讨论 D7）。
 */
export function loadState(dir, { sample = null } = {}) {
  const p = mechPath(dir, sample)
  if (!existsSync(p)) return emptyState(sample)
  const raw = readFileSync(p, 'utf8')
  let st
  try { st = JSON.parse(raw) } catch (e) {
    throw new Error(`${MECH_FILE} is not valid JSON (${e.message}). Fix or delete it; `
      + 'the plugin will not silently overwrite a damaged state file.')
  }
  const merged = { ...emptyState(sample), ...st }
  // 0.8.9: a closure the PLUGIN caused itself is cleared on load, and persisted at once
  // (otherwise a state that is only loaded — never saved — would keep the detector off).
  if (repairFalseTakeover(merged)) {
    try { saveState(dir, merged) } catch { /* best effort: never break a refinement */ }
  }
  return merged
}

/**
 * A takeover closure that the plugin caused ITSELF (0.8.9).
 *
 * `takeover_history[].snippet` is the first 120 characters of the message that closed
 * the detector. When a closure was recorded while the only "user message" was the
 * plugin's own injected reminder — which arrives as `role:'user'`,
 * `source.kind:'plugin'` and therefore LOOKS like human input to a seq-based scan — that
 * closure is provably false, and the detector would otherwise stay off for that sample
 * for the rest of its life. Measured on the Y2O3 end-to-end run (2026-09-28): the
 * snippet was literally the status-table injection and `detector_enabled` flipped to
 * false from round 2 on.
 *
 * Only closures whose EVERY recorded snippet carries the plugin's bilingual marker
 * qualify, so a genuine human takeover is never released by this — a human message does
 * not contain `language-zh-` / `language-en-` (and if someone pastes the plugin's text
 * back by hand, the marker is exactly what identifies it as that text).
 *
 * @returns {boolean} true when the state was repaired
 */
function repairFalseTakeover(st) {
  if (!st || !st.human_takeover) return false
  const closes = (st.takeover_history || []).filter((h) => h && h.action === 'close')
  if (!closes.length) return false
  const allPlugin = closes.every((h) => {
    const s = String(h.snippet || '')
    return s.includes(LANG_MARK.zh) || s.includes(LANG_MARK.en)
  })
  if (!allPlugin) return false
  st.human_takeover = false
  st.detector_enabled = true
  st.streaks = { B: 0, C: 0, D: 0 }
  st.levels = { B: 0, C: 0, D: 0 }
  st.e_last_injected_at_lag = 0
  st.takeover_history = st.takeover_history || []
  st.takeover_history.push({
    action: 'auto-resume',
    at_round: st.gsas_round_count || 0,
    ts: new Date().toISOString(),
    reason: 'the recorded closure was caused by the plugin\'s own injected reminder (0.8.9)',
  })
  return true
}

/** 原子写（tmp + rename），避免半截文件。 */
export function saveState(dir, st) {
  mkdirSync(dir, { recursive: true })
  const p = mechPath(dir, st && st.sample)
  const tmp = p + '.tmp'
  writeFileSync(tmp, JSON.stringify(st, null, 1), 'utf8')
  renameSync(tmp, p)
  return p
}

/** 判断栏被 AI 重写 ⇒ 记录"更新到第几轮"（mtime 变化才认，避免空写也算）。 */
export function noteJudgmentWrite(dir, st) {
  const p = judgmentPath(dir)
  if (!existsSync(p)) return { updated: false, reason: 'judgment file absent' }
  const mt = statSync(p).mtimeMs
  if (mt > (st.judgment_mtime_ms || 0)) {
    st.judgment_mtime_ms = mt
    st.judgment_updated_at_round = st.gsas_round_count
    return { updated: true, at_round: st.gsas_round_count }
  }
  return { updated: false, reason: 'unchanged mtime' }
}

// ────────────────────────────── 计数与等级 ──────────────────────────────
/** 就地记一轮（不落盘；调用方负责 saveState —— 单一读写循环，防覆盖）。 */
function bumpRound(st, { wR = null, ok = true, ts = new Date().toISOString() } = {}) {
  st.gsas_round_count = (st.gsas_round_count || 0) + 1
  st.rounds = st.rounds || []
  st.rounds.push({ round: st.gsas_round_count, wR, ok: ok !== false, ts })
  return st.gsas_round_count
}

/** 一轮是否触发审查（每 REVIEW_EVERY 轮、且这一轮还没审过、且检测器开着）。 */
function isDue(st) {
  return !!(st.detector_enabled && !st.human_takeover
    && st.gsas_round_count % (st.review_due_every || REVIEW_EVERY) === 0
    && st.gsas_round_count > (st.last_review_at_round || 0))
}

/**
 * 记一轮 GSAS 并落盘（**只应由 xrd_refine_gsas 调用**，用户裁定：只数 GSAS 轮）。
 * @returns {{state, round, due, lag, eDue}}
 */
export function recordGsasRound(dir, info = {}, opts = {}) {
  const st = loadState(dir, opts)
  bumpRound(st, info)
  const lag = judgeStaleness(st)
  const due = isDue(st)
  saveState(dir, st)
  return { state: st, round: st.gsas_round_count, due, lag, eDue: lag >= E_LAG_THRESHOLD }
}

/** 判断栏落后于 GSAS 轮次多少轮。 */
export function judgeStaleness(st) {
  return Math.max(0, (st.gsas_round_count || 0) - (st.judgment_updated_at_round || 0))
}

/**
 * E 判定（**纯机械，不派生子代理**）：lag ≥ 2 时提示；首次于 lag=2，其后每 +2 各一次。
 * @returns {{inject: boolean, lag: number, text: string|null, capped: boolean}}
 */
export function evaluateE(st) {
  const lag = judgeStaleness(st)
  if (!st.detector_enabled || st.human_takeover) return { inject: false, lag, text: null, capped: false }
  if (lag < E_LAG_THRESHOLD) return { inject: false, lag, text: null, capped: false }
  const last = st.e_last_injected_at_lag || 0
  if (lag < last + E_REPEAT_STEP) return { inject: false, lag, text: null, capped: lag > last }
  return { inject: true, lag, text: lag >= E_LAG_THRESHOLD + E_REPEAT_STEP ? TEXT.eCap : TEXT.eReminder, capped: lag > E_LAG_THRESHOLD }
}

/**
 * 更新某代号的连续计数与等级（用户裁定五：同一代号连续出现才 +1；换代号该代号从头算；A 全部清零）。
 * 代号 ∈ A/B/C/D（E 由插件判定，不走这里）。
 */
export function applyCode(st, code) {
  const c = String(code || '').trim().toUpperCase()
  if (c === 'A') {
    st.streaks = { B: 0, C: 0, D: 0 }
    st.levels = { B: 0, C: 0, D: 0 }
    st.last_code = 'A'
    return { code: 'A', level: 0, cleared: true }
  }
  if (!(c in st.streaks)) throw new Error(`unknown review code "${code}" (expected A/B/C/D)`)
  if (st.last_code && st.last_code !== c) {
    // 连续被打断 ⇒ **所有**代号计数从头算（"同一代号连续出现才 +1"）
    st.streaks = { B: 0, C: 0, D: 0 }
    st.levels = { B: 0, C: 0, D: 0 }
  }
  st.streaks[c] = (st.streaks[c] || 0) + 1
  st.levels[c] = st.streaks[c]
  st.last_code = c
  return { code: c, level: st.levels[c], cleared: false }
}

/** 某代号当前是否应当注入（level>=2），以及是否应当软冻结（B/C/D 且 level>=3）。 */
export function codeOutcome(st, code) {
  const c = String(code || '').toUpperCase()
  const level = (st.levels && st.levels[c]) || 0
  const injectable = level >= 2
  const freeze = injectable && level >= 3 && (c === 'B' || c === 'C' || c === 'D')
  return { code: c, level, injectable, freeze }
}

/** 软冻结记账（**不改任何工具行为**；只写状态 + 由注入文本提示）。 */
export function markFrozen(st, code, round) {
  st.frozen = { active: true, since_round: round, released_at_round: null, code }
  return st.frozen
}

/** 释放条件（用户："下次触发时发现 AI 读取了检查表 + 设计了新的路线"）由调用方判定后置位。 */
export function markReleased(st, round) {
  st.frozen = { active: false, since_round: st.frozen?.since_round ?? null, released_at_round: round, code: null }
  return st.frozen
}

// ────────────────────────────── 注入路由 ──────────────────────────────
/**
 * 注入的消息必须是**完整的 `UserMessage`**。
 *
 * ⚠ 0.8.0 实测缺陷（2026-09-27 真实宿主，第一次跑真实 GSAS 轮时抓到）：
 * 原先只发 `{role:'user', content:[…]}`，宿主在处理 inbox 时读 `message.source.kind`
 * ⇒ `TypeError: Cannot read properties of undefined (reading 'kind')`，
 * **整轮（turn）以 `kind:'error'` 结束**（日志：`turn/end {reason:{kind:'error', error:{message:"Cannot read properties of undefined (reading 'kind')"}}}`）。
 * 桩 agent 的测试**永远看不见**这个缝——只有把消息真的塞进宿主 inbox 才会走那段代码。
 *
 * 契约（读宿主源码，非推断）：`dsh-llm/lib/types/message.d.ts:120-133` —— `id` / `role` /
 * `content` / `source` 四者皆必填；`:94-101` —— 插件来源是
 * `{ kind: 'plugin', plugin: <name> } & ContextFormed`，而 `ContextFormed` 的 `notice`
 * 形是 `{ form: 'notice', summary }`。
 *
 * summary 的界（2026-09-27 读源码更正）：`dsh-llm` 的 `CONTEXT_SUMMARY_MAX_CHARS = 120`
 * 是**文档上限**，`boundContextSummary()` 是**截断加省略号、不是拒绝**，且它只被
 * `dsh-subagent` / `dsh-tool-jobs` / `dsh-tool-goal` 各自调用——**注入这条路径上宿主不做强制**。
 * 这里用 `SUMMARY_MAX_CHARS`（见 `LABEL`）并在有标签时优先用标签（标签短且是写死的）。
 * @param {string} text - 注入正文（完整进对话，永不截断）
 * @param {string} [plugin] - 来源插件名
 * @param {string|null} [summary] - 折叠行/日志上显示的标签；缺省则由正文压成一行
 * @returns 宿主可直接 splice 进 inbox 的用户消息
 */
export function injectedMessage(text, plugin = 'dsh-xrd', summary = null) {
  const line = summary != null ? String(summary) : String(text).replace(/\s+/g, ' ').trim()
  const bounded = line.length <= SUMMARY_MAX_CHARS ? line : line.slice(0, SUMMARY_MAX_CHARS - 1) + '…'
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin, form: 'notice', summary: bounded },
  }
}

/**
 * 把文本送进主对话。**不改变工具返回值** ⇒ 不触碰 output schema
 * （`additionalProperties:false` 下任何未声明字段都会被宿主拒绝，见 2026-09-21 记录）。
 * 正常走 `inject`（= next-step，下一步思考前消费）；主 agent 已 idle ⇒ `followup`（唤醒，保证送达）。
 * @param {string} [summary] - 见 {@link injectedMessage}；每条注入都传 `LABEL.*`
 * @returns {'inject'|'followup'|'unavailable'}
 */
export function deliver(agent, text, summary = null) {
  if (!agent || typeof text !== 'string' || !text) return 'unavailable'
  try {
    const idle = typeof agent.status === 'string' && agent.status === 'idle'
    // 每次投递都构造新消息（宿主会 freeze 消息对象，复用同一实例不安全）
    if (!idle && typeof agent.inject === 'function') { agent.inject(injectedMessage(text, 'dsh-xrd', summary)); return 'inject' }
    if (typeof agent.followup === 'function') { agent.followup(injectedMessage(text, 'dsh-xrd', summary)); return 'followup' }
    if (typeof agent.inject === 'function') { agent.inject(injectedMessage(text, 'dsh-xrd', summary)); return 'inject' }
    return 'unavailable'
  } catch {
    return 'unavailable'
  }
}

// ────────────────────────────── 切片 2：审查子代理 ──────────────────────────────
// 用户定论：**从主对话派生**（同 provider/model）、**极小读入**（只读状态包/日志）、
// 读取前给指导性提示词、只回一个代号、失败/超时注入"检测失效"、**独立时序**
// （后台跑，迟到先补送、idle 改 followup）、超时预算 120 s。
//
// provider 选 `spawn`（全新子代理 + 我们喂状态包）：`fork` 会继承"可能已被压缩的主对话"，
// 正是设计里明确要避免的（"不能只复制已经被压缩过的主对话"）。
export const REVIEW_PROVIDER_DEFAULT = 'spawn'

/** 模板 1：派生前给子代理的完整提示词（判据四类；E 由插件机械判定，不在此列）。 */
export const PROMPT_TEMPLATE = `你是本次 XRD 精修的临时复杂情况复核单元。你不是执行者：不改结构、不跑引擎、不写任何文件。
你的唯一输出是**一个代号**。不要输出解释、建议、R 值清单或任何分析文字。

【阅读指令】下面每一块数据读取时，按该块末尾的"看什么"来判断，不要只做摘要。

① 机械栏（插件写的运行流水：轮次 / 引擎 / R / 是否撞界 / 是否发散 / 冻结与接管标记）
  看什么：R 是否在同一路线内反复不降或来回抖动；是否只有单一引擎被反复使用；
          "撞界/发散"是否被继续沿用；相邻轮参数族差异是否几乎为零（原地打转）。

② 判断栏（主 AGENT 自己写的自述；前半段＝人工输入窗口里用户说过的话）
  看什么：用户的输入是否被后续轮次实际处理；列的"未解决假设"是否长时间没有对应轮次；
          branch 列表是否只剩一条（过早收敛）；自述的成功结论是否有机械栏的轮次支撑。

③ 精修日志（阶段切换 / 复杂情况检查表是否被调用过及何时 / 是否进过极限模式 / 每次判读的 trigger+why）
  看什么：复杂情况检查表是否触发过；触发后是否有对应动作；是否存在"跳过检查表直接换参数"。

【判据】只判下面四类之一（同类信息反复出现由插件计数，你不需要分级）：
  A 无问题       —— 路线在被检验、假设在被处理、检查表按需调用；没有值得打断主流程的问题。
  B 线路问题     —— 疑似方向性锚定：反复在同一路线压 R / 只用一个引擎 / 排除理由不成立。
  C 记忆丢失     —— 关键信息在长上下文中已丢失或被忽略：用户输入未被处理、未解决假设未被跟进、
                    检查表触发过但后续轮次无对应动作。
  D 未触发检查表 —— 已达到应触发复杂情况检查表的条件（多次无改善 / 物理异常 / 第二相 / 高缺陷…）却从未调用。

【输出】只输出一个字母：A / B / C / D。若你无法完成读取，输出：检测失效

────────────── 当前任务状态包 ──────────────
`

/** 从 skill 里抽出 §6 全文（复杂情况检查表）——Severe 时要注入的就是它。 */
export function extractChecklist(skillPath) {
  if (!skillPath || !existsSync(skillPath)) return null
  const lines = readFileSync(skillPath, 'utf8').split('\n')
  const start = lines.findIndex(l => /^## 6\./.test(l))
  if (start < 0) return null
  let end = lines.length
  for (let i = start + 1; i < lines.length; i++) if (/^## 7\./.test(lines[i])) { end = i; break }
  return lines.slice(start, end).join('\n').trim()
}

/** 组装状态包（极小读入）：机械栏 + 判断栏 + 日志尾部。 */
export function buildReviewPrompt({ st, judgment = '', journalTail = '' } = {}) {
  const mech = JSON.stringify({
    gsas_round_count: st.gsas_round_count,
    recent_rounds: (st.rounds || []).slice(-12),
    review_history: (st.review_history || []).slice(-6),
    frozen: st.frozen,
    detector_enabled: st.detector_enabled,
    human_takeover: st.human_takeover,
  }, null, 1)
  return PROMPT_TEMPLATE
    + '\n--- 机械栏（review_mechanical.json 摘要）---\n' + mech
    + '\n\n--- 判断栏（review_judgment.md）---\n' + String(judgment || '(尚未建立)').slice(0, 8000)
    + (journalTail ? '\n\n--- 精修日志（尾部）---\n' + String(journalTail).slice(-4000) : '')
}

/** 解析子代理的输出：只认一个独立字母 A–D；其余（含"检测失效"）一律视为无效。 */
export function parseCode(text) {
  if (typeof text !== 'string') return null
  const t = text.trim()
  const first = t.split('\n').map(s => s.trim()).filter(Boolean)[0] || ''
  const m = /^(?:代号[:：]\s*)?([ABCD])(?![A-Za-z])/.exec(first)
  return m ? m[1] : null
}

/** 由代号+等级决定注入文本（**不含代号字母**）与是否软冻结；level 3 附带 §6 与极限模式提示。 */
export function outcomeText(st, code, { checklist = null } = {}) {
  const { level, injectable, freeze } = codeOutcome(st, code)
  if (!injectable) return { text: null, level, freeze, injectable, labelKey: null }
  const c = String(code).toUpperCase()
  const L2 = { B: TEXT.b2, C: TEXT.c2, D: TEXT.d2 }[c]
  const L3 = { B: TEXT.b3, C: TEXT.c3, D: TEXT.d3 }[c]
  let text = (level >= 3 ? L3 : L2) || null
  if (text && level >= 3) {
    text += '\n' + TEXT.extremeHint
    // §6 正文本身保持中文（纯给 AI 读，用户 2026-09-27 明示不做），只把这个分隔行做成双语
    if (checklist) text += '\n──────────── 以下为完整检查表 §6 · full checklist §6 follows ────────────\n' + checklist
  }
  // 折叠行/日志用的标签键：注入哪一条就配哪一个（b2/b3/c2/c3/d2/d3）
  const labelKey = text ? `${c.toLowerCase()}${level >= 3 ? 3 : 2}` : null
  return { text, level, freeze, injectable, labelKey }
}

/**
 * 派生审查子代理并等它返回（**带超时**）。ctx 可注入以便代码层测试；
 * 真实运行时由工具侧传入插件 ctx（缺 `subagents` 服务则优雅跳过）。
 * @returns {{ok: boolean, code: string|null, raw: string, error?: string, skipped?: string}}
 */
export async function startReviewAndWait({
  ctx = null, parent = null, prompt, provider = REVIEW_PROVIDER_DEFAULT,
  timeoutMs = REVIEW_TIMEOUT_MS, agentOptions = undefined,
} = {}) {
  const subagents = ctx && typeof ctx.get === 'function' ? ctx.get('subagents') : (ctx?.subagents ?? null)
  if (!subagents || typeof subagents.start !== 'function') return { ok: false, code: null, raw: '', skipped: 'subagents service unavailable' }
  if (!parent) return { ok: false, code: null, raw: '', skipped: 'no parent agent' }
  if (typeof subagents.getProvider === 'function' && !subagents.getProvider(provider)) {
    return { ok: false, code: null, raw: '', skipped: `subagent provider "${provider}" not registered` }
  }
  // ── caller signal（2026-10-05 诊断的真因） ──────────────────────────────────
  // 宿主的 in-process 驱动**无条件**读 `request.signal.aborted`
  // （dsh-subagent-in-process-driver/lib/index.js:160-162），所以不带 `signal` 的请求
  // 会在 ~15 ms 内以 `TypeError: Cannot read properties of undefined (reading 'aborted')`
  // 死掉。本部署注册的两个 provider（spawn / fork）**都转调同一个驱动函数**，换 provider
  // 只会换一行栈 ⇒ 必须由调用方给 signal。
  // 用**我们自己的** controller，不用 `exec.signal`：审查是独立时序的后台任务，工具调用
  // 一返回就取消它，等于审查永远跑不完。abort 只在**我们放弃等待的那一刻**发生（见下），
  // 于是"插件不等了"与"子代理停下来"同刻，不会留下跑飞的子代理。
  const abort = new AbortController()
  let run = null
  try {
    // The START call itself can hang (a provider that never answers), and it used to be
    // awaited without any ceiling (0.8.6 — the third review's finding): the whole review
    // path then blocked forever, timer or not, because the timer only covered `result`.
    const startTimer = new Promise(res => {
      const t = setTimeout(() => res({ __startTimeout: true }), timeoutMs)
      if (t.unref) t.unref()
    })
    run = await Promise.race([subagents.start(provider, {
      label: '复杂情况审查',
      prompt: [{ type: 'text', text: prompt }],
      parent,
      signal: abort.signal,
      ...(agentOptions ? { agentOptions } : {}),
      toolFilter: { allow: ['read', 'glob', 'grep'] },
      maxDepth: 1,
    }), startTimer])
    if (run && run.__startTimeout) {
      try { abort.abort() } catch { /* ignore */ }
      return { ok: false, code: null, raw: '', error: `review subagent did not START within ${timeoutMs} ms` }
    }
  } catch (e) {
    try { abort.abort() } catch { /* ignore */ }
    return { ok: false, code: null, raw: '', error: String(e && e.message || e) }
  }
  const timer = new Promise(res => { const t = setTimeout(() => res({ timeout: true }), timeoutMs); if (t.unref) t.unref() })
  const settled = await Promise.race([Promise.resolve(run.result).catch(e => ({ error: String(e && e.message || e) })), timer])
  // 我们放弃等待 ⇒ 同刻取消子代理（成功返回时 run 已结束，不需要也不能提前 abort）。
  if (settled && settled.timeout) {
    try { abort.abort() } catch { /* ignore */ }
  }
  // dispose() may also never settle; a leaked child agent must not block the tool call
  // (0.8.6), so it is raced against a short fixed ceiling.
  try {
    if (run && typeof run.dispose === 'function') {
      const disposeTimer = new Promise(res => {
        const t = setTimeout(() => res('timeout'), 10_000)
        if (t.unref) t.unref()
      })
      await Promise.race([Promise.resolve(run.dispose()).catch(() => 'error'), disposeTimer])
    }
  } catch { /* ignore */ }
  if (settled && settled.timeout) return { ok: false, code: null, raw: '', error: `review subagent timed out after ${timeoutMs} ms` }
  if (settled && settled.error) return { ok: false, code: null, raw: '', error: settled.error }
  const text = (settled?.output ?? []).filter(b => b && b.type === 'text').map(b => b.text).join('\n')
  const code = parseCode(text)
  return { ok: !!code, code, raw: text }
}

/** 读状态包文件（极小读入：判断栏 + 日志尾部）。 */
function readPackage(dir) {
  let judgment = ''
  try { if (existsSync(judgmentPath(dir))) judgment = readFileSync(judgmentPath(dir), 'utf8') } catch { /* ignore */ }
  let journalTail = ''
  try {
    const jp = join(dir, 'journal.jsonl')
    if (existsSync(jp)) journalTail = readFileSync(jp, 'utf8').split('\n').filter(Boolean).slice(-40).join('\n')
  } catch { /* ignore */ }
  return { judgment, journalTail }
}

/**
 * 后台跑一次审查并处理结果（**不阻塞工具**）。所有失败都收敛为"检测失效"注入。
 * 结果落地前会重新加载状态：期间若已被接管/关闭 ⇒ 静默丢弃。
 */
export async function runReviewInBackground({
  ctx = null, parent = null, dir, pluginRoot = null, provider = REVIEW_PROVIDER_DEFAULT,
  timeoutMs = REVIEW_TIMEOUT_MS, agentOptions = undefined, logger = null, sample = null,
} = {}) {
  // The SAME state file the round was recorded in (0.8.7): onGsasRound() loads with the
  // sample key, so loading without it here read a DIFFERENT (unkeyed) file and wrote the
  // verdict of a per-sample review into it — the fourth review measured sample-A's history
  // staying empty while the verdict landed in the global state.
  const st0 = loadState(dir, { sample })
  const { judgment, journalTail } = readPackage(dir)
  const prompt = buildReviewPrompt({ st: st0, judgment, journalTail })
  const atRound = st0.gsas_round_count
  const r = await startReviewAndWait({ ctx, parent, prompt, provider, timeoutMs, agentOptions })

  const st = loadState(dir, { sample })   // 重新加载：期间可能被接管、或又有新轮
  if (!st.detector_enabled || st.human_takeover) return { discarded: 'detector off' }

  if (!r.ok) {
    st.review_history = st.review_history || []
    st.review_history.push({ at_round: atRound, code: null, invalid: true, reason: r.skipped || r.error || 'unparsable', ts: new Date().toISOString() })
    const how = deliver(parent, invalidText(r), LABEL.invalid)
    saveState(dir, st)
    return { invalid: true, reason: r.skipped || r.error, delivered: how }
  }

  const verdict = applyCode(st, r.code)
  const out = outcomeText(st, r.code, { checklist: extractChecklist(join(pluginRoot || '', 'skills', 'xrd-workflow', 'SKILL.md')) })
  let how = null
  if (out.text) how = deliver(parent, out.text, out.labelKey ? LABEL[out.labelKey] : null)
  if (out.freeze) markFrozen(st, r.code, st.gsas_round_count)
  st.review_history = st.review_history || []
  st.review_history.push({ at_round: atRound, code: r.code, level: verdict.level, injected: !!out.text, freeze: !!out.freeze, delivered: how, ts: new Date().toISOString() })
  if (st.frozen && st.frozen.active) st.frozen.last_review_at_round = atRound
  saveState(dir, st)
  if (logger) logger(`[xrd-review] ${r.code} level=${verdict.level} freeze=${!!out.freeze} injected=${!!out.text} via=${how}`)
  return { code: r.code, level: verdict.level, freeze: !!out.freeze, injected: !!out.text, delivered: how }
}

/**
 * 释放检测（用户："下次触发时发现 AI 读取了检查表 + 设计了新的路线"）。
 * 机械判据：冻结之后**判断栏**里同时出现"已读检查表"与"新路线"两处标记（模板 3 会要求 AI 写）。
 */
export function detectRelease(dir, st) {
  if (!st.frozen || !st.frozen.active) return { released: false }
  let j = ''
  try { if (existsSync(judgmentPath(dir))) j = readFileSync(judgmentPath(dir), 'utf8') } catch { return { released: false } }
  const ok = /已读检查表/.test(j) && /新路线/.test(j)
  if (!ok) return { released: false }
  markReleased(st, st.gsas_round_count)
  return { released: true }
}


/**
 * `xrd_refine_gsas` 每轮结束调用一次：记一轮 → 判 E → 注入（E 或状态表提醒）。
 * 切片 2 会在这里按 `due` 派生审查子代理。
 * **永不抛异常打断精修**：任何内部错误都降级为返回 { error }。
 */
// ────────────────────────────── 切片 3：人为接管 ⇒ 检测器关闭 ──────────────────────────────
// 用户定论："**检测到对话被人为接管后，这个检测器自动关闭**"；"是指这个检查器关闭或
// **不再次启用**……唯一恢复＝用户明说'继续/交回'"；范围＝**该样品**。
//
// 判据（机械、可测）：
//   · **主**：该样品精修**期间**出现新的用户消息（首轮的"任务提示"不算——首轮只做基线）；
//   · **辅**：用户消息里明说接管（我来/我自己/手动/接管/交给我…）。
// 恢复：用户消息里明说"继续/交回/接着/恢复" ⇒ 打开并**重置全部计数**。
const RE_TAKEOVER = /(我来|我自己|手动|接管|交给我|我接)/
const RE_RESUME = /(继续|交回|接着|恢复)/

/** 从一条 user/message 事件里取出文本（宽容：递归找 text 字段）。 */
function userText(ev) {
  const out = []
  const walk = (v, depth) => {
    if (depth > 6 || v == null) return
    if (typeof v === 'string') { out.push(v); return }
    if (Array.isArray(v)) { for (const x of v) walk(x, depth + 1); return }
    if (typeof v === 'object') {
      if (typeof v.text === 'string') out.push(v.text)
      else for (const k of Object.keys(v)) walk(v[k], depth + 1)
    }
  }
  walk(ev?.data ?? ev, 0)
  return out.join('\n')
}

/**
 * Is this session event something the PLUGIN injected rather than a human message (0.8.9)?
 *
 * Two independent tells, either is enough:
 *   · the host's own marker — an injected message carries `source.kind === 'plugin'`
 *     (see injectedMessage above; the contract was read from dsh-llm's message type);
 *   · the plugin's bilingual marker — every injected template is built by `bi()` and
 *     therefore contains `LANG_MARK.zh` AND `LANG_MARK.en`.
 * The text check is the fallback for a host that does not surface `source` on the event.
 */
function isPluginEvent(ev) {
  const d = ev?.data ?? ev ?? {}
  for (const src of [d.source, d.message?.source, ev?.source]) {
    if (src && typeof src === 'object' && String(src.kind || '') === 'plugin') return true
  }
  const t = userText(ev)
  return t.includes(LANG_MARK.zh) && t.includes(LANG_MARK.en)
}

/**
 * 扫描本次会话的 user/message。**只判"精修期间新到的"用户消息**（首轮只做基线）。
 *
 * 0.8.9: the plugin's OWN injections are `user/message` events too (`agent.inject` sends
 * `role:'user'` with `source.kind:'plugin'`), so a seq-based scan read its own reminder
 * back as human input and closed the detector — measured on the Y2O3 end-to-end run:
 * `takeover_history[0].snippet` was the status-table text and `detector_enabled` went
 * false from round 2 onwards. Injected events are now filtered out BEFORE the newest-seq
 * bookkeeping, so `last_user_seq` still tracks the newest HUMAN message.
 *
 * @returns {{takeover: boolean, resumed: boolean, changed: boolean, reason?: string}}
 */
export function scanHumanTakeover(agent, st) {
  const events = agent?.session?.events
  if (!Array.isArray(events)) return { takeover: false, resumed: false, changed: false }
  const last = st.last_user_seq || 0
  const fresh = events.filter(e => e?.type === 'user/message' && Number(e.seq || 0) > last
    && !isPluginEvent(e))
  if (!fresh.length) return { takeover: false, resumed: false, changed: false }
  st.last_user_seq = Math.max(...fresh.map(e => Number(e.seq || 0)))
  const text = fresh.map(userText).join('\n')

  // 已关闭时：只有明说"继续/交回"才恢复（不自动重启）
  if (st.human_takeover) {
    if (RE_RESUME.test(text)) {
      st.human_takeover = false
      st.detector_enabled = true
      st.streaks = { B: 0, C: 0, D: 0 }
      st.levels = { B: 0, C: 0, D: 0 }
      st.e_last_injected_at_lag = 0
      st.last_review_at_round = st.gsas_round_count   // 避免立刻触发
      st.takeover_history = (st.takeover_history || [])
      st.takeover_history.push({ action: 'resume', at_round: st.gsas_round_count, ts: new Date().toISOString() })
      return { takeover: false, resumed: true, changed: true, reason: 'user asked to resume' }
    }
    return { takeover: false, resumed: false, changed: false }
  }

  // 首轮：只吸收基线，不判定（任务提示不是"接管"）
  if ((st.gsas_round_count || 0) <= 1) return { takeover: false, resumed: false, changed: false }
  if (st.detector_enabled === false) return { takeover: false, resumed: false, changed: false }

  const explicit = RE_TAKEOVER.test(text)
  st.human_takeover = true
  st.detector_enabled = false
  st.takeover_history = (st.takeover_history || [])
  st.takeover_history.push({
    action: 'close', at_round: st.gsas_round_count, explicit,
    snippet: text.replace(/\s+/g, ' ').slice(0, 120), ts: new Date().toISOString(),
  })
  return { takeover: true, resumed: false, changed: true, reason: explicit ? 'user said they take over' : 'a user message arrived mid-refinement' }
}

// ────────────────────────────── 工具侧的单一挂钩 ──────────────────────────────
/** 每个"样品 + 目录"同时只允许一个在飞的审查（避免每 3 轮的触发堆积）。 */
const INFLIGHT = new Map()

// 0.8.7: the key is dir + sample, matching the state file. Keying by directory alone made
// two samples refining into one artifact directory look like ONE review in flight, so the
// second sample's review was skipped as "already running".
const inflightKey = (dir, sample = null) => `${dir}::${sample || ''}`

export function reviewInFlight(dir, sample = null) { return INFLIGHT.has(inflightKey(dir, sample)) }

/**
 * `xrd_refine_gsas` 每轮结束调用一次：记一轮 → 判 E → 注入（E 或状态表提醒）→
 * 到期则**后台**派生审查子代理（切片 2）。**永不抛异常打断精修**。
 */
export function onGsasRound({ dir, agent = null, wR = null, ok = true, ts, sample = null } = {},
  { ctx = null, pluginRoot = null, provider = REVIEW_PROVIDER_DEFAULT,
    timeoutMs = REVIEW_TIMEOUT_MS, agentOptions = undefined, logger = null } = {}) {
  try {
    // 单一读写循环：加载 → 更新判断栏戳 → 记一轮 → 判 E → 一次保存。
    // （早先版本在这里 load 两次、save 两次，第二次把判断栏戳覆盖回了旧值 ——
    //   集成测试 verify-review-detector.mjs 抓到，已修。）
    const st = loadState(dir, { sample })
    const note = noteJudgmentWrite(dir, st)
    bumpRound(st, { wR, ok, ts })
    const lag = judgeStaleness(st)
    const due = isDue(st)

    // 切片 3：人为接管检测（必须在"关闭即静默"之前，否则永远恢复不了）
    const human = scanHumanTakeover(agent, st)
    if (human.takeover) deliver(agent, TEXT.detectorOff, LABEL.detectorOff)
    else if (human.resumed) deliver(agent, TEXT.detectorOn, LABEL.detectorOn)

    // 接管 / 关闭：整个检测器静默（连 E 也不提示），但仍记账
    if (!st.detector_enabled || st.human_takeover) {
      saveState(dir, st)
      return {
        round: st.gsas_round_count, due: false, lag, injected: null, judgmentWrite: note,
        detector: 'off', human,
      }
    }

    let text = null
    let kind = null
    const e = evaluateE(st)
    if (e.inject) { text = e.text; kind = 'E' }
    else { text = TEXT.statusReminder; kind = 'status' }   // 模板 3：每次精修后
    const how = deliver(agent, text,
      kind === 'E' ? (e.capped ? LABEL.eCap : LABEL.eReminder) : LABEL.statusReminder)
    if (kind === 'E' && how !== 'unavailable') st.e_last_injected_at_lag = e.lag

    // 到期 ⇒ 后台派生审查子代理（不 await；结果迟到时由 deliver 走 followup 兜底）
    let review = null
    if (due) {
      if (reviewInFlight(dir, sample)) {
        review = { skipped: 'a review is already in flight' }
      } else {
        // 释放检测：上次冻结后，判断栏是否已同时给出"已读检查表 + 新路线"
        const rel = detectRelease(dir, st)
        if (rel.released) deliver(agent, TEXT.released, LABEL.released)
        st.last_review_at_round = st.gsas_round_count
        saveState(dir, st)                       // 先落盘，再起后台任务（避免竞态）
        const p = runReviewInBackground({ ctx, parent: agent, dir, pluginRoot, provider, timeoutMs, agentOptions, logger, sample })
          .catch(err => ({ error: String(err && err.message || err) }))
          .finally(() => INFLIGHT.delete(inflightKey(dir, sample)))
        INFLIGHT.set(inflightKey(dir, sample), p)
        review = { started: true, provider }
      }
    }
    saveState(dir, st)

    return {
      round: st.gsas_round_count, due, lag, injected: kind, delivered: how,
      judgmentWrite: note, detector: 'on', review,
    }
  } catch (e) {
    return { error: String(e && e.message || e) }
  }
}

export const _internals = { mechPath, SUCCESS }
