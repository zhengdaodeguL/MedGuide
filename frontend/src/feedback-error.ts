// Feedback-scoped error text for POST /api/feedback.
//
// The chat flows use formatHttpError (App.tsx), whose 400/422 wording is
// about the question input ("请确认描述不为空且不超过 2000 字"). Reusing it
// for a failed vote made the toast blame the user's question, which has
// nothing to do with feedback (issue #25). This formatter is only for
// feedback failures, so it never mentions the question input.
//
// Layering:
// - 4xx with a server detail: show the detail. The feedback endpoint's
//   own details are user-facing Chinese written by the backend
//   ("反馈目标不属于当前会话", "会话不存在或已过期"), so they say what
//   actually went wrong. FastAPI body-validation details are joined
//   msg strings (ensureResponseOk already flattens the array).
// - 4xx without a detail: a feedback-scoped generic message per status.
// - 5xx: never show the detail — those are raw backend exception text
//   (e.g. SessionBackendError) and can leak internals; use a generic
//   "service unavailable" message instead.
export interface FeedbackHttpError {
  status: number;
  detail?: string;
}

const MAX_DETAIL_LENGTH = 200;

function presentDetail(error: FeedbackHttpError): string | undefined {
  const detail = (error.detail ?? "").trim();
  if (!detail) return undefined;
  return detail.length > MAX_DETAIL_LENGTH
    ? `${detail.slice(0, MAX_DETAIL_LENGTH)}…`
    : detail;
}

function withDetail(prefix: string, detail: string | undefined, fallback: string): string {
  return detail ? `${prefix}：${detail}` : fallback;
}

export function formatFeedbackError(error: FeedbackHttpError): string {
  const detail = presentDetail(error);
  switch (error.status) {
    case 400:
    case 422:
      return withDetail("反馈提交失败", detail, "反馈提交失败：反馈内容未通过校验，请重试。");
    case 401:
    case 403:
      // sendFeedback returns to the login screen on 401 before this is
      // reached; this is only a defensive fallback.
      return "反馈提交失败：当前登录状态无效，请重新登录后重试。";
    case 404:
      return withDetail("反馈提交失败", detail, "反馈提交失败：当前会话已过期，请新建整理后重试。");
    case 409:
      return "反馈提交失败：反馈刚刚被其他请求更新，请稍后重试。";
    case 426:
      return withDetail(
        "反馈提交失败",
        detail,
        "反馈提交失败：当前入口未启用 HTTPS，请通过受保护的服务地址继续。",
      );
    case 429:
      return "反馈提交失败：提交过于频繁，请稍后再试。";
    default:
      if (error.status >= 500) {
        return "反馈提交失败：整理服务暂时不可用，请稍后重试。";
      }
      return withDetail(
        "反馈提交失败",
        detail,
        `反馈提交失败：整理服务返回错误（HTTP ${error.status}），请稍后重试。`,
      );
  }
}
