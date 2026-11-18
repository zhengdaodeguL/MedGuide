import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/copy-text.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
});
const { copyAnswerText } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

// The answer body is plain text: the bubble renders `content` split on
// newlines, while citations and latency live in separate UI. Copying must
// pass that text through verbatim — no citation markers, no interface copy.
const ANSWER = "根据你的描述，普通感冒的可能性较大。\n\n建议多休息、多喝水。";

test("copy uses the Clipboard API and passes the answer text through verbatim", async () => {
  const written = [];
  const legacyCalls = [];
  const result = await copyAnswerText(ANSWER, {
    clipboard: {
      writeText: async (text) => {
        written.push(text);
      },
    },
    legacyCopy: (text) => {
      legacyCalls.push(text);
      return true;
    },
  });
  assert.deepEqual(result, { ok: true, method: "clipboard" });
  assert.deepEqual(written, [ANSWER]);
  assert.deepEqual(legacyCalls, []);
});

test("clipboard rejection falls back to the legacy copy path", async () => {
  const legacyCalls = [];
  const result = await copyAnswerText(ANSWER, {
    clipboard: {
      writeText: async () => {
        throw new Error("NotAllowedError");
      },
    },
    legacyCopy: (text) => {
      legacyCalls.push(text);
      return true;
    },
  });
  assert.deepEqual(result, { ok: true, method: "legacy" });
  assert.deepEqual(legacyCalls, [ANSWER]);
});

test("missing Clipboard API uses the legacy copy path directly", async () => {
  const result = await copyAnswerText(ANSWER, {
    clipboard: null,
    legacyCopy: () => true,
  });
  assert.deepEqual(result, { ok: true, method: "legacy" });
});

test("clipboard rejection with a failing fallback reports denied", async () => {
  const result = await copyAnswerText(ANSWER, {
    clipboard: {
      writeText: async () => {
        throw new Error("NotAllowedError");
      },
    },
    legacyCopy: () => false,
  });
  assert.deepEqual(result, { ok: false, reason: "denied" });
});

test("no clipboard and no legacy path reports unavailable", async () => {
  const result = await copyAnswerText(ANSWER, {
    clipboard: null,
    legacyCopy: null,
  });
  assert.deepEqual(result, { ok: false, reason: "unavailable" });
});

test("empty answer text is not copied", async () => {
  let calls = 0;
  const result = await copyAnswerText("", {
    clipboard: {
      writeText: async () => {
        calls += 1;
      },
    },
    legacyCopy: () => true,
  });
  assert.deepEqual(result, { ok: false, reason: "unavailable" });
  assert.equal(calls, 0);
});
