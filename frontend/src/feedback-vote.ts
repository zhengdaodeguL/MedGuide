export type FeedbackRating = "up" | "down";

export type FeedbackVote = Record<string, FeedbackRating>;

export type FeedbackAction = "vote" | "revoke";

/**
 * What a click on a feedback button means, given the vote already recorded
 * for that answer (if any). Clicking the other button changes the vote;
 * clicking the same button again revokes it (issue #19). There is no
 * no-op click: every enabled button press is meant to do something.
 */
export function feedbackAction(
  current: FeedbackRating | undefined,
  clicked: FeedbackRating,
): FeedbackAction {
  return current === clicked ? "revoke" : "vote";
}

/** Record a successful vote for one answer, keeping other answers untouched. */
export function applyFeedbackVote(
  current: FeedbackVote,
  messageId: string,
  rating: FeedbackRating,
): FeedbackVote {
  return { ...current, [messageId]: rating };
}

/** Remove the recorded vote for one answer after a successful revoke. */
export function applyFeedbackRevoke(current: FeedbackVote, messageId: string): FeedbackVote {
  if (!(messageId in current)) return current;
  const next = { ...current };
  delete next[messageId];
  return next;
}
