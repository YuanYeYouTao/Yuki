import type { CommandMethod } from "./control-methods";
import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { Badge, ErrorNote, JsonNote, QueryList, Section } from "./components";
import { PluginDetails } from "./plugin-details";
import { PluginApproval } from "./plugin-approval";
const status = (value: unknown) => <Badge value={value} />;
export function Tools(props: PageProps) {
  const { allowed, act, refresh } = props;
  const [plugin, setPlugin] = useState(""),
    [approval, setApproval] = useState("");
  const runtime = useQuery<Row>(
    "read_plugin_runtime",
    { plugin_id: plugin },
    refresh,
    !!plugin && allowed("read_plugin_runtime"),
  );
  function mutation(
    method: CommandMethod,
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
                disabled={!allowed("read_plugin_approval")}
                onClick={() => setApproval(String(row.plugin_id))}
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
      {approval && (
        <PluginApproval key={approval} pluginId={approval} props={props} />
      )}
      {plugin && <PluginDetails key={plugin} pluginId={plugin} props={props} />}
    </>
  );
}
