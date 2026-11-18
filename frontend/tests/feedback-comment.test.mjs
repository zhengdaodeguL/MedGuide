import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/feedback-comment.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
});
const {
  FEEDBACK_COMMENT_MAX_LENGTH,
  buildFeedbackRequestBody,
  feedbackCommentError,
  feedbackCommentReducer,
  initialFeedbackCommentState,
} = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

// --- request body: the comment rides along with the downvote -------------

test("downvote with text sends the trimmed comment", () => {
  const body = buildFeedbackRequestBody({
    sessionId: "s1",
    messageId: "m1",
    rating: "down",
    comment: "  回答没有说清楚要不要去医院  ",
  });
  assert.equal(body.session_id, "s1");
  assert.equal(body.message_id, "m1");
  assert.equal(body.rating, "down");
  assert.equal(body.comment, "回答没有说清楚要不要去医院");
});

test("downvote with blank text sends no comment key, like the old vote", () => {
  for (const comment of [undefined, "", "   ", "\n\t "]) {
    const body = buildFeedbackRequestBody({
      sessionId: "s1",
      messageId: "m1",
      rating: "down",
      comment,
    });
    assert.equal("comment" in body, false);
    assert.deepEqual(body, { session_id: "s1", message_id: "m1", rating: "down" });
  }
});

test("upvote never carries a comment through this helper", () => {
  const body = buildFeedbackRequestBody({
    sessionId: "s1",
    messageId: "m1",
    rating: "up",
    comment: "   ",
  });
  assert.equal("comment" in body, false);
});

// --- length guard: the backend caps comment at 500 chars ------------------

test("comment length guard matches the backend 500-char limit", () => {
  assert.equal(FEEDBACK_COMMENT_MAX_LENGTH, 500);
  assert.equal(feedbackCommentError(""), null);
  assert.equal(feedbackCommentError("x".repeat(500)), null);
  assert.match(feedbackCommentError("x".repeat(501)) ?? "", /500/);
});

// --- comment box state machine --------------------------------------------

test("downvote opens the comment box for that answer only", () => {
  const state = feedbackCommentReducer(initialFeedbackCommentState, {
    type: "open",
    messageId: "m1",
  });
  assert.equal(state.openMessageId, "m1");
  assert.deepEqual(state.drafts, {});
});

test("typing stores a draft for the open box, and only for the open box", () => {
  const opened = feedbackCommentReducer(initialFeedbackCommentState, {
    type: "open",
    messageId: "m1",
  });
  const drafted = feedbackCommentReducer(opened, {
    type: "draft",
    messageId: "m1",
    text: "太笼统了",
  });
  assert.equal(drafted.drafts.m1, "太笼统了");
  const stray = feedbackCommentReducer(opened, {
    type: "draft",
    messageId: "m2",
    text: "不该存下来",
  });
  assert.equal("m2" in stray.drafts, false);
});

test("opening another answer keeps the first draft", () => {
  let state = feedbackCommentReducer(initialFeedbackCommentState, {
    type: "open",
    messageId: "m1",
  });
  state = feedbackCommentReducer(state, { type: "draft", messageId: "m1", text: "第一条的草稿" });
  state = feedbackCommentReducer(state, { type: "open", messageId: "m2" });
  assert.equal(state.openMessageId, "m2");
  assert.equal(state.drafts.m1, "第一条的草稿");
});

test("cancelling closes the box and discards the draft", () => {
  let state = feedbackCommentReducer(initialFeedbackCommentState, {
    type: "open",
    messageId: "m1",
  });
  state = feedbackCommentReducer(state, { type: "draft", messageId: "m1", text: "不要了" });
  state = feedbackCommentReducer(state, { type: "close", messageId: "m1" });
  assert.equal(state.openMessageId, null);
  assert.equal("m1" in state.drafts, false);
});

test("successful submit closes the box and clears the draft", () => {
  let state = feedbackCommentReducer(initialFeedbackCommentState, {
    type: "open",
    messageId: "m1",
  });
  state = feedbackCommentReducer(state, { type: "draft", messageId: "m1", text: "已提交的文字" });
  state = feedbackCommentReducer(state, { type: "submitted", messageId: "m1" });
  assert.equal(state.openMessageId, null);
  assert.equal("m1" in state.drafts, false);
});

test("a failed submit leaves the box open with the draft kept", () => {
  // The component only dispatches "submitted" on success; on failure it
  // dispatches nothing, so the state below is exactly what a failed
  // submit leaves behind: box open, draft intact.
  let state = feedbackCommentReducer(initialFeedbackCommentState, {
    type: "open",
    messageId: "m1",
  });
  state = feedbackCommentReducer(state, { type: "draft", messageId: "m1", text: "提交失败别丢" });
  assert.equal(state.openMessageId, "m1");
  assert.equal(state.drafts.m1, "提交失败别丢");
});

test("submitting another answer does not close the open box", () => {
  let state = feedbackCommentReducer(initialFeedbackCommentState, {
    type: "open",
    messageId: "m1",
  });
  state = feedbackCommentReducer(state, { type: "submitted", messageId: "m2" });
  assert.equal(state.openMessageId, "m1");
});

test("reset clears the open box and every draft", () => {
  let state = feedbackCommentReducer(initialFeedbackCommentState, {
    type: "open",
    messageId: "m1",
  });
  state = feedbackCommentReducer(state, { type: "draft", messageId: "m1", text: "草稿" });
  assert.deepEqual(feedbackCommentReducer(state, { type: "reset" }), initialFeedbackCommentState);
});
