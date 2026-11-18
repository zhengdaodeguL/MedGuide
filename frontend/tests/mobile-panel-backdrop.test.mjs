import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

// Issue #28: while a mobile drawer is open the topbar looked clickable
// but a tap on it did nothing -- the backdrop stopped below the topbar
// (inset:64px / inset:58px) while the topbar itself was inert. The fix
// is to let the backdrop cover the whole viewport, so a tap anywhere
// outside the drawer -- topbar included -- closes the panel. There is
// no DOM in this suite, so these tests assert on the shipped source and
// styles instead of a live click.
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const appSource = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

function backdropRules() {
  const rules = [];
  const pattern = /\.mobile-panel-backdrop\s*\{([^}]*)\}/g;
  let match;
  while ((match = pattern.exec(styles)) !== null) rules.push(match[1]);
  return rules;
}

test("backdrop covers the whole viewport, including the topbar strip", () => {
  const rules = backdropRules();
  assert.ok(rules.length > 0, "expected a .mobile-panel-backdrop rule");
  for (const rule of rules) {
    assert.match(rule, /(^|;)\s*inset:\s*0\s*(;|$)/, `backdrop rule must start at the viewport top: ${rule}`);
    assert.doesNotMatch(rule, /inset:\s*(64|58)px/, `backdrop must not stop below the topbar: ${rule}`);
  }
});

test("backdrop is only shown in the mobile layout", () => {
  assert.match(styles, /\.mobile-panel-backdrop,\s*\.close-mobile\s*\{\s*display:\s*none\s*;?\s*\}/);
  const mediaIndex = styles.indexOf("@media (max-width:1020px)");
  const ruleIndex = styles.indexOf(".mobile-panel-backdrop { display:block");
  assert.ok(mediaIndex >= 0 && ruleIndex > mediaIndex, "backdrop display:block must live inside the <=1020px block");
});

test("drawers stay above the backdrop so they remain usable", () => {
  const backdrop = backdropRules().find((rule) => rule.includes("display:block"));
  assert.match(backdrop, /z-index:\s*4\b/);
  const drawerRule = styles.match(/\.left-rail,\s*\.right-panel\s*\{([^}]*)\}/);
  assert.ok(drawerRule, "expected the shared drawer rule");
  assert.match(drawerRule[1], /z-index:\s*5\b/, "drawers must paint above the backdrop");
});

test("backdrop is rendered while a panel is open and closes it on tap", () => {
  assert.ok(
    appSource.includes('{mobilePanel && <button className="mobile-panel-backdrop" onClick={closeMobilePanel}'),
    "backdrop button must be rendered when mobilePanel is set and close the panel on click",
  );
});

test("topbar stays inert while a panel is open (keyboard protection unchanged)", () => {
  assert.ok(appSource.includes('document.querySelector<HTMLElement>(".topbar")'), "inert effect must target .topbar");
  assert.ok(appSource.includes('element.setAttribute("inert", "")'), "inert effect must set inert while a panel is open");
  assert.ok(appSource.includes('element.removeAttribute("inert")'), "inert must be removed when the panel closes");
});
