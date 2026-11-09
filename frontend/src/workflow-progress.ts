export type PipelineEvent = {
  node: string;
  label: string;
  elapsed_ms: number;
  status?: "completed" | "skipped";
  reason?: string;
};

type WorkflowMetadata = {
  workflow_path?: string[];
  workflow_skipped_nodes?: string[];
};

const STAGES = [
  { id: "profile", nodes: ["normalize", "extract_profile", "intent"], label: "整理关键信息" },
  { id: "risk", nodes: ["risk"], label: "筛查就医信号" },
  { id: "answer", nodes: ["retrieve", "structured_query", "draft"], label: "核对资料并整理回答" },
  { id: "review", nodes: ["safety_review", "finalize"], label: "审查回答与引用" },
] as const;

export function mergePipelineEvent(events: PipelineEvent[], event: PipelineEvent): PipelineEvent[] {
  return [...events.filter((item) => item.node !== event.node), event];
}

/** Final replies are authoritative, including JSON replies and recovered commits. */
export function responsePipelineEvents(events: PipelineEvent[] | undefined, metadata: WorkflowMetadata): PipelineEvent[] {
  if (events?.length) return events.reduce(mergePipelineEvent, [] as PipelineEvent[]);
  const completed = (metadata.workflow_path || []).map((node): PipelineEvent => ({
    node, label: node, elapsed_ms: 0, status: "completed",
  }));
  const skipped = (metadata.workflow_skipped_nodes || []).map((node): PipelineEvent => ({
    node, label: node, elapsed_ms: 0, status: "skipped",
  }));
  return [...completed, ...skipped];
}

export function buildWorkflowStages(
  events: PipelineEvent[],
  risk: string,
  intent: string | undefined,
  loading: boolean,
) {
  const byNode = new Map(events.map((event) => [event.node, event]));
  let activeAssigned = false;
  return STAGES.map((stage) => {
    const terminal = stage.nodes.every((node) => byNode.has(node));
    const skipped = terminal && stage.nodes.every((node) => byNode.get(node)?.status === "skipped");
    const status = terminal ? (skipped ? "skipped" : "completed")
      : loading && !activeAssigned ? "running" : "pending";
    if (!terminal) activeAssigned = true;
    const label = stage.id === "answer" && risk === "high" ? "整理紧急就医指引"
      : stage.id === "answer" && intent === "structured" ? "核对只读查询结果" : stage.label;
    const skippedRetrieval = byNode.get("retrieve")?.status === "skipped";
    const detail = stage.id === "answer" && skippedRetrieval
      ? risk === "high" ? "高风险分流，跳过普通资料检索" : "结构化分支，跳过普通资料检索"
      : undefined;
    return { id: stage.id, label, status, detail };
  });
}
