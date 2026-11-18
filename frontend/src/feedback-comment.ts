// Downvote comment box: pure helpers and the per-answer comment-form state
// machine. Kept free of React and the DOM so it can be unit-tested with
// node:test. The backend already accepts an optional `comment` on
// POST /api/feedback (max 500 chars); this module only shapes what the UI
// sends. How the comment is stored server-side is issue #21's scope, not
// this module's.

export const FEEDBACK_COMMENT_MAX_LENGTH = 500;

export type FeedbackRating = "up" | "down";

export interface FeedbackCommentState {
  /** Message whose comment box is currently open, if any. */
  openMessageId: string | null;
  /** Unsent drafts, keyed by message id. */
  drafts: Record<string, string>;
}

export const initialFeedbackCommentState: FeedbackCommentState = {
  openMessageId: null,
  drafts: {},
};

export type FeedbackCommentAction =
  | { type: "open"; messageId: string }
  | { type: "close"; messageId: string }
  | { type: "draft"; messageId: string; text: string }
  | { type: "submitted"; messageId: string }
  | { type: "reset" };

export function feedbackCommentReducer(
  state: FeedbackCommentState,
  action: FeedbackCommentAction,
): FeedbackCommentState {
  switch (action.type) {
    case "open":
      // Opening another answer's box keeps the first draft: switching
      // answers must not silently discard what the user already typed.
      return { ...state, openMessageId: action.messageId };
    case "close": {
      if (state.openMessageId !== action.messageId) return state;
      // Cancelling discards this draft; submitting is the only way to keep it.
      const drafts = { ...state.drafts };
      delete drafts[action.messageId];
      return { openMessageId: null, drafts };
    }
    case "draft": {
      if (state.openMessageId !== action.messageId) return state;
      return { ...state, drafts: { ...state.drafts, [action.messageId]: action.text } };
    }
    case "submitted": {
      const drafts = { ...state.drafts };
      delete drafts[action.messageId];
      return {
        openMessageId: state.openMessageId === action.messageId ? null : state.openMessageId,
        drafts,
      };
    }
    case "reset":
      return initialFeedbackCommentState;
  }
}

/** Length guard for the comment box. Returns an error message, or null when OK. */
export function feedbackCommentError(comment: string): string | null {
  if (comment.length > FEEDBACK_COMMENT_MAX_LENGTH) {
    return `反馈文字请控制在 ${FEEDBACK_COMMENT_MAX_LENGTH} 字以内。`;
  }
  return null;
}

export interface FeedbackRequestBody {
  session_id: string;
  message_id: string;
  rating: FeedbackRating;
  comment?: string;
}

/**
 * Shape the POST /api/feedback body. A blank comment is omitted entirely so
 * submitting without text behaves exactly like the old comment-less vote.
 * A non-blank comment is trimmed before sending.
 */
export function buildFeedbackRequestBody(input: {
  sessionId: string;
  messageId: string;
  rating: FeedbackRating;
  comment?: string;
}): FeedbackRequestBody {
  const comment = (input.comment ?? "").trim();
  if (!comment) {
    return { session_id: input.sessionId, message_id: input.messageId, rating: input.rating };
  }
  return { session_id: input.sessionId, message_id: input.messageId, rating: input.rating, comment };
}
