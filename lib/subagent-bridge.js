// lib/subagent-bridge.js — 宿主两代 `subagents` 服务的桥接（0.10.2，B 组）
//
// 为什么需要它：`lib/review.js` 的 D19 审查子代理只会调用旧代入口，而宿主换代后那个入口
// 改名了，能力探测（`typeof subagents.start === 'function'`）永假 ⇒ 审查链路静默 skipped、
// 不崩不告警。两代契约（2026-11-06 读源码确认）：
//
//   旧代 dsh-subagent 0.1.0-rc.5
//     await subagents.start(providerName, request)
//       request = { label, prompt:[{type:'text',text}], parent, signal,
//                   agentOptions?, toolFilter?:{allow:[…]}, maxDepth? }
//
//   新代 dsh-subagent 0.2.1-rc.2
//     await subagents.startActivation({ provider, label, request, signal, delivery })
//       request = { prompt:[{type:'text',text}], parent, cwd?, maxDepth?, agentOptions?,
//                   agentPreset?, outputSchema? }
//       → { childId, result: Promise, dispose() }
//
// 关键事实（决定了这次改动的规模）：**两代返回的句柄都提供 `result` 与 `dispose()`，且
// `result` 的载荷都是 `{ output:[{type:'text',text}], stopReason }`** ⇒ 调用方只需换
// "怎么起"，超时竞速、结果解析、dispose 兜底全部原样复用。
//   · 新代 `startActivation` 在**子代理 inbox 接受初始 prompt 时** resolve；
//     `result` 在**自然结算时** resolve（不是立即），所以调用方的时间上限继续作用于 `result`。
//   · 新代 `signal` 是硬必需（服务内部无条件 `signal.throwIfAborted()`），漏传 = 数毫秒内
//     TypeError —— 与 0.9.1 修过的那个坑同型，这里两代都显式传。
//
// 新代的两处能力差异，**有意接受并如实披露**，不假装还原：
//   ① `toolFilter` 已从请求面移除（persona/toolFilter 归 Agent Preset，新代 provider 的能力表
//      只有 agentOptions/agentPreset/outputSchema/depthLimit）⇒ 审查子代理在新代拿到父 agent
//      的**完整工具集**；旧代仍限制到 read/glob/grep。返回值里的 `toolRestriction` 说明实况。
//   ② `delivery` 必须显式给：`'parent'` 会在子代理结算时向父会话投递一条
//      `Background subagent … finished` 通知，与插件自己的注入重复且不可控 ⇒ 用 `'caller'`
//      （服务在 caller 模式下跳过结算通知）。代价：UI 的子代理列表不列出它。

/** 旧代审查子代理被限定的工具集（新代无此字段）。 */
export const REVIEW_TOOL_ALLOW = Object.freeze(['read', 'glob', 'grep'])

/** 每个后端实际生效的工具限制——**如实报告**，不把"没有限制"写成"已限制"。 */
export const TOOL_RESTRICTION = Object.freeze({
  legacy: REVIEW_TOOL_ALLOW.join('/'),
  activation: 'none (new host: toolFilter moved to Agent Preset)',
})

/**
 * 每个后端该用的**注入消息 source kind**（两代契约互斥，T-10 真机取证）：
 *   · 旧代 0.1.0-rc.5：`{ kind: 'plugin', plugin: <name> }`（`plugin` 字段必需，
 *     见 dsh-llm/lib/types/message.d.ts:98-101）
 *   · 新代 0.2.1-rc.2：session v4 格式拒绝 `kind:'plugin'`（抛 "producer-owned
 *     source kind"），插件应写 `kind:'plugin:<name>'` 且**丢弃 plugin 字段**
 *     （见 dsh-session-format-v3-to-v4/lib/index.js:126 与 producerKind 的 fallback）
 *
 * 两代都不能同时接受另一种格式 ⇒ `injectedMessage` 必须按 `detectBackend` 的结果分支。
 */
export const SOURCE_KIND = Object.freeze({
  legacy: 'plugin',
  activation: 'plugin:dsh-xrd',
})

/** 给定后端，返回它该用的注入消息 source kind。 */
export function sourceKindFor(backend) {
  return backend === 'activation' ? SOURCE_KIND.activation : SOURCE_KIND.legacy
}

/** 新代 delivery 策略：`caller` 让服务跳过向父会话的结算通知（我们自己注入结论）。 */
export const ACTIVATION_DELIVERY = 'caller'

/**
 * 判断宿主提供哪一代入口。
 *
 * 两代都在时**优先新代**（`startActivation` 是新契约；旧 `start` 在过渡期可能只是 shim）。
 *
 * @param {object|null} svc `ctx.get('subagents')`
 * @returns {'activation'|'legacy'|null} 两代都没有时返回 null（调用方据此报"服务不可用"）。
 */
export function detectBackend(svc) {
  if (!svc || typeof svc !== 'object') return null
  if (typeof svc.startActivation === 'function') return 'activation'
  if (typeof svc.start === 'function') return 'legacy'
  return null
}

/**
 * 派生一个审查子代理（两代通吃）。
 *
 * 只负责"怎么起"：参数映射与两处能力差异。持久化、超时、结果解析、dispose 都归调用方。
 *
 * @param {object} opts
 * @param {object} opts.svc `ctx.get('subagents')`
 * @param {string} opts.provider 子代理 provider 名（默认 `spawn`）
 * @param {string} opts.label 给宿主看的标签
 * @param {Array<object>} opts.promptBlocks 初始任务块（`[{type:'text',text}]`）
 * @param {object} opts.parent 精确活着的父 agent（新代服务会校验身份）
 * @param {AbortSignal} opts.signal 必需；调用方自建（不用工具调用的 signal）
 * @param {number} [opts.maxDepth] 递归深度上限（两代同值）
 * @param {object} [opts.agentOptions] 子代理路由覆盖（不给则继承父 agent）
 * @returns {Promise<{ok:true, handle:object, backend:string, toolRestriction:string}
 *                 | {ok:false, skipped:string}>}
 */
export async function startChild({
  svc = null, provider, label, promptBlocks, parent, signal,
  maxDepth = 1, agentOptions = undefined,
} = {}) {
  const backend = detectBackend(svc)
  if (backend === null) return { ok: false, skipped: 'subagents service unavailable' }
  if (!parent) return { ok: false, skipped: 'no parent agent' }
  if (!signal) return { ok: false, skipped: 'no cancellation signal' }
  if (!Array.isArray(promptBlocks) || promptBlocks.length === 0) {
    return { ok: false, skipped: 'empty prompt' }
  }

  const common = {
    prompt: promptBlocks,
    parent,
    ...(maxDepth !== undefined ? { maxDepth } : {}),
    ...(agentOptions !== undefined ? { agentOptions } : {}),
  }

  if (backend === 'activation') {
    const handle = await svc.startActivation({
      provider,
      label,
      request: common,
      signal,
      delivery: ACTIVATION_DELIVERY,
    })
    return { ok: true, handle, backend, toolRestriction: TOOL_RESTRICTION.activation }
  }

  const handle = await svc.start(provider, {
    label,
    ...common,
    signal,
    toolFilter: { allow: [...REVIEW_TOOL_ALLOW] },
  })
  return { ok: true, handle, backend, toolRestriction: TOOL_RESTRICTION.legacy }
}

// ────────────────────────────── 自检（node lib/subagent-bridge.js --selftest） ──────────────────────────────
// 假 service 覆盖四种后端形态 + 三种参数落位断言。**不需要宿主**，因此可以在任何机器上跑。
const SELFTEST_CASES = []
function check(name, cond, detail = '') {
  SELFTEST_CASES.push({ name, ok: !!cond, detail })
}

async function selftest() {
  const parent = { id: 'parent-agent' }
  const blocks = [{ type: 'text', text: 'review this' }]
  const signal = new AbortController().signal

  // ① 只有旧代 start
  {
    let got = null
    const svc = { start: async (provider, request) => { got = { provider, request }; return { result: Promise.resolve({ output: [] }), dispose() {} } } }
    const r = await startChild({ svc, provider: 'spawn', label: 'L', promptBlocks: blocks, parent, signal })
    check('legacy: ok', r.ok === true)
    check('legacy: backend', r.backend === 'legacy')
    check('legacy: provider passthrough', got && got.provider === 'spawn')
    check('legacy: label in request', got && got.request.label === 'L')
    check('legacy: parent in request', got && got.request.parent === parent)
    check('legacy: signal in request', got && got.request.signal === signal)
    check('legacy: maxDepth 1', got && got.request.maxDepth === 1)
    check('legacy: toolFilter kept', got && Array.isArray(got.request.toolFilter?.allow)
      && got.request.toolFilter.allow.join('/') === 'read/glob/grep')
    check('legacy: toolRestriction says limited', r.toolRestriction === 'read/glob/grep')
    check('legacy: handle passed through', r.handle && typeof r.handle.dispose === 'function')
  }

  // ② 只有新代 startActivation
  {
    let got = null
    const svc = { startActivation: async (spec) => { got = spec; return { childId: 'c1', result: Promise.resolve({ output: [{ type: 'text', text: 'A' }] }), dispose() {} } } }
    const r = await startChild({ svc, provider: 'spawn', label: 'L', promptBlocks: blocks, parent, signal })
    check('activation: ok', r.ok === true)
    check('activation: backend', r.backend === 'activation')
    check('activation: provider top-level', got && got.provider === 'spawn')
    check('activation: label top-level', got && got.label === 'L')
    check('activation: signal top-level', got && got.signal === signal)
    check('activation: delivery caller', got && got.delivery === 'caller')
    check('activation: prompt in request', got && got.request.prompt === blocks)
    check('activation: parent in request', got && got.request.parent === parent)
    check('activation: maxDepth in request', got && got.request.maxDepth === 1)
    check('activation: NO toolFilter sent', got && !('toolFilter' in got.request) && !('toolFilter' in got))
    check('activation: toolRestriction admits none', /none/.test(r.toolRestriction))
    check('activation: childId passthrough', r.handle && r.handle.childId === 'c1')
  }

  // ③ 两代都缺 ⇒ skipped（不是抛错）
  {
    const r = await startChild({ svc: {}, provider: 'spawn', label: 'L', promptBlocks: blocks, parent, signal })
    check('none: not ok', r.ok === false)
    check('none: skipped text', r.skipped === 'subagents service unavailable')
  }

  // ④ 没有服务 / 没有 parent / 没有 signal / 空 prompt —— 每一条都要有明确说法
  {
    const a = await startChild({ svc: null, promptBlocks: blocks, parent, signal })
    check('no svc: skipped', a.ok === false && /service unavailable/.test(a.skipped))
    const b = await startChild({ svc: { start: async () => ({}) }, promptBlocks: blocks, parent: null, signal })
    check('no parent: skipped', b.ok === false && /parent/.test(b.skipped))
    const c = await startChild({ svc: { start: async () => ({}) }, promptBlocks: blocks, parent })
    check('no signal: skipped', c.ok === false && /signal/.test(c.skipped))
    const d = await startChild({ svc: { start: async () => ({}) }, promptBlocks: [], parent, signal })
    check('empty prompt: skipped', d.ok === false && /prompt/.test(d.skipped))
  }

  // ⑤ 两代都在 ⇒ 优先新代（旧 start 不得被调用）
  {
    let legacyCalled = false
    const svc = {
      startActivation: async () => ({ childId: 'c2', result: Promise.resolve({ output: [] }), dispose() {} }),
      start: async () => { legacyCalled = true; return {} },
    }
    const r = await startChild({ svc, provider: 'spawn', label: 'L', promptBlocks: blocks, parent, signal })
    check('both: prefers activation', r.backend === 'activation')
    check('both: legacy not called', legacyCalled === false)
  }

  // ⑥ 服务抛错必须冒泡（调用方负责归类成"检测未执行"），不能被吞成 skipped
  {
    const svc = { startActivation: async () => { throw new Error('boom') } }
    let threw = null
    try { await startChild({ svc, provider: 'spawn', label: 'L', promptBlocks: blocks, parent, signal }) } catch (e) { threw = e }
    check('throw: bubbles', threw !== null && /boom/.test(String(threw && threw.message)))
  }

  // ⑦ agentOptions 只在给了的时候出现（避免把 undefined 塞进请求面）
  {
    let got = null
    const svc = { startActivation: async (spec) => { got = spec; return { childId: 'c3', result: Promise.resolve({ output: [] }), dispose() {} } } }
    await startChild({ svc, provider: 'spawn', label: 'L', promptBlocks: blocks, parent, signal })
    check('agentOptions: absent when omitted', got && !('agentOptions' in got.request))
    await startChild({ svc, provider: 'spawn', label: 'L', promptBlocks: blocks, parent, signal, agentOptions: { model: 'x' } })
    check('agentOptions: present when given', got && got.request.agentOptions && got.request.agentOptions.model === 'x')
  }

  const fail = SELFTEST_CASES.filter(c => !c.ok)
  for (const c of SELFTEST_CASES) {
    if (!c.ok) console.log(`FAIL  ${c.name}${c.detail ? ' — ' + c.detail : ''}`)
  }
  console.log(`SELFTEST ${SELFTEST_CASES.length - fail.length}/${SELFTEST_CASES.length} passed`)
  return fail.length === 0
}

const invokedDirectly = process.argv[1] && /subagent-bridge\.js$/.test(process.argv[1].replace(/\\/g, '/'))
if (invokedDirectly && process.argv.includes('--selftest')) {
  selftest().then(ok => { process.exit(ok ? 0 : 1) }, (e) => { console.error('SELFTEST CRASH', e); process.exit(1) })
}
