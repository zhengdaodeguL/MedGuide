/**
 * Copy an assistant answer to the clipboard (issue #24).
 *
 * The answer body is plain text: the message bubble renders `content`
 * split on newlines, while citations, latency and the "view references"
 * affordance live in separate UI. Copying `content` verbatim therefore
 * copies the answer text only — no citation markers or interface copy.
 *
 * Strategy, in order:
 * 1. `navigator.clipboard.writeText` (async Clipboard API).
 * 2. Legacy `document.execCommand("copy")` via a temporary textarea,
 *    for older browsers or when the Clipboard API is unavailable.
 *
 * The environment is injectable so the behaviour can be unit-tested
 * without a browser. Defaults are resolved lazily from the globals so
 * importing this module in Node (no `navigator`/`document`) is safe.
 */

export type ClipboardWriter = {
  writeText: (text: string) => Promise<void>;
};

export type CopyEnvironment = {
  /** Defaults to `navigator.clipboard` when present. */
  clipboard?: ClipboardWriter | null;
  /**
   * Legacy fallback. Defaults to a temporary-textarea `execCommand`
   * implementation when `document` exists, otherwise absent.
   */
  legacyCopy?: ((text: string) => boolean) | null;
};

export type CopyResult =
  | { ok: true; method: "clipboard" | "legacy" }
  | { ok: false; reason: "unavailable" | "denied" };

function legacyCopyWithTextarea(text: string): boolean {
  if (typeof document === "undefined") return false;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  document.body.removeChild(textarea);
  return copied;
}

function resolveClipboard(env: CopyEnvironment): ClipboardWriter | null {
  if (env.clipboard !== undefined) return env.clipboard;
  if (typeof navigator === "undefined") return null;
  const clipboard = (navigator as Navigator).clipboard;
  return clipboard && typeof clipboard.writeText === "function" ? clipboard : null;
}

function resolveLegacyCopy(env: CopyEnvironment): ((text: string) => boolean) | null {
  if (env.legacyCopy !== undefined) return env.legacyCopy;
  return typeof document === "undefined" ? null : legacyCopyWithTextarea;
}

export async function copyAnswerText(
  text: string,
  env: CopyEnvironment = {},
): Promise<CopyResult> {
  if (!text) return { ok: false, reason: "unavailable" };
  const clipboard = resolveClipboard(env);
  if (clipboard) {
    try {
      await clipboard.writeText(text);
      return { ok: true, method: "clipboard" };
    } catch {
      // Fall through to the legacy path (e.g. permission denied).
    }
  }
  const legacyCopy = resolveLegacyCopy(env);
  if (legacyCopy) {
    try {
      if (legacyCopy(text)) return { ok: true, method: "legacy" };
    } catch {
      // Fall through to the failure result below.
    }
    return { ok: false, reason: "denied" };
  }
  return { ok: false, reason: "unavailable" };
}
