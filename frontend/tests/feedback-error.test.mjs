import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/feedback-error.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
});
const { formatFeedbackError } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

// The complaint in issue #25: a failed vote showed the chat-input wording
// ("请确认描述不为空且不超过 2000 字"). No feedback error may ever say
// that, whatever the status or detail.
const chatInputPhrases = ["问题描述", "2000"];

for (const [status, detail] of [
  [400, "反馈目标不属于当前会话"],
  [422, "反馈目标不属于当前会话"],
  [422, undefined],
  [404, "会话不存在或已过期"],
  [404, undefined],
  [409, undefined],
  [426, "请通过 HTTPS 受保护入口访问"],
  [429, undefined],
  [500, "redis connection refused"],
  [503, undefined],
  [418, "teapot"],
  [418, undefined],
]) {
  test(`feedback error ${status} ${detail ? "with detail" : "without detail"} never blames the question input`, () => {
    const message = formatFeedbackError({ status, detail });
    for (const phrase of chatInputPhrases) {
      assert.ok(!message.includes(phrase), `${message} must not contain ${phrase}`);
    }
    assert.ok(message.startsWith("反馈提交失败"), message);
  });
}

test("422 with a server detail shows the detail", () => {
  assert.equal(
    formatFeedbackError({ status: 422, detail: "反馈目标不属于当前会话" }),
    "反馈提交失败：反馈目标不属于当前会话",
  );
});

test("422 with a FastAPI validation detail shows the joined messages", () => {
  assert.equal(
    formatFeedbackError({ status: 422, detail: "Field required；String should have at most 500 characters" }),
    "反馈提交失败：Field required；String should have at most 500 characters",
  );
});

test("422 without a detail falls back to a feedback-scoped validation message", () => {
  assert.equal(
    formatFeedbackError({ status: 422 }),
    "反馈提交失败：反馈内容未通过校验，请重试。",
  );
});

test("404 shows the session detail, or a feedback-scoped expiry message", () => {
  assert.equal(
    formatFeedbackError({ status: 404, detail: "会话不存在或已过期" }),
    "反馈提交失败：会话不存在或已过期",
  );
  assert.equal(
    formatFeedbackError({ status: 404 }),
    "反馈提交失败：当前会话已过期，请新建整理后重试。",
  );
});

test("5xx never surfaces the raw backend detail", () => {
  assert.equal(
    formatFeedbackError({ status: 500, detail: "redis connection refused" }),
    "反馈提交失败：整理服务暂时不可用，请稍后重试。",
  );
  assert.equal(
    formatFeedbackError({ status: 503, detail: "SessionBackendError: dial tcp 10.0.0.4:6379" }),
    "反馈提交失败：整理服务暂时不可用，请稍后重试。",
  );
});

test("429 and 426 get feedback-scoped messages", () => {
  assert.equal(
    formatFeedbackError({ status: 429 }),
    "反馈提交失败：提交过于频繁，请稍后再试。",
  );
  assert.equal(
    formatFeedbackError({ status: 426, detail: "请通过 HTTPS 受保护入口访问" }),
    "反馈提交失败：请通过 HTTPS 受保护入口访问",
  );
});

test("401 falls back to a login message (the caller returns to login first)", () => {
  assert.equal(
    formatFeedbackError({ status: 401 }),
    "反馈提交失败：当前登录状态无效，请重新登录后重试。",
  );
});

test("blank detail is treated as no detail", () => {
  assert.equal(
    formatFeedbackError({ status: 422, detail: "   " }),
    "反馈提交失败：反馈内容未通过校验，请重试。",
  );
});

test("overlong detail is truncated", () => {
  const message = formatFeedbackError({ status: 422, detail: "字".repeat(300) });
  assert.equal(message, `反馈提交失败：${"字".repeat(200)}…`);
});

test("unknown status with detail shows the detail, without it names the status", () => {
  assert.equal(
    formatFeedbackError({ status: 418, detail: "teapot" }),
    "反馈提交失败：teapot",
  );
  assert.equal(
    formatFeedbackError({ status: 418 }),
    "反馈提交失败：整理服务返回错误（HTTP 418），请稍后重试。",
  );
});
