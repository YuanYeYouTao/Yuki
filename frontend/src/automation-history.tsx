import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { Badge, QueryList } from "./components";
import { stamp } from "./format";

const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const status = (value: unknown) => <Badge value={value} />;

export function AutomationHistory({
  automationId,
  allowed,
  refresh,
}: {
  automationId: number;
  allowed: PageProps["allowed"];
  refresh: number;
}) {
  const [runId, selectRun] = useState<number | null>(null);
  return (
    <>
      {allowed("list_automation_runs") && (
        <section aria-label="自动化执行历史">
          <h3>执行历史</h3>
          <QueryList
            method="list_automation_runs"
            args={{ automation_id: automationId }}
            refresh={refresh}
            onRow={flatten}
            columns={[
              ["id", "原执行 ID"],
              ["status", "状态", status],
              ["scheduled_for", "计划时间", stamp],
              ["started_at", "开始", stamp],
              ["finished_at", "结束", stamp],
              ["steps_completed", "完成步骤"],
              ["model_calls", "模型调用"],
              ["tool_calls", "工具调用"],
              ["sent_messages", "消息"],
              ["error_category", "原因"],
            ]}
            actions={
              allowed("list_automation_steps")
                ? (row) => (
                    <button
                      className="btn-secondary"
                      onClick={() => selectRun(Number(row.id))}
                    >
                      查看本次步骤
                    </button>
                  )
                : undefined
            }
          />
        </section>
      )}
      {allowed("list_automation_steps") && (
        <section aria-label="自动化执行步骤">
          <h3>{runId == null ? "全部执行步骤" : `执行 #${runId} 的步骤`}</h3>
          {runId != null && (
            <button className="btn-secondary" onClick={() => selectRun(null)}>
              查看全部执行
            </button>
          )}
          <QueryList
            method="list_automation_steps"
            args={{ automation_id: automationId, run_id: runId }}
            refresh={refresh}
            onRow={flatten}
            columns={[
              ["id", "内部步骤记录"],
              ["run_id", "执行 ID"],
              ["step_id", "脚本步骤"],
              ["capability", "能力"],
              ["status", "状态", status],
              ["started_at", "开始", stamp],
              ["finished_at", "结束", stamp],
              ["error_category", "原因"],
            ]}
          />
        </section>
      )}
    </>
  );
}
