/**
 * Streaming-contract regression test for the NAT SSE wire format.
 *
 * Runs with `npm run verify:nat-wire`. It needs no browser, no running stack
 * and no model, so it is safe in pull-request CI.
 */
import assert from "node:assert/strict";
import { parseNatOutputString } from "../lib/nat-wire.ts";

// Every one of these is a valid JSON scalar. Parsing them changes their runtime
// type, and the joined answer then silently loses amounts, counts, identifiers
// and dates. These are the literal chunks a real answer is made of.
const literalDeltas = [
  "1",
  "0",
  "5",
  "12",
  "100",
  "1001",
  "0042",
  "true",
  "false",
  "null",
  "4250",
  "75",
  "2026",
  "06",
  "30",
  "-",
  "1e3",
  "NaN",
];

for (const delta of literalDeltas) {
  const parsed = parseNatOutputString(delta);
  assert.equal(
    typeof parsed,
    "string",
    `literal NAT output delta changed type: ${delta} -> ${typeof parsed}`,
  );
  assert.equal(parsed, delta, `literal NAT output delta was rewritten: ${delta}`);
}

// Reconstructing a streamed answer from its per-token deltas must be lossless.
const reconstructed = [
  "Ticket TKT-",
  "1001",
  " has ",
  "5",
  " replies totalling $",
  "4250",
  ".",
  "75",
  " in refunds. Escalated: ",
  "true",
  ". Opened ",
  "2026",
  "-",
  "06",
  "-",
  "30",
  ". Priority score ",
  "100",
  "/",
  "100",
  ".",
]
  .map((chunk) => parseNatOutputString(chunk))
  .join("");

assert.equal(
  reconstructed,
  "Ticket TKT-1001 has 5 replies totalling $4250.75 in refunds. Escalated: true." +
    " Opened 2026-06-30. Priority score 100/100.",
);

// Whitespace-only and empty deltas round-trip unchanged rather than collapsing.
assert.equal(parseNatOutputString(""), "");
assert.equal(parseNatOutputString(" "), " ");
assert.equal(parseNatOutputString("\n"), "\n");

// Nested structured ChatResponse JSON is still decoded, so the chat route can
// reach into `choices[0].delta.content`.
const structured = parseNatOutputString('{"choices":[{"delta":{"content":"100"}}]}');
assert.equal(typeof structured, "object");
assert.equal(structured.choices[0].delta.content, "100");
assert.equal(
  typeof structured.choices[0].delta.content,
  "string",
  "content inside a decoded container must stay a string",
);

const structuredArray = parseNatOutputString('[{"content":"a"}]');
assert.ok(Array.isArray(structuredArray));

// Text that merely starts or ends with a brace is not a container and must be
// preserved verbatim, including malformed JSON.
for (const notAContainer of [
  "{not json}",
  "{",
  "}",
  "[unclosed",
  "use {tools} for every factual claim",
]) {
  assert.equal(
    parseNatOutputString(notAContainer),
    notAContainer,
    `non-container text was altered: ${notAContainer}`,
  );
}

console.log("PASS: NAT scalar answer chunks remain literal text");
console.log("PASS: streamed answer reconstruction is lossless");
console.log("PASS: nested structured ChatResponse JSON is still decoded");
