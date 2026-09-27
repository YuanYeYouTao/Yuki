import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { Badge, ErrorNote, JsonNote, QueryList, Section } from "./components";
import { PluginDetails } from "./plugin-details";
const status = (value: unknown) => <Badge value={value} />;
export function Tools(props: PageProps) {
  const { allowed, act, refresh } = props;
  const [plugin, setPlugin] = useState("");
  const runtime = useQuery<Row>(
    "read_plugin_runtime",
    { plugin_id: plugin },
    refresh,
    !!plugin && allowed("read_plugin_runtime"),
  );
  function mutation(
    method: string,
    row: Row,
    resource: string,
    action: string,
    label: string,
  ) {
    act({
      method,
      label,
      revision: Number(row.revision),
      payload: { resource_id: String(row[resource]), action },
    });
  }
  return (
    <>
      <Section title="插件">
        <QueryList
          method="list_plugins"
          refresh={refresh}
          columns={[
            ["name", "名称"],
            ["version", "版本"],
            ["status", "状态", status],
            ["enabled", "启用", status],
          ]}
          actions={(row) => (
            <>
              <button
                className="btn-secondary"
                onClick={() => setPlugin(String(row.plugin_id))}
              >
                运行详情
              </button>
              {[
                ["enable", "启用"],
                ["disable", "停用"],
                ["doctor", "诊断"],
              ].map(([action, label]) => (
                <button
                  key={action}
                  className="btn-secondary"
                  disabled={!allowed("mutate_plugin")}
                  onClick={() =>
                    mutation("mutate_plugin", row, "plugin_id", action, label)
                  }
                >
                  {label}
                </button>
              ))}
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_plugin")}
                onClick={() =>
                  act({
                    method: "mutate_plugin",
                    label: "审核插件授权",
                    revision: Number(row.revision),
                    payload: {
                      resource_id: row.plugin_id,
                      action: "approve",
                      spec: {},
                    },
                    edit: "spec",
                    hint: "逐项检查 manifest hash 和所需权限后提交。",
                  })
                }
              >
                授权
              </button>
            </>
          )}
        />
        {runtime.error != null && <ErrorNote error={runtime.error} />}
        {runtime.data && (
          <JsonNote title={`${plugin} · 实际运行状态`} value={runtime.data} />
        )}
      </Section>
      {plugin && <PluginDetails key={plugin} pluginId={plugin} props={props} />}
      <Section title="MCP">
        <QueryList
          method="list_mcp_servers"
          refresh={refresh}
          columns={[
            ["server_id", "服务"],
            ["enabled", "启用", status],
            ["healthy", "健康", status],
            ["tool_count", "工具"],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <>
              {[
                ["enable", "启用"],
                ["disable", "停用"],
                ["refresh", "刷新工具"],
                ["reconnect", "重连"],
              ].map(([action, label]) => (
                <button
                  key={action}
                  className="btn-secondary"
                  disabled={!allowed("mutate_mcp")}
                  onClick={() =>
                    mutation("mutate_mcp", row, "server_id", action, label)
                  }
                >
                  {label}
                </button>
              ))}
            </>
          )}
        />
      </Section>
    </>
  );
}
