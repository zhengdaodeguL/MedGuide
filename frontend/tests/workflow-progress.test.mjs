import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/workflow-progress.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
});
const { buildWorkflowStages, mergePipelineEvent, responsePipelineEvents } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);
const order = ["normalize", "extract_profile", "intent", "risk", "retrieve", "structured_query", "draft", "safety_review", "finalize"];
const event = (node, status = "completed") => ({ node, label: node, status, elapsed_ms: 0 });

test("duplicate SSE events cannot advance an unfinished phase", () => {
  const events = [event("normalize"), event("extract_profile")];
  const merged = mergePipelineEvent(events, event("normalize"));
  assert.equal(merged.length, 2);
  assert.equal(buildWorkflowStages(merged, "unknown", "unknown", true)[0].status, "running");
  assert.equal(buildWorkflowStages(merged, "unknown", "unknown", true)[1].status, "pending");
});

test("SQL and emergency routes show skipped retrieval without an unfinished answer phase", () => {
  for (const risk of ["low", "high"]) {
    const events = order.map((node) => event(node, node === "retrieve" ? "skipped" : "completed"));
    const stages = buildWorkflowStages(events, risk, "structured", false);
    assert.ok(stages.every((stage) => stage.status === "completed"));
    assert.match(stages[2].detail, /跳过普通资料检索/);
    assert.equal(stages[2].label, risk === "high" ? "整理紧急就医指引" : "核对只读查询结果");
  }
});

test("final JSON and recovered replies restore node states from metadata", () => {
  const metadata = { workflow_path: order.filter((node) => node !== "structured_query"), workflow_skipped_nodes: ["structured_query"] };
  const restored = responsePipelineEvents(undefined, metadata);
  assert.equal(restored.length, 9);
  assert.equal(restored.find((item) => item.node === "structured_query").status, "skipped");
  assert.ok(buildWorkflowStages(restored, "low", "disease", false).every((stage) => stage.status === "completed"));
  assert.deepEqual(responsePipelineEvents([event("risk")], metadata), [event("risk")]);
});
