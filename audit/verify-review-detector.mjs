// verify-review-detector.mjs — 代码层验证（切片 1：D19/D20 机械层）
// 用户授权："只在代码层面检验，因为不同AI判断与结果均不同" ⇒ 全部用**桩 agent**，
// 不调用任何真实模型/引擎，零额度。
//
// usage: node verify-review-detector.mjs [--root <plugin root>]
import { readFileSync, existsSync, writeFileSync, rmSync, mkdirSync, readdirSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const argv = process.argv.slice(2)
const rootIdx = argv.indexOf('--root')
// Default root resolves from THIS file (dev layout: <repo>/workspace/xrd-plugin-checks),
// so the module carries no maintainer path — it ships inside the package as audit/, and a
// hardcoded one is a real leak, not just an inconvenience.
function defaultRoot() {
  if (process.env.DSH_XRD_SRC) return resolve(process.env.DSH_XRD_SRC)
  const sibling = resolve(HERE, '..', '..', 'dsh-xrd')
  return existsSync(sibling) ? sibling : resolve(HERE, '..')
}
const ROOT = rootIdx >= 0 ? argv[rootIdx + 1] : defaultRoot()
const WORK = join(HERE, '.work', 'review-detector')

let pass = 0, fail = 0
const check = (ok, label, detail = '') => {
  if (ok) { pass++; console.log('  PASS  ' + label) }
  else { fail++; console.log('  FAIL  ' + label + (detail ? '\n        ' + detail : '')) }
}

console.log('plugin root :', ROOT)
const mod = await import(pathToFileURL(join(ROOT, 'lib', 'review.js')).href)
const {
  loadState, saveState, recordGsasRound, judgeStaleness, evaluateE, applyCode, codeOutcome,
  deliver, injectedMessage, onGsasRound, emptyState, TEXT, LABEL, LANG_MARK, SUMMARY_MAX_CHARS,
  MECH_FILE, JUDGMENT_FILE,
  REVIEW_EVERY, E_LAG_THRESHOLD, REVIEW_TIMEOUT_MS, markFrozen, markReleased,
} = mod

// ── 桩 agent ────────────────────────────────────────────────────────────────
// 桩**整条消息都记录**（不只 content[0].text）：0.8.0 的真实事故正是消息形状问题——
// 只发 {role, content} 时宿主处理 inbox 会读 message.source.kind 而抛
// "Cannot read properties of undefined (reading 'kind')"，整轮以错误结束；
// 桩只看 text 的话，这个缝永远测不出来。
const mkAgent = (status = 'running') => {
  const sent = []
  return {
    status, sent,
    inject: (m) => sent.push({ how: 'inject', text: m.content[0].text, message: m }),
    followup: (m) => sent.push({ how: 'followup', text: m.content[0].text, message: m }),
  }
}
const fresh = (name) => {
  const d = join(WORK, name)
  rmSync(d, { recursive: true, force: true })
  mkdirSync(d, { recursive: true })
  return d
}

// ── 1. 计数与触发 ───────────────────────────────────────────────────────────
{
  const d = fresh('count')
  for (let i = 1; i <= 7; i++) {
    const r = recordGsasRound(d, { wR: 20 - i, ok: true })
    check(r.round === i, `round ${i} recorded (gsas_round_count=${r.round})`)
    if (i === 3 || i === 6) check(r.due === true, `round ${i} marks review due (every ${REVIEW_EVERY})`)
    else check(r.due === false, `round ${i} does not mark review due`)
  }
  const st = loadState(d)
  check(st.rounds.length === 7, 'mechanical file keeps the round ledger', JSON.stringify(st.rounds.length))
  check(st.rounds.every(x => typeof x.wR === 'number'), 'ledger rows carry wR')
}
// 同一轮不重复触发：last_review_at_round 之后才 due
{
  const d = fresh('dedupe')
  let due = 0
  for (let i = 1; i <= 6; i++) if (recordGsasRound(d, {}).due) due++
  check(due === 2, 'due fires exactly twice over 6 rounds (3 and 6)', 'due=' + due)
  const st = loadState(d)
  st.last_review_at_round = 6
  saveState(d, st)
  check(recordGsasRound(d, {}).due === false, 'a round already reviewed is never due again')
}

// ── 2. 只数 GSAS 轮（源码断言） ──────────────────────────────────────────────
{
  const src = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
  const callSites = [...src.matchAll(/onGsasRound\(/g)].length
  // 0.8.10: TWO tools do GSAS bookkeeping now — `xrd_refine_gsas` and the new
  // `xrd_refine_multiphase` — and each must call it on BOTH the success and the failure
  // path, so the expected count is 2 tools x 2 paths = 4 (it was 2 while one tool
  // existed). Kept EXACT on purpose: a new GSAS-family tool that forgets its bookkeeping
  // is precisely the silent gap this guards.
  check(callSites === 4, 'onGsasRound called twice per GSAS-family tool (success + failure)',
    'sites=' + callSites + ' (expected 4: xrd_refine_gsas + xrd_refine_multiphase)')
  // manual / auto 工具体内不得出现该调用
  const manualBody = src.slice(src.indexOf("name: 'xrd_refine_manual'"), src.indexOf("name: 'xrd_verify_fp2k'"))
  const autoBody = src.slice(src.indexOf("name: 'xrd_refine_auto'"), src.indexOf("name: 'xrd_refine_manual'"))
  check(!manualBody.includes('onGsasRound('), 'xrd_refine_manual does NOT count rounds')
  check(!autoBody.includes('onGsasRound('), 'xrd_refine_auto does NOT count rounds')
  const gsasBody = src.slice(src.indexOf("name: 'xrd_refine_gsas'"), src.indexOf('// cif_to_pcr'))
  check(gsasBody.includes('onGsasRound('), 'xrd_refine_gsas counts rounds')
}

// ── 3. streak / level（用户裁定五） ─────────────────────────────────────────
{
  const st = emptyState()
  const seq = ['B', 'B', 'B', 'C', 'C', 'A', 'D']
  const levels = seq.map(c => applyCode(st, c).level)
  check(JSON.stringify(levels) === JSON.stringify([1, 2, 3, 1, 2, 0, 1]),
    'levels follow "same code consecutive +1; a different non-A code restarts; A clears all"',
    JSON.stringify(levels))
  check(Object.values(st.levels).every(v => v === 0 || st.levels.D === 1), 'A cleared every counter before D')
  // 冻结门槛
  const s2 = emptyState()
  applyCode(s2, 'B'); check(codeOutcome(s2, 'B').freeze === false, 'level 1 never freezes')
  applyCode(s2, 'B'); check(codeOutcome(s2, 'B').injectable === true && codeOutcome(s2, 'B').freeze === false,
    'level 2 injects but does not freeze')
  applyCode(s2, 'B'); check(codeOutcome(s2, 'B').freeze === true, 'level 3 freezes (B/C/D only)')
  let threw = false
  try { applyCode(emptyState(), 'E') } catch { threw = true }
  check(threw, 'code E cannot enter the streak machinery (E is plugin-mechanical, never freezes)')
}

// ── 4. E 判定（插件侧，不派生子代理） ───────────────────────────────────────
{
  const d = fresh('elag')
  const agent = mkAgent('running')
  const seen = []
  for (let i = 1; i <= 9; i++) {
    const r = onGsasRound({ dir: d, agent, wR: 20 })
    seen.push({ round: i, injected: r.injected, lag: r.lag })
  }
  // lag 随轮次增长：judgment 从未更新
  const eRounds = seen.filter(x => x.injected === 'E').map(x => x.round)
  check(JSON.stringify(eRounds) === JSON.stringify([2, 4, 6, 8]),
    'E reminder fires at lag 2 and every +2 afterwards', JSON.stringify(eRounds))
  check(seen[0].injected === 'status', 'non-E rounds inject the status-table reminder')
  check(seen.filter(x => x.injected === 'status').length === 5, 'status reminder on the other rounds')
  // 判断栏更新后 lag 归零、E 不再提示
  writeFileSync(join(d, JUDGMENT_FILE), '# judgment\n(updated)\n', 'utf8')
  const r10 = onGsasRound({ dir: d, agent })
  check(r10.lag === 1, 'judgment write resets the lag to 1 (the table is now one round old)', 'lag=' + r10.lag)
  check(r10.injected === 'status', 'no E reminder right after the judgment file was rewritten')
  const st = loadState(d)
  check(st.judgment_updated_at_round === st.gsas_round_count - 1,
    'judgment_updated_at_round records the round the AI wrote at')
}

// ── 5. 注入路由（inject vs followup vs unavailable） ─────────────────────────
{
  const running = mkAgent('running')
  check(deliver(running, 'x') === 'inject' && running.sent[0].how === 'inject', 'running agent ⇒ inject (next-step)')
  const idle = mkAgent('idle')
  check(deliver(idle, 'x') === 'followup' && idle.sent[0].how === 'followup', 'idle agent ⇒ followup (guaranteed delivery)')
  check(deliver(null, 'x') === 'unavailable', 'absent agent ⇒ unavailable (no crash)')
  // 送不出去时不得把 E 记成"已提示"
  const d = fresh('undeliverable')
  for (let i = 1; i <= 4; i++) onGsasRound({ dir: d, agent: null })
  const st = loadState(d)
  check(!st.e_last_injected_at_lag, 'an undeliverable E reminder is not booked as delivered',
    'e_last_injected_at_lag=' + st.e_last_injected_at_lag)
}

// ── 6. 注入文本不得含代号字母（用户裁定三/五） ───────────────────────────────
{
  const standalone = /(?<![A-Za-z])[A-E](?![A-Za-z])/
  for (const [k, v] of Object.entries(TEXT)) {
    check(!standalone.test(v), `TEXT.${k} contains no bare A–E code letter`, JSON.stringify(v.slice(0, 60)))
  }
  check(TEXT.b3.includes('冻结') && TEXT.c3.includes('冻结') && TEXT.d3.includes('冻结'),
    'B/C/D level-3 texts carry the soft-freeze wording')
  check(!TEXT.eReminder.includes('冻结') || TEXT.eReminder.includes('不冻结'),
    'the E text never asks for a freeze')
}

// ── 7. 机械栏：严格 JSON + 原子写 + 损坏不静默覆盖 ──────────────────────────
{
  const d = fresh('json')
  recordGsasRound(d, { wR: 12.3 })
  const raw = readFileSync(join(d, MECH_FILE), 'utf8')
  check(JSON.parse(raw).gsas_round_count === 1, 'mechanical file is strict JSON')
  check(!readdirSync(d).some(f => f.endsWith('.tmp')), 'atomic write leaves no .tmp behind')
  writeFileSync(join(d, MECH_FILE), '{ "gsas_round_count": +2 }', 'utf8')   // 故意写成非法 JSON
  let msg = ''
  try { loadState(d) } catch (e) { msg = e.message }
  check(/not valid JSON/.test(msg), 'a damaged mechanical file fails loudly instead of being overwritten', msg)
}

// ── 8. 检测器关闭时静默 ─────────────────────────────────────────────────────
{
  const d = fresh('off')
  const agent = mkAgent('running')
  const st = loadState(d)
  st.detector_enabled = false
  saveState(d, st)
  const r = onGsasRound({ dir: d, agent })
  check(r.detector === 'off' && r.injected === null && agent.sent.length === 0,
    'detector off ⇒ no injection at all (not even E)')
}

// ── 9. 常量与设计一致 ───────────────────────────────────────────────────────
check(REVIEW_EVERY === 3, 'review cadence = every 3 GSAS rounds')
check(E_LAG_THRESHOLD === 2, 'E threshold = lag 2')
check(REVIEW_TIMEOUT_MS === 120_000, 'subagent timeout budget = 120 s (user: 稍微增加预算)')

// ── 10. 切片 2：审查子代理（桩 subagents 服务，零额度） ──────────────────────
{
  const {
    runReviewInBackground, buildReviewPrompt, parseCode, outcomeText, extractChecklist,
    detectRelease, REVIEW_PROVIDER_DEFAULT, PROMPT_TEMPLATE, startReviewAndWait, invalidText,
  } = mod

  // 桩：记录 start() 的入参，按需返回一个代号 / 卡住 / 抛错
  const mkCtx = ({ answer = 'B', hang = false, throwOnStart = false, hasProvider = true } = {}) => {
    const calls = []
    return {
      calls,
      get: (name) => name !== 'subagents' ? null : {
        getProvider: () => (hasProvider ? { name: 'spawn' } : undefined),
        start: async (provider, req) => {
          calls.push({ provider, req })
          if (throwOnStart) throw new Error('start refused')
          return {
            id: 'stub-run',
            result: hang ? new Promise(() => {}) : Promise.resolve({ output: [{ type: 'text', text: answer }] }),
            dispose: async () => {},
          }
        },
      },
    }
  }
  const parent = mkAgent('running')
  const d = fresh('slice2')
  writeFileSync(join(d, JUDGMENT_FILE), '# judgment\n用户输入：先试 P2_1/c\n', 'utf8')

  // 10a 一次到期轮触发一次派生，且入参符合设计
  const ctx = mkCtx({ answer: 'B' })
  onGsasRound({ dir: d, agent: parent, wR: 15 }, { ctx, pluginRoot: ROOT })
  onGsasRound({ dir: d, agent: parent, wR: 14 }, { ctx, pluginRoot: ROOT })
  const third = onGsasRound({ dir: d, agent: parent, wR: 13 }, { ctx, pluginRoot: ROOT })
  check(third.review && third.review.started === true, 'round 3 starts a background review')
  check(ctx.calls.length === 1, 'exactly one subagent start per due round', 'calls=' + ctx.calls.length)
  {
    const { provider, req } = ctx.calls[0]
    check(provider === 'spawn', 'uses the fresh-child provider "spawn" (not fork)', provider)
    check(req.parent === parent, 'parent is the calling agent (exec.agent)')
    check(req.agentOptions === undefined, 'no agentOptions ⇒ child inherits the main conversation route')
    check(JSON.stringify(req.toolFilter) === JSON.stringify({ allow: ['read', 'glob', 'grep'] }),
      'child is read-only (read/glob/grep allow-list)')
    check(req.maxDepth === 1, 'child may not delegate further (maxDepth 1)')
    // 2026-10-05 diagnosis: the host driver reads request.signal.aborted UNCONDITIONALLY
    // (dsh-subagent-in-process-driver:160-162) and BOTH providers funnel into it, so a
    // request without a signal dies in ~15 ms with a TypeError — which the old one-size
    // "did not return within its budget" text disguised as a timeout.
    check(!!req.signal && typeof req.signal.aborted === 'boolean',
      'the start request carries an AbortSignal (the host driver reads request.signal.aborted)',
      `signal=${JSON.stringify(req.signal)} — the review would die with TypeError in ~15 ms`)
    check(typeof req.prompt[0].text === 'string' && req.prompt[0].text.includes('当前任务状态包'),
      'prompt carries the state package')
    check(req.prompt[0].text.includes('只输出一个字母'), 'prompt states the one-letter output rule')
  }
  await new Promise(r => setTimeout(r, 60))   // 让后台任务落地
  {
    const st = loadState(d)
    check(st.review_history.length === 1 && st.review_history[0].code === 'B',
      'the verdict is booked in the mechanical file', JSON.stringify(st.review_history))
    check(st.last_review_at_round === 3, 'last_review_at_round marks the reviewed round')
    check(st.levels.B === 1, 'B level 1 recorded')
    // 等级 1 ⇒ **不注入审查文本**（Possible 不注入）。注意：同一轮里 E 提醒可能合法出现
    // （此例判断栏自第 0 轮起未更新），所以这里断言的是"B 的两段文本都没被送出"。
    const bTexts = parent.sent.filter(x => x.text === TEXT.b2 || x.text === TEXT.b3)
    check(bTexts.length === 0, 'level 1 does NOT inject the review text (Possible 不注入)',
      JSON.stringify(parent.sent.map(x => x.text.slice(0, 24))))
  }

  // 10b 在飞时不重复派生
  {
    const d2 = fresh('inflight')
    const hangCtx = mkCtx({ hang: true })
    for (let i = 1; i <= 6; i++) onGsasRound({ dir: d2, agent: parent, wR: 12 }, { ctx: hangCtx, pluginRoot: ROOT, timeoutMs: 50 })
    await new Promise(r => setTimeout(r, 80))
    check(hangCtx.calls.length === 1, 'a second due round does not stack a second in-flight review', 'calls=' + hangCtx.calls.length)
    const st2 = loadState(d2)
    check(st2.review_history.some(x => x.invalid === true), 'a timed-out review injects the invalid marker')
    check(parent.sent.some(x => /检测失效/.test(x.text)), 'invalid marker actually delivered')
  }

  // 10c 代号解析与等级→文本（B/B/B → 软冻结 + §6）
  {
    check(parseCode('B') === 'B' && parseCode('  代号：C\n') === 'C' && parseCode('检测失效') === null
      && parseCode('the answer is D') === null, 'parseCode accepts only a leading standalone A–D')
    const st = emptyState()
    const l1 = applyCode(st, 'B'); const t1 = outcomeText(st, 'B', { checklist: '§6 BODY' })
    const l2 = applyCode(st, 'B'); const t2 = outcomeText(st, 'B', { checklist: '§6 BODY' })
    const l3 = applyCode(st, 'B'); const t3 = outcomeText(st, 'B', { checklist: '§6 BODY' })
    check(l1.level === 1 && t1.text === null, 'level 1 ⇒ no text')
    check(l2.level === 2 && t2.text === TEXT.b2 && t2.freeze === false, 'level 2 ⇒ B text, no freeze')
    check(l3.level === 3 && t3.freeze === true && t3.text.includes('§6 BODY') && t3.text.includes('极限模式'),
      'level 3 ⇒ soft freeze + §6 body + extreme-mode hint')
    check(!/(?<![A-Za-z])[A-D](?![A-Za-z])/.test(t3.text), 'the level-3 text still contains no bare code letter')
    const stA = emptyState(); applyCode(stA, 'B'); applyCode(stA, 'B'); applyCode(stA, 'A')
    check(stA.levels.B === 0 && stA.streaks.B === 0, 'code A clears every counter')
  }

  // 10d §6 抽取 + 状态包组装
  {
    const six = extractChecklist(join(ROOT, 'skills', 'xrd-workflow', 'SKILL.md'))
    check(!!six && /^## 6\./.test(six) && !/^## 7\./m.test(six), 'extractChecklist returns §6 only (stops before §7)')
    check(six.length > 2000, '§6 body is substantial', 'len=' + (six ? six.length : 0))
    const p = buildReviewPrompt({ st: loadState(fresh('pkg')), judgment: 'J', journalTail: 'L' })
    check(p.includes('机械栏') && p.includes('判断栏') && p.includes('精修日志'), 'state package carries all three blocks')
  }

  // 10e 释放条件（判断栏出现"已读检查表 + 新路线"）
  {
    const d3 = fresh('release')
    const st = loadState(d3)
    markFrozen(st, 'D', 3); saveState(d3, st)
    writeFileSync(join(d3, JUDGMENT_FILE), '# judgment\n已读检查表：是\n新路线：先试分支 A\n', 'utf8')
    const r = detectRelease(d3, loadState(d3))
    check(r.released === true, 'release detected when the judgment declares checklist + new route')
    const st4 = loadState(d3); markFrozen(st4, 'D', 3); saveState(d3, st4)
    writeFileSync(join(d3, JUDGMENT_FILE), '# judgment\n啥也没写\n', 'utf8')
    check(detectRelease(d3, loadState(d3)).released === false, 'no release without both markers')
  }

  // 10f 失败/服务缺失 ⇒ 检测失效，且不影响计数
  {
    const d4 = fresh('nosvc')
    const before = loadState(d4).gsas_round_count
    const res = await runReviewInBackground({ ctx: { get: () => null }, parent, dir: d4, pluginRoot: ROOT })
    const st = loadState(d4)
    check(res.invalid === true && st.gsas_round_count === before, 'missing subagents service ⇒ invalid, counters untouched')
    const d5 = fresh('throwstart')
    const res5 = await runReviewInBackground({ ctx: mkCtx({ throwOnStart: true }), parent, dir: d5, pluginRoot: ROOT })
    check(res5.invalid === true, 'a refusing backend ⇒ invalid marker, never a crash')
  }

  // 10g 宿主的 start 契约（2026-10-05 诊断）：两个 provider（spawn / fork）都进同一个
  // in-process 驱动，而它**无条件**读 `request.signal.aborted` ⇒ 不带 signal 的请求在
  // ~15 ms 内以 TypeError 死掉。旧桩什么都不校验，这正是缺陷能出厂的原因 —— 所以这个桩
  // 按真实契约**抛**，插件必须自己满足它。
  {
    const strictParent = mkAgent('running')
    const seen = []
    const strictCtx = {
      get: (name) => name !== 'subagents' ? null : {
        getProvider: () => ({ name: 'spawn' }),
        start: async (provider, request) => {
          seen.push(request)
          // 与 dsh-subagent-in-process-driver:160-162 同形：先 assertMaxDepth，再读 signal
          if (!request.signal || typeof request.signal.aborted !== 'boolean') {
            throw new TypeError("Cannot read properties of undefined (reading 'aborted')")
          }
          return {
            id: 'strict-run',
            result: Promise.resolve({ output: [{ type: 'text', text: 'D' }] }),
            dispose: async () => {},
          }
        },
      },
    }
    const r = await startReviewAndWait({ ctx: strictCtx, parent: strictParent, prompt: 'x' })
    check(seen.length === 1, 'the strict driver contract was exercised', 'calls=' + seen.length)
    check(r.ok === true && r.code === 'D',
      'the review start request satisfies the REAL driver contract (signal present and readable)',
      `r=${JSON.stringify({ ok: r.ok, code: r.code, error: r.error })}`)
    // 时间戳证据：老缺陷是"15 ms 内 TypeError"，不是超时
    check(!/reading 'aborted'/.test(String(r.error || '')),
      'no "reading \'aborted\'" TypeError (the 2026-10-05 failure mode)')
  }

  // 10h 失败文案必须分辨来源（旧文案把 skipped / 报错 / 超时混成一句"未在预算内返回"）
  {
    const tSkip = invalidText({ skipped: 'subagents service unavailable' })
    const tErr = invalidText({ error: 'boom' })
    const tTime = invalidText({ error: 'review subagent timed out after 120000 ms' })
    check(/检测未执行/.test(tSkip) && !/检测失效/.test(tSkip),
      'an unavailable service reads as "not run", not as a failure', tSkip.slice(0, 80))
    check(/检测失效/.test(tErr) && /boom/.test(tErr) && !/超时/.test(tErr),
      'a thrown error names the error and does NOT claim a timeout', tErr.slice(0, 90))
    check(/检测失效/.test(tTime) && /超时/.test(tTime),
      'a real timeout is the only case that says 超时', tTime.slice(0, 90))
    check(tErr !== tTime && tSkip !== tErr, 'the three sources produce three different texts')
    check(/language-zh-/.test(tErr) && /language-en-/.test(tErr),
      'the failure text keeps the bilingual markers and its label anchor')
  }

  // 10i 接管中返回 ⇒ 静默丢弃
  {
    const d6 = fresh('takeover')
    const st = loadState(d6); st.human_takeover = true; saveState(d6, st)
    const parent2 = mkAgent('running')
    const res = await runReviewInBackground({ ctx: mkCtx({ answer: 'D' }), parent: parent2, dir: d6, pluginRoot: ROOT })
    check(res.discarded === 'detector off' && parent2.sent.length === 0,
      'a verdict arriving after takeover is discarded silently')
  }

  check(REVIEW_PROVIDER_DEFAULT === 'spawn', 'default review provider is spawn')
  check(PROMPT_TEMPLATE.includes('A 无问题') && PROMPT_TEMPLATE.includes('D 未触发检查表'),
    'template 1 carries the four-code judgement table')
}

// ── 11. 切片 3：人为接管 ⇒ 关闭（不自动重启）/ 仅"继续/交回"恢复 ───────────────
{
  const mkSessAgent = (status = 'running') => {
    const a = mkAgent(status)
    a.session = { events: [] }
    let seq = 0
    a.pushUser = (text) => { a.session.events.push({ type: 'user/message', seq: ++seq, data: { content: [{ type: 'text', text }] } }) }
    return a
  }
  const d = fresh('takeover2')
  const agent = mkSessAgent()

  // 首轮的"任务提示"不是接管
  agent.pushUser('请帮我精修这个样品的 XRD 数据')
  onGsasRound({ dir: d, agent, wR: 20 })
  let st = loadState(d)
  check(st.human_takeover === false && st.detector_enabled === true,
    'the task prompt on round 1 is NOT treated as a takeover')

  // 精修期间来了用户消息 ⇒ 关闭 + 一次性通知
  agent.pushUser('我来接管这部分，你别动 K 分裂')
  const r2 = onGsasRound({ dir: d, agent, wR: 19 })
  st = loadState(d)
  check(r2.detector === 'off' && st.human_takeover === true && st.detector_enabled === false,
    'a user message mid-refinement closes the detector')
  check(agent.sent.filter(x => x.text === TEXT.detectorOff).length === 1,
    'the close notice is delivered exactly once', JSON.stringify(agent.sent.map(x => x.text.slice(0, 20))))
  check(st.takeover_history.length === 1 && st.takeover_history[0].action === 'close',
    'the takeover is recorded in the mechanical file')

  // 关闭后完全静默（连 E/状态表提醒都没有），且不派生
  const ctxOff = { get: () => ({ getProvider: () => ({ name: 'spawn' }), start: async () => { throw new Error('must not start') } }) }
  const sentBefore = agent.sent.length
  for (let i = 0; i < 6; i++) onGsasRound({ dir: d, agent, wR: 18 }, { ctx: ctxOff, pluginRoot: ROOT })
  check(agent.sent.length === sentBefore, 'while closed: no injection at all (not even E)')
  st = loadState(d)
  check(st.review_history.length === 0, 'while closed: no review is ever spawned')
  check(st.gsas_round_count > 6, 'while closed: rounds are still counted (ledger keeps growing)')

  // 非"继续/交回"的消息不会自动重启
  agent.pushUser('这个峰看起来不太对')
  onGsasRound({ dir: d, agent, wR: 17 })
  st = loadState(d)
  check(st.human_takeover === true, 'an unrelated user message does NOT silently re-enable the detector')

  // 只有明说"继续/交回"才恢复，并重置全部计数
  st.levels.B = 3; st.streaks.B = 3; st.e_last_injected_at_lag = 4; saveState(d, st)
  agent.pushUser('交回给你，继续')
  const r = onGsasRound({ dir: d, agent, wR: 16 })
  st = loadState(d)
  check(st.human_takeover === false && st.detector_enabled === true, 'explicit "继续/交回" resumes the detector')
  check(st.streaks.B === 0 && st.levels.B === 0, 'resume resets the streak/level counters')
  check(st.takeover_history.some(x => x.action === 'resume'), 'the resume is recorded in the mechanical file')
  // 注意：恢复后的**同一轮**里 E 提醒会合法触发（判断栏滞后多轮）并把 e_last_injected_at_lag
  // 重新记上——那是重置之后的新记账，不是"没重置"。这里断言它等于本轮 lag。
  check(st.e_last_injected_at_lag === judgeStaleness(st) || st.e_last_injected_at_lag === 0,
    'E bookkeeping after resume reflects the new lag (reset then re-booked)', 'e=' + st.e_last_injected_at_lag + ' lag=' + judgeStaleness(st))
  check(st.last_review_at_round === st.gsas_round_count,
    'resume defers the next review (no immediate trigger)')
  check(agent.sent.some(x => x.text === TEXT.detectorOn), 'a resume notice is delivered')
  check(r.detector === 'on', 'the round after resume is active again')
}

// ── 12. 0.8.9：插件自己的注入不是"人"（自关缺陷）─────────────────────────────
// 真实事故（2026-09-28 端到端验收）：注入的提醒本身也是 `user/message`
// （inject 发的就是 role:'user' + source.kind:'plugin'），于是**下一轮**把自己的话读成
// 人发的话 ⇒ human_takeover=true、detector_enabled=false，整个 D19/D20 静默失效。
// 首轮有"只做基线"的早退保护，所以**从第 2 轮起必然自关**。
console.log('\n== 12. the plugin\'s own injection is never mistaken for a human takeover (0.8.9) ==')
{
  const mkSessAgent = (status = 'running') => {
    const a = mkAgent(status)
    a.session = { events: [] }
    let seq = 0
    a.pushEvent = (data) => { a.session.events.push({ type: 'user/message', seq: ++seq, data }) }
    a.pushUser = (text) => a.pushEvent({ content: [{ type: 'text', text }] })
    return a
  }

  // (a) the REAL injected message shape (constructor, not a hand-built stub)
  const d = fresh('self-inject')
  const agent = mkSessAgent()
  agent.pushUser('请帮我精修这个样品的 XRD 数据')          // round-1 baseline
  onGsasRound({ dir: d, agent, wR: 20 })
  check(loadState(d).detector_enabled === true, 'round 1 baseline keeps the detector on')
  // the round-1 reminder the plugin just delivered, read back on round 2
  agent.pushEvent(injectedMessage(TEXT.statusReminder, 'dsh-xrd', LABEL.statusReminder))
  const r2 = onGsasRound({ dir: d, agent, wR: 19 })
  let st = loadState(d)
  check(st.human_takeover === false && st.detector_enabled === true,
    'THE REGRESSION: the plugin\'s own injected reminder does NOT close the detector',
    JSON.stringify({ takeover: st.human_takeover, detector: st.detector_enabled }))
  check(r2.detector === 'on' && (r2.injected === 'status' || r2.injected === 'E'),
    'round 2 keeps injecting as usual (whatever the round is due: status, or E once the judgment lags)',
    JSON.stringify({ detector: r2.detector, injected: r2.injected }))
  check((st.takeover_history || []).length === 0, 'no takeover is recorded for an injected message')
  // the injected text must not have advanced last_user_seq past real user messages
  check(st.last_user_seq === 1, 'an injected event does not advance last_user_seq', 'seq=' + st.last_user_seq)

  // (b) a host variant that does not surface `source`: the bilingual marker is the tell
  const d2 = fresh('self-inject-mark')
  const a2 = mkSessAgent()
  a2.pushUser('精修这个样品')
  onGsasRound({ dir: d2, agent: a2, wR: 20 })
  a2.pushEvent({ content: [{ type: 'text', text: TEXT.eReminder }] })   // no source.kind
  onGsasRound({ dir: d2, agent: a2, wR: 19 })
  check(loadState(d2).human_takeover === false,
    'an injected event WITHOUT source.kind is still recognised by its bilingual marker')

  // (c) control: a real human message still closes it
  const d3 = fresh('real-takeover')
  const a3 = mkSessAgent()
  a3.pushUser('精修这个样品')
  onGsasRound({ dir: d3, agent: a3, wR: 20 })
  a3.pushUser('我来手动接管，你别继续了')
  const r3 = onGsasRound({ dir: d3, agent: a3, wR: 19 })
  check(r3.detector === 'off' && loadState(d3).human_takeover === true,
    'control: a genuine human message mid-refinement still closes the detector')

  // (d) migration: a closure PROVABLY caused by the plugin is repaired on load, and persisted
  const d4 = fresh('repair')
  const st4 = loadState(d4, { sample: 'S1' })
  st4.gsas_round_count = 2
  st4.human_takeover = true
  st4.detector_enabled = false
  st4.takeover_history = [{ action: 'close', at_round: 2, snippet: TEXT.statusReminder.replace(/\s+/g, ' ').slice(0, 120), ts: 'x' }]
  saveState(d4, st4)
  const loaded = loadState(d4, { sample: 'S1' })
  check(loaded.human_takeover === false && loaded.detector_enabled === true,
    'a closure whose only snippet is the plugin\'s own injection is repaired on load')
  check((loaded.takeover_history || []).some(x => x.action === 'auto-resume'),
    'the repair is recorded as auto-resume (auditable, not silent)')
  check(loadState(d4, { sample: 'S1' }).human_takeover === false,
    'the repair is persisted to disk, not only in memory')

  // (e) control: a genuine closure is NOT repaired
  const d5 = fresh('no-repair')
  const st5 = loadState(d5, { sample: 'S2' })
  st5.gsas_round_count = 4
  st5.human_takeover = true
  st5.detector_enabled = false
  st5.takeover_history = [{ action: 'close', at_round: 4, snippet: '我来手动接管 K 分裂', ts: 'x' }]
  saveState(d5, st5)
  const loaded5 = loadState(d5, { sample: 'S2' })
  check(loaded5.human_takeover === true && loaded5.detector_enabled === false,
    'control: a closure caused by a REAL human message is left closed')

  // (f) mixed history (one real + one injected closure) is left alone as well
  const d6 = fresh('mixed')
  const st6 = loadState(d6, { sample: 'S3' })
  st6.human_takeover = true
  st6.detector_enabled = false
  st6.takeover_history = [
    { action: 'close', snippet: TEXT.statusReminder.replace(/\s+/g, ' ').slice(0, 120) },
    { action: 'close', snippet: '我自己来' },
  ]
  saveState(d6, st6)
  check(loadState(d6, { sample: 'S3' }).human_takeover === true,
    'control: a MIXED history is never released (the plugin does not guess)')
}

// ── 消息形状：宿主 inbox 契约（2026-09-27 真实宿主抓到的缺陷）────────────────
// 真实事故：注入消息只带 {role, content} ⇒ 宿主读 message.source.kind 抛 TypeError，
// **整轮 turn 以 kind:'error' 结束**（日志 turn/end reason.error）。桩必须断言整条形状。
console.log('\n== 注入消息必须是完整的 UserMessage（宿主 inbox 契约） ==')
{
  const d = fresh('shape')
  const agent = mkAgent('running')
  onGsasRound({ dir: d, agent, wR: 12 })
  const m = agent.sent[0]?.message
  check(!!m, 'something was delivered')
  check(typeof m?.id === 'string' && m.id.length >= 8, 'the message carries an id (host freezes identified messages)')
  check(m?.role === 'user', 'role is user')
  check(Array.isArray(m?.content) && m.content[0]?.type === 'text', 'content is a text block')
  check(m?.source?.kind === 'plugin', 'source.kind is "plugin" (host reads source.kind — its absence crashed the turn)',
    'source=' + JSON.stringify(m?.source))
  check(m?.source?.plugin === 'dsh-xrd', 'source.plugin names this plugin')
  check(m?.source?.form === 'notice' && typeof m?.source?.summary === 'string', 'source form is "notice" with a summary')
  // 2026-09-27：summary 不再由正文截取（双语后截出来是个被切断的中文半句），改为每条专门写的
  // 双语短标签；标签本身仍落在宿主**文档**上限 120 内（`CONTEXT_SUMMARY_MAX_CHARS`）。
  check(m.source.summary === LABEL.statusReminder, 'the summary is the purpose-written label, not a truncated payload',
    'summary=' + m.source.summary)
  check(m.source.summary.length <= 120, 'the shipped labels stay inside the host documented bound (120)',
    'len=' + m.source.summary.length)
  // 每次投递都是新 id（宿主会 freeze 消息；复用同一实例不安全）
  const agent2 = mkAgent('running')
  onGsasRound({ dir: d, agent: agent2, wR: 12 })
  check(agent2.sent[0]?.message?.id !== m.id, 'each delivery builds a fresh message identity')
  // followup 路径同样带 source
  const idleAgent = mkAgent('idle')
  deliver(idleAgent, TEXT.statusReminder)
  check(idleAgent.sent[0]?.how === 'followup' && idleAgent.sent[0]?.message?.source?.kind === 'plugin',
    'the followup path carries the same complete message shape')
  // 直接测构造器：只有 summary 有界，payload 完整；上限按用户要求放宽到 200
  const long = injectedMessage('x'.repeat(400))
  check(long.source.summary.length === SUMMARY_MAX_CHARS && long.content[0].text.length === 400,
    'injectedMessage bounds only the summary, never the payload')
  check(SUMMARY_MAX_CHARS === 200, 'the summary cap is relaxed to 200 (user 2026-09-27)')
  check(injectedMessage('x', 'dsh-xrd', 'z'.repeat(130)).source.summary.length === 130,
    'a summary between 120 and 200 is no longer clipped')
  const over = Object.entries(LABEL).filter(([, v]) => v.length > 120)
  check(over.length === 0, 'every shipped label fits the host documented bound (120)',
    JSON.stringify(over.map(([k, v]) => [k, v.length])))
  // 注入行双语（用户 2026-09-27）：两块各带语言标记，让 AI 一眼看出是同一内容的两种语言
  for (const [k, v] of Object.entries(TEXT)) {
    check(v.includes(LANG_MARK.zh) && v.includes(LANG_MARK.en), `TEXT.${k} carries both language markers`)
  }
  // 0.9.1: `invalid` is the ONE deliberate exception — its text is built per failure source
  // by invalidText() (skipped / thrown / timed out), so there is no static TEXT entry for it.
  // The label stays: it is still the summary anchor handed to deliver(). Any OTHER orphan
  // label means a TEXT entry was renamed or dropped.
  const LABEL_WITHOUT_TEXT = new Set(['invalid'])
  const orphans = Object.keys(LABEL).filter((k) => !(k in TEXT) && !LABEL_WITHOUT_TEXT.has(k))
  check(orphans.length === 0, 'every label key has a matching TEXT key (except the dynamic `invalid`)',
    JSON.stringify(orphans))
  check(typeof mod.invalidText === 'function' && /检测失效|检测未执行/.test(mod.invalidText({ error: 'x' })),
    'the dynamic `invalid` text really exists (the exception above is not a loophole)')
}

console.log(`\n  ${fail === 0 ? 'ALL PASS' : 'FAILURES'} — pass=${pass} fail=${fail}`)
rmSync(WORK, { recursive: true, force: true })
process.exit(fail === 0 ? 0 : 1)
