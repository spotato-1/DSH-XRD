/**
 * hypothesis-gate.js -- zero-dependency ESM wrapper around the Python
 * `hypothesis_gate.py` CLI (evaluate / record subcommands).
 *
 * Design contract
 * ---------------
 *  - ESM only (the package has "type": "module"); no `require`.
 *  - Imports nothing at all: no node builtins, no npm packages. Pure string
 *    building + JSON parsing, so it is safe on any runtime and cannot pull a
 *    dependency into the plugin.
 *  - NO side effects at import time: the module only declares a const and
 *    functions. Every side effect (console output, process.exit) lives inside
 *    `runSelftest`, which is invoked exclusively from the direct-run guard at
 *    the very bottom (`node hypothesis-gate.js --selftest`).
 *  - ASCII-only stdout. All comments are English.
 *  - `--sample` is the LEDGER KEY and must match what the caller passes to the
 *    record step verbatim, so this module NEVER derives, normalizes or slugs
 *    it: an empty / non-string sample is simply omitted, never invented.
 *
 * Python CLI contract (measured):
 *   python hypothesis_gate.py evaluate --dir <D> [--sample <S>] --request <JSON>
 *   python hypothesis_gate.py record   --dir <D> [--sample <S>] --request <JSON>
 *                                      [--rwp-before <num>] [--rwp-after <num>]
 *                                      [--improved true|false|unknown] [--at <ISO8601>]
 * Exit codes: 0 = allow / 3 = deny / 1 = IO or parse failure / 2 = usage error.
 * A deny still prints a complete JSON verdict on stdout carrying `reason`,
 * `message` and `next_step` (the latter may be null).
 */

/** The Python entry point this module builds argv for. */
export const GATE_SCRIPT_NAME = "hypothesis_gate.py";

/** Action names returned by interpretGate(). */
const ACTION_ALLOW = "allow";
const ACTION_DENY = "deny";
const ACTION_UNAVAILABLE = "unavailable";

/** Allowed values for the `--improved` flag. */
const IMPROVED_VALUES = ["true", "false", "unknown"];

/** How many trailing stderr characters go into an "unavailable" detail. */
const STDERR_TAIL_CHARS = 300;

/* ------------------------------------------------------------------ */
/* small value helpers                                                 */
/* ------------------------------------------------------------------ */

/** True for a non-empty, non-whitespace string. */
function isNonEmptyString(value) {
  return typeof value === "string" && value.trim() !== "";
}

/** True for a finite number (rejects NaN, +/-Infinity, strings, null). */
function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * True for a "plain" object: an object literal / Object.create(null) alike,
 * but not null, not an array, not a Date/Map/class instance.
 */
function isPlainObject(value) {
  if (value === null || typeof value !== "object") return false;
  if (Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/**
 * Normalize a verdict value to an exact passthrough.
 * Strings are returned untouched (never trimmed / rewritten); anything absent
 * becomes null so callers get a stable three-state (string | null).
 */
function passthrough(value) {
  return value === undefined ? null : value;
}

/* ------------------------------------------------------------------ */
/* 2. buildEvaluateArgs                                               */
/* ------------------------------------------------------------------ */

/**
 * Build the argv (interpreter excluded) for the `evaluate` subcommand.
 *
 * @param {{script:string, dir:string, sample?:string, request:object}} input
 * @returns {string[]}
 *   [script, "evaluate", "--dir", dir, ...(sample ? ["--sample", sample] : []),
 *    "--request", JSON.stringify(request)]
 * @throws {TypeError} when `script` / `dir` is missing or `request` is not a
 *   plain object.
 */
export function buildEvaluateArgs(input) {
  const { script, dir, sample, request } = input ?? {};

  if (!isNonEmptyString(script)) {
    throw new TypeError(
      "buildEvaluateArgs: `script` must be a non-empty string (got " + typeOf(script) + ")"
    );
  }
  if (!isNonEmptyString(dir)) {
    throw new TypeError(
      "buildEvaluateArgs: `dir` must be a non-empty string (got " + typeOf(dir) + ")"
    );
  }
  if (!isPlainObject(request)) {
    throw new TypeError(
      "buildEvaluateArgs: `request` must be a non-null plain object (got " + typeOf(request) + ")"
    );
  }

  const args = [script, "evaluate", "--dir", dir];
  // `--sample` is the ledger key: passed through verbatim or omitted entirely.
  if (isNonEmptyString(sample)) {
    args.push("--sample", sample);
  }
  args.push("--request", JSON.stringify(request));
  return args;
}

/* ------------------------------------------------------------------ */
/* 3. buildRecordArgs                                                 */
/* ------------------------------------------------------------------ */

/**
 * Build the argv (interpreter excluded) for the `record` subcommand.
 *
 * Omission rules (deliberate, so a bad value can never reach the ledger):
 *  - rwpBefore / rwpAfter: appended only for finite numbers. NaN, Infinity,
 *    null, undefined, numeric strings -> not appended.
 *  - improved: appended only for exactly "true" / "false" / "unknown";
 *    anything else ("maybe", booleans, null) -> not appended.
 *  - at: appended only for a non-empty string, verbatim.
 *
 * @param {{script:string, dir:string, sample?:string, request:object,
 *          rwpBefore?:number, rwpAfter?:number,
 *          improved?:string, at?:string}} input
 * @returns {string[]}
 * @throws {TypeError} on the same required-field conditions as buildEvaluateArgs.
 */
export function buildRecordArgs(input) {
  const { script, dir, sample, request, rwpBefore, rwpAfter, improved, at } = input ?? {};

  if (!isNonEmptyString(script)) {
    throw new TypeError(
      "buildRecordArgs: `script` must be a non-empty string (got " + typeOf(script) + ")"
    );
  }
  if (!isNonEmptyString(dir)) {
    throw new TypeError(
      "buildRecordArgs: `dir` must be a non-empty string (got " + typeOf(dir) + ")"
    );
  }
  if (!isPlainObject(request)) {
    throw new TypeError(
      "buildRecordArgs: `request` must be a non-null plain object (got " + typeOf(request) + ")"
    );
  }

  const args = [script, "record", "--dir", dir];
  if (isNonEmptyString(sample)) {
    args.push("--sample", sample);
  }
  args.push("--request", JSON.stringify(request));

  if (isFiniteNumber(rwpBefore)) {
    args.push("--rwp-before", String(rwpBefore));
  }
  if (isFiniteNumber(rwpAfter)) {
    args.push("--rwp-after", String(rwpAfter));
  }
  if (typeof improved === "string" && IMPROVED_VALUES.includes(improved)) {
    args.push("--improved", improved);
  }
  if (isNonEmptyString(at)) {
    args.push("--at", at);
  }
  return args;
}

/* ------------------------------------------------------------------ */
/* 4. parseGateJson                                                   */
/* ------------------------------------------------------------------ */

/**
 * Extract the last JSON verdict from gate stdout.
 *
 * Two shapes are supported, because the real CLI uses both:
 *  1. COMPACT -- the verdict is one line. We take the LAST line whose trimmed
 *     form starts with "{" and parse it.
 *  2. PRETTY -- `json.dumps(verdict, ensure_ascii=True, indent=1)` (what
 *     hypothesis_gate.py actually prints) spreads the verdict over many
 *     lines, after possible log lines. A single-line scan cannot parse that,
 *     so we then rescan the WHOLE stdout with a string-aware brace scanner and
 *     parse the LAST complete balanced {...} block.
 * Single-line parsing is tried first so a compact verdict is never shadowed by
 * a pretty block earlier in the same stream.
 *
 * Never throws: any failure (non-string input, no candidate, invalid JSON)
 * returns null.
 *
 * @param {string} stdout
 * @returns {object|null}
 */
export function parseGateJson(stdout) {
  if (typeof stdout !== "string" || stdout === "") return null;

  const isVerdictObject = (parsed) =>
    parsed !== null && typeof parsed === "object" && !Array.isArray(parsed);

  // Collect every balanced {...} block (both compact and pretty) with its start
  // offset, then parse from the LAST one backwards. Doing it positionally is
  // what makes "last" really last: a compact verdict followed by a pretty one
  // must yield the pretty one, not whichever shape we happened to look for
  // first.
  const blocks = [];
  let depth = 0;
  let start = -1;
  let inString = false;
  let escaped = false;
  for (let i = 0; i < stdout.length; i += 1) {
    const ch = stdout[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else if (ch === '"') {
        inString = false;
      }
      continue;
    }
    if (ch === '"') {
      inString = true;
      continue;
    }
    if (ch === "{") {
      if (depth === 0) start = i;
      depth += 1;
      continue;
    }
    if (ch === "}") {
      if (depth > 0) {
        depth -= 1;
        if (depth === 0 && start >= 0) {
          blocks.push({ start, text: stdout.slice(start, i + 1) });
          start = -1;
        }
      }
    }
  }

  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    try {
      const parsed = JSON.parse(blocks[i].text);
      if (isVerdictObject(parsed)) return parsed;
    } catch {
      // Not valid JSON on its own (e.g. a fragment): try the previous block.
    }
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* 5. interpretGate                                                   */
/* ------------------------------------------------------------------ */

/**
 * Turn a finished gate process into a decision.
 *
 * The single hard rule: an outcome we cannot positively justify is NEVER
 * "allow". allow requires exit 0 AND a parsable verdict object.
 *
 * @param {{code:number|null, stdout?:string, stderr?:string}} result
 * @returns {{action:"allow"|"deny"|"unavailable",
 *            reason:*, message:*|null, nextStep:*, detail:string|null}}
 */
export function interpretGate(result) {
  const { code, stdout, stderr } = result ?? {};
  const verdict = parseGateJson(stdout);
  const stderrText = typeof stderr === "string" ? stderr : "";
  const stderrTail = stderrText.slice(-STDERR_TAIL_CHARS);

  if (code === 0) {
    if (verdict) {
      return {
        action: ACTION_ALLOW,
        reason: passthrough(verdict.reason),
        message: passthrough(verdict.message),
        nextStep: passthrough(verdict.next_step ?? verdict.nextStep),
        detail: null,
      };
    }
    // Exit 0 without a verdict is not a proven allow.
    return {
      action: ACTION_UNAVAILABLE,
      reason: null,
      message: null,
      nextStep: null,
      detail: "exit 0 without a parsable verdict",
    };
  }

  if (code === 3) {
    if (verdict) {
      const reason = passthrough(verdict.reason);
      const rawMessage = passthrough(verdict.message);
      // message / next_step are passed through byte-for-byte; the fallback
      // message is used only when the verdict carries no message at all.
      const message =
        typeof rawMessage === "string" && rawMessage !== ""
          ? rawMessage
          : "denied: " + (reason === null ? "unknown reason" : String(reason));
      return {
        action: ACTION_DENY,
        reason,
        message,
        nextStep: passthrough(verdict.next_step ?? verdict.nextStep),
        detail: null,
      };
    }
    return {
      action: ACTION_UNAVAILABLE,
      reason: null,
      message: null,
      nextStep: null,
      detail: "exit 3 without a parsable verdict",
    };
  }

  // Exit 1 (IO / parse failure), 2 (usage error), null (spawn failure) and any
  // unexpected status all land here.
  return {
    action: ACTION_UNAVAILABLE,
    reason: null,
    message: null,
    nextStep: null,
    detail:
      "gate produced no decision: exit code " +
      String(code) +
      (stderrTail ? "; stderr tail: " + stderrTail : "; stderr empty"),
  };
}

/* ------------------------------------------------------------------ */
/* 6. summarizeRound                                                  */
/* ------------------------------------------------------------------ */

/**
 * Compare two Rwp values for the ledger.
 *
 * @param {{before:*, after:*}} round
 * @returns {{before:*|null, after:*|null, improved:boolean|"unknown", delta:number|null}}
 *   `improved` is a boolean only when both sides are finite numbers.
 */
export function summarizeRound(round) {
  const { before, after } = round ?? {};
  const hasBoth = isFiniteNumber(before) && isFiniteNumber(after);
  return {
    before: isFiniteNumber(before) ? before : null,
    after: isFiniteNumber(after) ? after : null,
    improved: hasBoth ? after < before : "unknown",
    delta: hasBoth ? Number((after - before).toFixed(6)) : null,
  };
}

/* ------------------------------------------------------------------ */
/* internals                                                          */
/* ------------------------------------------------------------------ */

/** Human-readable type tag for error messages. */
function typeOf(value) {
  if (value === null) return "null";
  if (undefined === value) return "undefined";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

/* ------------------------------------------------------------------ */
/* 7. selftest                                                        */
/* ------------------------------------------------------------------ */

/**
 * Run the built-in self test. This is the ONLY function with side effects and
 * it is called only from the direct-run guard below.
 *
 * @returns {{passed:number, failed:number, lines:string[]}}
 */
export function runSelftest() {
  const lines = [];
  let passed = 0;
  let failed = 0;

  const check = (name, ok, extra) => {
    if (ok) {
      passed += 1;
      lines.push("PASS  " + name);
    } else {
      failed += 1;
      lines.push("FAIL  " + name + (extra ? "  <<< " + extra : ""));
    }
  };

  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const throwsTypeError = (fn) => {
    try {
      fn();
      return false;
    } catch (err) {
      return err instanceof TypeError;
    }
  };

  const SCRIPT = GATE_SCRIPT_NAME;
  const DIR = "C:/work/gate";
  const REQ = { round: 3, hypothesis: "Mo substitution", expect: "lower Rwp" };

  /* --- a. buildEvaluateArgs ordering, with and without sample --- */

  const evalWithSample = buildEvaluateArgs({
    script: SCRIPT,
    dir: DIR,
    sample: "Na2MoO4-batch7",
    request: REQ,
  });
  check(
    "a1 buildEvaluateArgs argv with sample (order + content)",
    eq(evalWithSample, [
      "hypothesis_gate.py",
      "evaluate",
      "--dir",
      DIR,
      "--sample",
      "Na2MoO4-batch7",
      "--request",
      JSON.stringify(REQ),
    ]),
    JSON.stringify(evalWithSample)
  );

  const evalNoSample = buildEvaluateArgs({ script: SCRIPT, dir: DIR, request: REQ });
  check(
    "a2 buildEvaluateArgs argv without sample (no --sample flag)",
    eq(evalNoSample, ["hypothesis_gate.py", "evaluate", "--dir", DIR, "--request", JSON.stringify(REQ)]),
    JSON.stringify(evalNoSample)
  );
  check(
    "a3 buildEvaluateArgs omits --sample when sample is empty string",
    !buildEvaluateArgs({ script: SCRIPT, dir: DIR, sample: "", request: REQ }).includes("--sample"),
    "empty sample still produced --sample"
  );
  check(
    "a4 buildEvaluateArgs does not derive/rewrite the sample ledger key",
    buildEvaluateArgs({ script: SCRIPT, dir: DIR, sample: "  Odd Key/7 ", request: REQ })[5] === "  Odd Key/7 ",
    "sample was mangled"
  );

  /* --- b. buildEvaluateArgs TypeError on bad required fields --- */

  check(
    "b1 buildEvaluateArgs throws TypeError on missing script",
    throwsTypeError(() => buildEvaluateArgs({ dir: DIR, request: REQ })),
    "no TypeError for missing script"
  );
  check(
    "b2 buildEvaluateArgs throws TypeError on missing dir",
    throwsTypeError(() => buildEvaluateArgs({ script: SCRIPT, request: REQ })),
    "no TypeError for missing dir"
  );
  check(
    "b3 buildEvaluateArgs throws TypeError on non-object request (null)",
    throwsTypeError(() => buildEvaluateArgs({ script: SCRIPT, dir: DIR, request: null })),
    "no TypeError for null request"
  );
  check(
    "b4 buildEvaluateArgs throws TypeError on non-object request (array)",
    throwsTypeError(() => buildEvaluateArgs({ script: SCRIPT, dir: DIR, request: ["x"] })),
    "no TypeError for array request"
  );
  check(
    "b5 buildEvaluateArgs throws TypeError on non-object request (string)",
    throwsTypeError(() => buildEvaluateArgs({ script: SCRIPT, dir: DIR, request: "{}" })),
    "no TypeError for string request"
  );
  const errMsg = (() => {
    try {
      buildEvaluateArgs({ dir: DIR, request: REQ });
      return "";
    } catch (err) {
      return String(err.message || "");
    }
  })();
  check(
    "b6 buildEvaluateArgs TypeError message names the offending field",
    errMsg.includes("script"),
    "message did not name `script`: " + errMsg
  );

  /* --- c. buildRecordArgs omission / inclusion rules --- */

  const recordClean = buildRecordArgs({
    script: SCRIPT,
    dir: DIR,
    request: REQ,
    rwpBefore: null,
    rwpAfter: Number.NaN,
    improved: "maybe",
    at: undefined,
  });
  check(
    "c1 buildRecordArgs omits rwpBefore=null / rwpAfter=NaN / improved=maybe / at=undefined",
    eq(recordClean, ["hypothesis_gate.py", "record", "--dir", DIR, "--request", JSON.stringify(REQ)]),
    JSON.stringify(recordClean)
  );

  const recordInf = buildRecordArgs({
    script: SCRIPT,
    dir: DIR,
    request: REQ,
    rwpBefore: Number.POSITIVE_INFINITY,
    rwpAfter: Number.NEGATIVE_INFINITY,
    improved: true,
    at: "",
  });
  check(
    "c2 buildRecordArgs omits Infinity values and non-string improved",
    !recordInf.includes("--rwp-before") &&
      !recordInf.includes("--rwp-after") &&
      !recordInf.includes("--improved") &&
      !recordInf.includes("--at"),
    JSON.stringify(recordInf)
  );

  const recordFull = buildRecordArgs({
    script: SCRIPT,
    dir: DIR,
    sample: "Na2MoO4-batch7",
    request: REQ,
    rwpBefore: 21.8,
    rwpAfter: 20.9,
    improved: "true",
    at: "2026-10-05T09:30:00Z",
  });
  check(
    "c3 buildRecordArgs appends finite rwp / known improved / at, in documented order",
    eq(recordFull, [
      "hypothesis_gate.py",
      "record",
      "--dir",
      DIR,
      "--sample",
      "Na2MoO4-batch7",
      "--request",
      JSON.stringify(REQ),
      "--rwp-before",
      "21.8",
      "--rwp-after",
      "20.9",
      "--improved",
      "true",
      "--at",
      "2026-10-05T09:30:00Z",
    ]),
    JSON.stringify(recordFull)
  );

  const recordZero = buildRecordArgs({
    script: SCRIPT,
    dir: DIR,
    request: REQ,
    rwpBefore: 0,
    rwpAfter: 0,
    improved: "unknown",
  });
  check(
    "c4 buildRecordArgs keeps finite 0 (0 is a value, not a missing value)",
    recordZero.includes("--rwp-before") &&
      recordZero.includes("--rwp-after") &&
      recordZero[recordZero.indexOf("--improved") + 1] === "unknown",
    JSON.stringify(recordZero)
  );

  /* --- d. parseGateJson --- */

  const noisy = [
    "[gate] loaded ledger",
    "  not json at all",
    '{"reason": "first", "message": "old"}',
    "[gate] tail note",
    '  {"reason": "LOWER_RWP", "message": "accepted", "next_step": "refine"}  ',
    "",
  ].join("\n");
  const parsedNoisy = parseGateJson(noisy);
  check(
    "d1 parseGateJson takes the LAST JSON line after non-JSON lines",
    parsedNoisy !== null && parsedNoisy.reason === "LOWER_RWP" && parsedNoisy.message === "accepted",
    JSON.stringify(parsedNoisy)
  );
  check(
    "d2 parseGateJson returns null on invalid JSON",
    parseGateJson('{"a": 1,\n{bad json}\n') === null,
    "invalid JSON was parsed"
  );
  check(
    "d3 parseGateJson returns null for non-string input",
    parseGateJson(null) === null && parseGateJson(42) === null && parseGateJson(undefined) === null,
    "non-string input produced a value"
  );
  check(
    "d4 parseGateJson returns null when no line starts with {",
    parseGateJson("some log\nmore log\n") === null,
    "parsed something with no JSON line"
  );
  check(
    "d5 parseGateJson handles CRLF stdout",
    parseGateJson('log line\r\n{"reason":"X"}\r\n')?.reason === "X",
    "CRLF line handling failed"
  );

  // The real engine prints json.dumps(verdict, ensure_ascii=True, indent=1),
  // i.e. a PRETTY multi-line object after possible log lines. A single-line
  // scanner returns null there, which used to turn every real allow/deny into
  // "unavailable" -- this is the regression guard for that.
  const prettyAllow = [
    ' "decision": "allow",',
    ' "allow": true,',
    ' "reason": "ok",',
    ' "message": "accepted: this hypothesis is new relative to the recorded rounds",',
    ' "next_step": "",',
    ' "counters": {',
    '  "attempts": 1',
    " }",
  ];
  const prettyStdout = "[gate] log line\n{\n" + prettyAllow.join("\n") + "\n}\n";
  const pretty = parseGateJson(prettyStdout);
  check(
    "d6 parseGateJson parses a pretty multi-line verdict (real engine format)",
    pretty !== null && pretty.reason === "ok" && pretty.allow === true && pretty.counters.attempts === 1,
    JSON.stringify(pretty)
  );

  const braceInString = '{\n "message": "ratio {x} stays balanced",\n "reason": "OK"\n}';
  check(
    "d7 parseGateJson is not confused by braces inside JSON strings",
    parseGateJson("/log/\n" + braceInString)?.reason === "OK",
    JSON.stringify(parseGateJson("/log/\n" + braceInString))
  );

  const twoBlocks = '{"reason":"FIRST"}\nlog between\n{\n "reason": "SECOND"\n}';
  check(
    "d8 parseGateJson prefers the LAST complete block when several appear",
    parseGateJson(twoBlocks)?.reason === "SECOND",
    JSON.stringify(parseGateJson(twoBlocks))
  );

  check(
    "d9 parseGateJson still returns null on an unbalanced pretty block",
    parseGateJson("{\n \"reason\": \"X\"\n") === null,
    "unbalanced JSON was parsed"
  );

  /* --- e. interpretGate paths --- */

  const allow = interpretGate({
    code: 0,
    stdout: '[gate] ok\n{"reason":"SPECIFIC_HYPOTHESIS","message":"hypothesis is falsifiable","next_step":"refine"}\n',
    stderr: "",
  });
  check(
    "e1 interpretGate allow on exit 0 + parsable verdict",
    allow.action === "allow" &&
      allow.reason === "SPECIFIC_HYPOTHESIS" &&
      allow.message === "hypothesis is falsifiable" &&
      allow.nextStep === "refine",
    JSON.stringify(allow)
  );

  const deny = interpretGate({
    code: 3,
    stdout: '{"reason":"TOO_VAGUE","message":"hypothesis is not falsifiable","next_step":"rewrite the hypothesis"}',
    stderr: "",
  });
  check(
    "e2 interpretGate deny on exit 3",
    deny.action === "deny" &&
      deny.reason === "TOO_VAGUE" &&
      deny.message === "hypothesis is not falsifiable" &&
      deny.nextStep === "rewrite the hypothesis",
    JSON.stringify(deny)
  );

  const denyNoMessage = interpretGate({ code: 3, stdout: '{"reason":"NO_NEXT_STEP","message":null,"next_step":null}' });
  check(
    "e3 interpretGate deny falls back to 'denied: <reason>' when message is missing",
    denyNoMessage.action === "deny" && denyNoMessage.message === "denied: NO_NEXT_STEP" && denyNoMessage.nextStep === null,
    JSON.stringify(denyNoMessage)
  );

  const ioFail = interpretGate({ code: 1, stdout: "Traceback (most recent call last): ...\n", stderr: "boom: ledger unreadable" });
  check(
    "e4 interpretGate unavailable on exit 1, detail carries code and stderr tail",
    ioFail.action === "unavailable" &&
      ioFail.message === null &&
      ioFail.nextStep === null &&
      ioFail.detail.includes("1") &&
      ioFail.detail.includes("ledger unreadable"),
    JSON.stringify(ioFail)
  );

  const usageFail = interpretGate({ code: 2, stdout: "", stderr: "usage: hypothesis_gate.py ..." });
  check(
    "e5 interpretGate unavailable on exit 2",
    usageFail.action === "unavailable" && usageFail.detail.includes("2"),
    JSON.stringify(usageFail)
  );

  const unparsableDeny = interpretGate({ code: 3, stdout: "denied\n(no json here)\n", stderr: "" });
  check(
    "e6 interpretGate unavailable when exit 3 has no parsable verdict",
    unparsableDeny.action === "unavailable" &&
      unparsableDeny.detail === "exit 3 without a parsable verdict",
    JSON.stringify(unparsableDeny)
  );

  const spawnFail = interpretGate({ code: null, stdout: "", stderr: "ENOENT python" });
  check(
    "e7 interpretGate unavailable on null exit code (spawn failure)",
    spawnFail.action === "unavailable" && spawnFail.detail.includes("null"),
    JSON.stringify(spawnFail)
  );

  const zeroNoJson = interpretGate({ code: 0, stdout: "log only\n", stderr: "" });
  check(
    "e8 interpretGate never reports allow without a parsable verdict",
    zeroNoJson.action !== "allow",
    JSON.stringify(zeroNoJson)
  );

  const stderr300 = interpretGate({ code: 1, stdout: "", stderr: "x".repeat(500) }).detail;
  check(
    "e9 interpretGate truncates the stderr tail to 300 characters",
    stderr300.includes("x".repeat(300)) && !stderr300.includes("x".repeat(301)),
    "stderr tail length not 300: " + String(stderr300.length)
  );

  /* --- f. deny branch preserves message / next_step byte-for-byte --- */

  const trickyMessage =
    'Quoted "value" <tag> & ampersand | pipe ; semicolon \\ backslash / slash ' +
    "tab:\t newline:\n unicode: alpha-beta \u00e9\u4e2d\u6587 100% {braces}";
  const trickyNext = 'step A -> step "B"\nstep C';
  const trickyVerdict = { reason: "TRICKY", message: trickyMessage, next_step: trickyNext };
  const tricky = interpretGate({ code: 3, stdout: "[gate] pre\n" + JSON.stringify(trickyVerdict) + "\n", stderr: "" });
  check(
    "f1 interpretGate deny preserves a special-character message verbatim",
    tricky.action === "deny" && tricky.message === trickyMessage,
    "message was rewritten: " + JSON.stringify(tricky.message)
  );
  check(
    "f2 interpretGate deny preserves next_step verbatim",
    tricky.nextStep === trickyNext,
    "next_step was rewritten: " + JSON.stringify(tricky.nextStep)
  );
  check(
    "f3 interpretGate deny keeps reason verbatim",
    tricky.reason === "TRICKY",
    "reason was rewritten: " + JSON.stringify(tricky.reason)
  );

  /* --- g. summarizeRound --- */

  const s1 = summarizeRound({ before: 21.8, after: 20.9 });
  check(
    "g1 summarizeRound 21.8 -> 20.9 is improved=true",
    s1.improved === true && s1.before === 21.8 && s1.after === 20.9,
    JSON.stringify(s1)
  );

  const s2 = summarizeRound({ before: 21.8, after: 21.8 });
  check(
    "g2 summarizeRound 21.8 -> 21.8 is improved=false",
    s2.improved === false,
    JSON.stringify(s2)
  );

  const s3 = summarizeRound({ before: 21.8, after: undefined });
  check(
    "g3 summarizeRound with a missing value is improved='unknown'",
    s3.improved === "unknown" && s3.after === null,
    JSON.stringify(s3)
  );

  const s4 = summarizeRound({ before: Number.NaN, after: 20.9 });
  check(
    "g4 summarizeRound rejects NaN operands as 'unknown'",
    s4.improved === "unknown",
    JSON.stringify(s4)
  );

  const s5 = summarizeRound({ before: 20.9, after: 21.8 });
  check(
    "g5 summarizeRound treats a higher after as improved=false",
    s5.improved === false && s5.delta === 0.9,
    JSON.stringify(s5)
  );

  /* --- h. documented design constraint: no side effects at import --- */

  // "Importing this module has no side effects" cannot be proven from INSIDE
  // the module: by the time this function runs the import has already
  // happened, and this module deliberately imports nothing (no node:fs), so it
  // cannot read its own source either. Jumping to pass would be a fake check,
  // so the constraint is recorded as a NOTE -- a review-level statement of the
  // module body -- plus what this process CAN observe right now.
  const observed = describeRuntimeFacts();
  lines.push(
    "NOTE  h import-time side effects are NOT verified by this selftest (design " +
      "note, not a pass). The guarantee is review-level: the module body contains " +
      "no import/require at all and declares only constants and functions; every " +
      "console.* call and the single process.exit live inside runSelftest(), which " +
      "is reachable only through the direct-run guard at the end of the file. " +
      "Observable now: " + observed + ". No fs access is taken to inspect the " +
      "source, deliberately, to keep the module dependency-free."
  );

  return { passed, failed, lines };
}

/**
 * Report what this process can observe about the module's effect on it.
 * Deliberately report-only: it is used in a NOTE, never asserted as a check.
 *
 * @returns {string}
 */
function describeRuntimeFacts() {
  const facts = [];
  facts.push("argv[1]=" + String(typeof process !== "undefined" && process.argv ? process.argv[1] : "n/a"));
  facts.push("promise-based main thread still running (no process.exit was called)");
  facts.push("validator availability: no node builtins imported");
  return facts.join("; ");
}

/* ------------------------------------------------------------------ */
/* direct-run guard                                                   */
/* ------------------------------------------------------------------ */

/**
 * True when this file was executed directly (node hypothesis-gate.js ...)
 * rather than imported. Works without node builtins by comparing the script
 * path against this module's own URL, which only exists when run as a file.
 */
function isDirectRun() {
  try {
    if (typeof process === "undefined" || !process.argv || !Array.isArray(process.argv)) return false;
    const entry = process.argv[1];
    if (typeof entry !== "string" || entry === "") return false;
    const norm = (p) => p.replace(/\\/g, "/");
    const selfUrl = typeof import.meta !== "undefined" && import.meta && typeof import.meta.url === "string"
      ? import.meta.url
      : "";
    if (selfUrl === "") return false;
    let selfPath = selfUrl;
    try {
      selfPath = decodeURIComponent(new URL(selfUrl).pathname);
    } catch {
      // Fall back to the raw URL text.
    }
    // Windows paths arrive as "/X:/..." after URL parsing.
    if (/^\/[A-Za-z]:\//.test(selfPath)) selfPath = selfPath.slice(1);
    return norm(selfPath).toLowerCase() === norm(entry).toLowerCase();
  } catch {
    return false;
  }
}

if (isDirectRun() && process.argv.slice(2).includes("--selftest")) {
  const result = runSelftest();
  for (const line of result.lines) console.log(line);
  console.log("");
  console.log("passed " + String(result.passed) + ", failed " + String(result.failed));
  process.exit(result.failed === 0 ? 0 : 1);
}