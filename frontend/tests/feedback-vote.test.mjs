import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/feedback-vote.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
});
const { applyFeedbackRevoke, applyFeedbackVote, feedbackAction } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

test("clicking the other button changes the vote", () => {
  assert.equal(feedbackAction("up", "down"), "vote");
  assert.equal(feedbackAction("down", "up"), "vote");
  assert.equal(feedbackAction(undefined, "up"), "vote");
  assert.equal(feedbackAction(undefined, "down"), "vote");
});

test("clicking the same button again revokes the vote", () => {
  assert.equal(feedbackAction("up", "up"), "revoke");
  assert.equal(feedbackAction("down", "down"), "revoke");
});

test("applying a vote keeps other answers untouched", () => {
  const votes = applyFeedbackVote({ "message-1": "up" }, "message-2", "down");
  assert.deepEqual(votes, { "message-1": "up", "message-2": "down" });
  const changed = applyFeedbackVote(votes, "message-2", "up");
  assert.deepEqual(changed, { "message-1": "up", "message-2": "up" });
});

test("revoking removes only that answer's vote", () => {
  const votes = applyFeedbackRevoke({ "message-1": "up", "message-2": "down" }, "message-1");
  assert.deepEqual(votes, { "message-2": "down" });
});

test("revoking a vote that was never recorded leaves state untouched", () => {
  const votes = { "message-1": "up" };
  assert.equal(applyFeedbackRevoke(votes, "message-2"), votes);
});
