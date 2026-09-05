import assert from "node:assert/strict";
import { parseNatOutputString } from "../lib/nat-wire.ts";

// Every one of these is a valid JSON scalar. Parsing them changes their runtime
// type, and the joined answer then silently loses scores, expense ratios, fund
// sizes, ISINs and dates. The literal chunks a real answer is made of:
const literalDeltas = [
  "1",
  "87",
  "100",
  "0",
  "0022",
  "3600",
  "28",
  "000",
  "true",
  "false",
  "IE00BK5BQT80",
  "$28,000,000,000",
  "2026",
  "06",
  "30",
];

for (const delta of literalDeltas) {
  assert.equal(
    parseNatOutputString(delta),
    delta,
    `literal NAT output delta was type-coerced: ${delta}`,
  );
}

const reconstructed = [
  "1",
  ". **VWCE-XETRA** — ISIN ",
  "IE00BK5BQT80",
  "\n- **Investment score:** ",
  "87",
  "/",
  "100",
  "\n- **TER:** ",
  "0",
  ".",
  "0022",
  "\n- **Holdings:** ",
  "3600",
  "\n- **Data as of:** ",
  "2026",
  "-",
  "06",
  "-",
  "30",
].map((chunk) => parseNatOutputString(chunk)).join("");

assert.equal(
  reconstructed,
  "1. **VWCE-XETRA** — ISIN IE00BK5BQT80\n- **Investment score:** 87/100" +
    "\n- **TER:** 0.0022\n- **Holdings:** 3600\n- **Data as of:** 2026-06-30",
);

const structured = parseNatOutputString(
  '{"choices":[{"delta":{"content":"87"}}]}',
);
assert.equal(typeof structured, "object");
assert.equal(structured.choices[0].delta.content, "87");

console.log("PASS: NAT scalar answer chunks remain literal text");
console.log("PASS: nested structured ChatResponse JSON is still decoded");
