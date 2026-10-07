import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { Badge, JsonNote, QueryList, Section } from "./components";
import { OwnerPicker } from "./owner-picker";

function BindingForm({
  owner,
  kind,
  props,
}: {
  owner?: Row;
  kind: "person" | "space" | "presence";
  props: PageProps;
}) {
  const [platform, setPlatform] = useState("qq"),
    [external, setExternal] = useState(""),
    [display, setDisplay] = useState("");
  const method =
    kind === "person"
      ? "attach_identity_binding"
      : kind === "space"
        ? "attach_space_binding"
        : "register_presence";
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        props.act({
          method,
          label: kind === "presence" ? "注册连接入口" : "绑定已有内部主体",
          revision: Number(owner?.revision || 0),
          target: owner ? { kind, id: owner[`${kind}_id`] } : { kind: "yuki" },
          payload: {
            platform,
            [kind === "space" ? "external_space_id" : "external_account_id"]:
              external,
            ...(kind !== "presence" ? { display_name: display } : {}),
          },
          review: { owner, platform, external, display },
          hint: "外部账号仅在接入边界绑定，不迁移消息、合并已拥有业务资料的主体或重建内部编号。",
        });
      }}
    >
      <label className="form-group">
        平台
        <input
          className="form-control"
          required
          value={platform}
          onChange={(e) => setPlatform(e.target.value)}
        />
      </label>
      <label className="form-group">
        外部{kind === "space" ? "空间" : "账号"}编号
        <input
          className="form-control"
          required
          value={external}
          onChange={(e) => setExternal(e.target.value)}
        />
      </label>
      {kind !== "presence" && (
        <label className="form-group">
          显示名称
          <input
            className="form-control"
            value={display}
            onChange={(e) => setDisplay(e.target.value)}
          />
        </label>
      )}
      <button className="btn-primary" disabled={!props.allowed(method)}>
        检查并{kind === "presence" ? "注册" : "绑定"}
      </button>
    </form>
  );
}

function RouteForm({ route, props }: { route?: Row; props: PageProps }) {
  const [kind, setKind] = useState(String(route?.kind || "person_active")),
    [owner, setOwner] = useState(
      String(
        route?.person_id || route?.space_id || route?.space_binding_id || "",
      ),
    ),
    [binding, setBinding] = useState(
      String(route?.identity_binding_id || route?.space_binding_id || ""),
    ),
    [presence, setPresence] = useState(
      String(route?.presence_id || route?.ingest_presence_id || ""),
    ),
    [paused, setPaused] = useState(Boolean(route?.paused));
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        props.act({
          method: "set_route",
          label: route ? "更新原路由" : "创建原路由",
          revision: Number(route?.revision || 0),
          target: {
            kind:
              kind === "person_active"
                ? "person"
                : kind === "space_active"
                  ? "space"
                  : "space_binding",
            id: owner,
          },
          payload: {
            kind,
            paused,
            ...(kind === "space_binding_ingest"
              ? { ingest_presence_id: presence }
              : {
                  presence_id: presence,
                  [kind === "person_active"
                    ? "identity_binding_id"
                    : "space_binding_id"]: binding,
                }),
          },
          review: { kind, owner, binding, presence, paused },
          hint: "所有路由引用使用原 canonical UUID。新建按版本 0 处理；已有路由请从列表选择，不覆盖未读取的版本。",
        });
      }}
    >
      <label className="form-group">
        路由类型
        <select
          className="form-control"
          disabled={!!route}
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          {[
            ["person_active", "人物的主动发送"],
            ["space_active", "群的主动发送"],
            ["space_binding_ingest", "群消息接入"],
          ].map(([value, title]) => (
            <option value={value} key={value}>
              {title}
            </option>
          ))}
        </select>
      </label>
      <label className="form-group">
        {kind === "person_active"
          ? "人物"
          : kind === "space_active"
            ? "群"
            : "群接入绑定"}
        <OwnerPicker
          kind={
            kind === "person_active"
              ? "person"
              : kind === "space_active"
                ? "space"
                : "space_binding"
          }
          label={
            kind === "person_active"
              ? "人物"
              : kind === "space_active"
                ? "群"
                : "群接入绑定"
          }
          required
          disabled={!!route}
          value={owner}
          change={setOwner}
          refresh={props.refresh}
          empty="选择主体"
        />
      </label>
      {kind !== "space_binding_ingest" && (
        <label className="form-group">
          {kind === "person_active" ? "人物接入绑定" : "群接入绑定"}
          <OwnerPicker
            kind={kind === "person_active" ? "binding" : "space_binding"}
            label={kind === "person_active" ? "人物接入绑定" : "群接入绑定"}
            required
            value={binding}
            change={setBinding}
            refresh={props.refresh}
            empty="选择绑定"
          />
        </label>
      )}
      <label className="form-group">
        Yuki 的平台入口
        <OwnerPicker
          kind="presence"
          label="Yuki 的平台入口"
          required
          value={presence}
          change={setPresence}
          refresh={props.refresh}
          empty="选择入口"
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={paused}
          onChange={(e) => setPaused(e.target.checked)}
        />
        暂停路由
      </label>
      <button className="btn-primary" disabled={!props.allowed("set_route")}>
        检查并保存路由
      </button>
    </form>
  );
}

export function Identity(props: PageProps) {
  const [selected, setSelected] = useState<{
      kind: "person" | "space";
      row: Row;
    } | null>(null),
    [route, setRoute] = useState<Row | null>(null),
    [createRoute, setCreateRoute] = useState(false),
    [register, setRegister] = useState(false);
  const badge = (value: unknown) => <Badge value={value} />;
  return (
    <>
      {(["person", "space"] as const).map((kind) => (
        <Section key={kind} title={kind === "person" ? "用户身份" : "群与空间"}>
          <QueryList
            method={kind === "person" ? "list_persons" : "list_spaces"}
            refresh={props.refresh}
            columns={[
              [`${kind}_id`, kind === "person" ? "人物" : "群"],
              ["enabled", "启用", badge],
              ["binding_count", "绑定"],
              ["revision", "版本"],
            ]}
            actions={(row) => (
              <>
                <button
                  className="btn-secondary"
                  onClick={() => setSelected({ kind, row })}
                >
                  详情与绑定
                </button>
                <button
                  className="btn-secondary"
                  disabled={
                    !row[`${kind}_id`] ||
                    !props.allowed(
                      `${row.enabled ? "disable" : "enable"}_${kind}`,
                    )
                  }
                  onClick={() =>
                    props.act({
                      method: `${row.enabled ? "disable" : "enable"}_${kind}`,
                      label: row.enabled ? "停用内部主体" : "启用内部主体",
                      revision: Number(row.revision),
                      payload: {},
                      target: { kind, id: row[`${kind}_id`] },
                      review: row,
                    })
                  }
                >
                  {row.enabled ? "停用" : "启用"}
                </button>
              </>
            )}
          />
        </Section>
      ))}
      {selected && (
        <Section title={`${selected.kind} · 已读取的内部主体`}>
          <JsonNote title="原主体详情" value={selected.row} />
          <BindingForm
            key={`${selected.kind}:${selected.row[`${selected.kind}_id`]}:${selected.row.revision}`}
            owner={selected.row}
            kind={selected.kind}
            props={props}
          />
        </Section>
      )}
      {(["person", "space"] as const).map((kind) => (
        <Section
          key={kind}
          title={kind === "person" ? "用户接入绑定" : "空间接入绑定"}
        >
          {props.allowed(
            kind === "person"
              ? "list_identity_bindings"
              : "list_space_bindings",
          ) && (
            <QueryList
              method={
                kind === "person"
                  ? "list_identity_bindings"
                  : "list_space_bindings"
              }
              refresh={props.refresh}
              columns={[
                ["binding_id", "接入绑定"],
                [`${kind}_id`, kind === "person" ? "人物" : "群"],
                ["platform", "平台"],
                ["display_name", "名称"],
                ["external", "平台账号"],
                ["status", "状态", badge],
                ["resolution", "身份状态"],
                ["revision", "版本"],
              ]}
            />
          )}
        </Section>
      ))}
      <Section title="连接入口">
        <button
          className="btn-secondary"
          disabled={!props.allowed("register_presence")}
          onClick={() => setRegister(!register)}
        >
          注册入口
        </button>
        {register && <BindingForm kind="presence" props={props} />}
        <QueryList
          method="list_presences"
          refresh={props.refresh}
          columns={[
            ["presence_id", "Yuki 的平台入口"],
            ["platform", "平台"],
            ["connection_state", "连接", badge],
            ["enabled", "启用", badge],
            ["ingest_eligible", "接入", badge],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <>
              {(["start_presence", "stop_presence"] as const).map((method) => (
                <button
                  key={method}
                  className="btn-secondary"
                  disabled={!props.allowed(method)}
                  onClick={() =>
                    props.act({
                      method,
                      label:
                        method === "start_presence" ? "启动入口" : "停止入口",
                      revision: Number(row.revision),
                      payload: {},
                      target: { kind: "presence", id: row.presence_id },
                      review: row,
                    })
                  }
                >
                  {method === "start_presence" ? "启动" : "停止"}
                </button>
              ))}
              <button
                className="btn-secondary"
                disabled={!props.allowed("set_presence_ingest")}
                onClick={() =>
                  props.act({
                    method: "set_presence_ingest",
                    label: row.ingest_eligible ? "停止接入" : "开启接入",
                    revision: Number(row.revision),
                    payload: { ingest_eligible: !row.ingest_eligible },
                    target: { kind: "presence", id: row.presence_id },
                    review: row,
                  })
                }
              >
                切换接入
              </button>
            </>
          )}
        />
      </Section>
      <Section title="投递与接入路由">
        <button
          className="btn-secondary"
          disabled={!props.allowed("set_route")}
          onClick={() => {
            setCreateRoute(!createRoute);
            setRoute(null);
          }}
        >
          新建路由
        </button>
        {createRoute && <RouteForm props={props} />}
        {route && (
          <>
            <JsonNote title="原路由版本与引用状态" value={route} />
            <RouteForm
              key={`${route.kind}:${route.person_id || route.space_id || route.space_binding_id}:${route.revision}`}
              route={route}
              props={props}
            />
          </>
        )}
        {(
          ["person_active", "space_active", "space_binding_ingest"] as const
        ).map((kind) => {
          const method = `list_${kind}_routes` as const;
          return (
            props.allowed(method) && (
              <div key={kind}>
                <h3>
                  {kind === "person_active"
                    ? "人物主动发送"
                    : kind === "space_active"
                      ? "群主动发送"
                      : "群接入"}
                </h3>
                <QueryList
                  method={method}
                  refresh={props.refresh}
                  columns={[
                    [
                      kind === "person_active"
                        ? "person_id"
                        : kind === "space_active"
                          ? "space_id"
                          : "space_binding_id",
                      "原主体",
                    ],
                    [
                      kind === "person_active"
                        ? "identity_binding_id"
                        : "space_binding_id",
                      "Binding",
                    ],
                    [
                      kind === "space_binding_ingest"
                        ? "ingest_presence_id"
                        : "presence_id",
                      "Presence",
                    ],
                    ["paused", "暂停", badge],
                    ["route_generation", "代数"],
                    ["reference_state", "引用状态"],
                    ["revision", "版本"],
                  ]}
                  actions={(row) => (
                    <>
                      <button
                        className="btn-secondary"
                        onClick={() => {
                          setRoute(row);
                          setCreateRoute(false);
                        }}
                      >
                        编辑原路由
                      </button>
                      <button
                        className="btn-secondary"
                        disabled={
                          !props.allowed(
                            row.paused ? "resume_route" : "pause_route",
                          )
                        }
                        onClick={() =>
                          props.act({
                            method: row.paused ? "resume_route" : "pause_route",
                            label: row.paused ? "恢复原路由" : "暂停原路由",
                            revision: Number(row.revision),
                            payload: { kind },
                            target: {
                              kind:
                                kind === "person_active"
                                  ? "person"
                                  : kind === "space_active"
                                    ? "space"
                                    : "space_binding",
                              id: row[
                                kind === "person_active"
                                  ? "person_id"
                                  : kind === "space_active"
                                    ? "space_id"
                                    : "space_binding_id"
                              ],
                            },
                            review: row,
                          })
                        }
                      >
                        {row.paused ? "恢复" : "暂停"}
                      </button>
                    </>
                  )}
                />
              </div>
            )
          );
        })}
      </Section>
    </>
  );
}
