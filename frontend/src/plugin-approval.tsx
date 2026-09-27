import { useState } from "react";
import type { Row } from "./api";
import { useQuery } from "./hooks";
import type { PageProps } from "./pages";
import { Empty, ErrorNote, Section } from "./components";

function Permissions({
  pluginId,
  fields,
  props,
}: {
  pluginId: string;
  fields: Row;
  props: PageProps;
}) {
  const [selected, setSelected] = useState(
    (fields.approved_permissions || []) as string[],
  );
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        props.act({
          method: "mutate_plugin",
          label: "批准插件声明的权限",
          revision: Number(fields.revision),
          payload: {
            resource_id: pluginId,
            action: "approve",
            spec: { permissions: selected },
          },
          review: {
            plugin_id: pluginId,
            manifest_hash: fields.manifest_hash,
            permissions: selected,
          },
          hint: "Manager 将再次核验声明摘要与原安装版本；修改运行中权限会沿原生命周期停用插件。",
        });
      }}
    >
      <p className="small">
        Manifest SHA256：{String(fields.manifest_hash)} · 版本{" "}
        {String(fields.revision)}
      </p>
      <fieldset className="config-checkboxes">
        <legend>逐项授权</legend>
        {((fields.requested_permissions || []) as string[]).map(
          (permission) => (
            <label key={permission}>
              <input
                type="checkbox"
                checked={selected.includes(permission)}
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...selected, permission]
                      : selected.filter((item) => item !== permission),
                  )
                }
              />
              {permission}
            </label>
          ),
        )}
      </fieldset>
      <button
        className="btn-primary"
        disabled={
          !props.allowed("mutate_plugin") ||
          !fields.manifest_available ||
          !fields.manifest_hash_matches
        }
      >
        检查并批准
      </button>
    </form>
  );
}

export function PluginApproval({
  pluginId,
  props,
}: {
  pluginId: string;
  props: PageProps;
}) {
  const result = useQuery<Row>(
    "read_plugin_approval",
    { plugin_id: pluginId },
    props.refresh,
    props.allowed("read_plugin_approval"),
  );
  const fields = result.data?.fields as Row | undefined;
  return (
    <Section title={`${pluginId} · 原声明与授权`}>
      {result.error != null && <ErrorNote error={result.error} />}
      {result.loading && <Empty>正在读取原插件声明…</Empty>}
      {fields && (
        <Permissions
          key={String(fields.revision)}
          pluginId={pluginId}
          fields={fields}
          props={props}
        />
      )}
    </Section>
  );
}
