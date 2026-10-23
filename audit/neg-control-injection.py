#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""neg-control-injection.py — prove the new message-shape assertions can FAIL.

Mutates review.js back to the 0.8.0 defect (an injected message with no `source`),
runs the detector suite, requires the shape assertions to fail, then restores the file
byte-exactly. A check that cannot fail is not a check — and this one exists because the
stub suite passed 108/108 while the real host crashed on every injection.

Usage: python neg-control-injection.py [<plugin root>]
"""
import subprocess
import sys
from pathlib import Path

# Paths resolve from THIS file, never a literal maintainer path (this module ships
# inside the package as audit/ — see the codex review of 2026-09-27).
HERE = Path(__file__).resolve().parent
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else (HERE.parent.parent / "dsh-xrd")
REVIEW = ROOT / "lib" / "review.js"
SUITE = HERE / "verify-review-detector.mjs"

original = REVIEW.read_text(encoding="utf-8")
# the defect: build the message WITHOUT the source field
MUTANT = """export function injectedMessage(text, plugin = 'dsh-xrd') {
  const summary = String(text).replace(/\\s+/g, ' ').trim().slice(0, 120)
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
  }
}"""
start = original.index("export function injectedMessage(")
end = original.index("\n}\n", start) + len("\n}\n")
mutated = original[:start] + MUTANT + "\n" + original[end:]
if mutated == original:
    sys.exit("mutation produced no change — the control is meaningless")


def run_suite():
    out = subprocess.run(["node", str(SUITE)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return out.stdout + out.stderr


try:
    REVIEW.write_text(mutated, encoding="utf-8")
    mutated_out = run_suite()
finally:
    REVIEW.write_text(original, encoding="utf-8")
restored_ok = REVIEW.read_text(encoding="utf-8") == original
restored_out = run_suite()


def shape_section(text):
    lines, on = [], False
    for ln in text.splitlines():
        if ln.startswith("== 注入消息必须是完整的 UserMessage"):
            on = True
        elif on and ln.strip().startswith(("ALL PASS", "FAILURES")):
            on = False
        if on:
            lines.append(ln)
    return lines


fails_mut = [ln for ln in shape_section(mutated_out) if "FAIL" in ln]
summary_mut = [ln for ln in mutated_out.splitlines() if "ALL PASS" in ln or "FAILURES" in ln]
fails_res = [ln for ln in shape_section(restored_out) if "FAIL" in ln]
summary_res = [ln for ln in restored_out.splitlines() if "ALL PASS" in ln or "FAILURES" in ln]

print("--- control 1: message built WITHOUT source (the 0.8.0 defect) ---")
for ln in fails_mut:
    print(ln.strip())
print("suite verdict:", summary_mut[-1].strip() if summary_mut else "(none)")
print("--- control 2: file restored ---")
print("shape-section FAILs:", len(fails_res))
print("suite verdict:", summary_res[-1].strip() if summary_res else "(none)")
print(f"file restored byte-exact: {restored_ok}")
verdict = "PASS" if fails_mut and not fails_res and restored_ok else "FAIL"
print("VERDICT:", verdict)
sys.exit(0 if verdict == "PASS" else 1)
