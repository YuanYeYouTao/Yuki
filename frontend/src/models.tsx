import { useQuery } from "./hooks";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { ConfigFile } from "./config-files";
import {
  Badge,
  Empty,
  ErrorNote,
  QueryList,
  Section,
  Table,
} from "./components";
import { stamp } from "./format";
const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const status = (value: unknown) => <Badge value={value} />;
export function Models(props: PageProps) {
  const { allowed, refresh } = props;
  const catalog = useQuery<Row>(
    "read_model_catalog",
    {},
    refresh,
    allowed("read_model_catalog"),
  );
  const fields = catalog.data?.fields as Row | undefined;
  return (
    <>
      <Section title="已加载的模型与路由">
        {catalog.error != null && <ErrorNote error={catalog.error} />}
        {fields ? (
          <>
            <Table
              rows={fields.profiles as Row[]}
              columns={[
                ["id", "Profile"],
                ["provider", "Provider"],
                ["protocol", "协议"],
                ["model", "模型"],
                ["max_output_tokens", "输出预算"],
                ["timeout_seconds", "超时（秒）"],
              ]}
            />
            <h3>任务路由</h3>
            <Table
              rows={fields.routes as Row[]}
              columns={[
                ["task", "任务"],
                ["profile_id", "使用 Profile"],
              ]}
            />
            <p className="small">
              Profile 文件变更于重启生效。凭据与请求头不显示。
            </p>
          </>
        ) : (
          <Empty>
            {allowed("read_model_catalog")
              ? "正在读取…"
              : "未授予配置读取权限。"}
          </Empty>
        )}
      </Section>
      <ConfigFile fileId="model_profiles" props={props} />
      <Section title="实际调用与用量">
        <QueryList
          method="list_model_usage"
          refresh={refresh}
          onRow={flatten}
          columns={[
            ["created_at", "时间", stamp],
            ["task", "任务"],
            ["profile_id", "Profile"],
            ["model", "模型"],
            ["success", "成功", status],
            ["prompt_tokens", "输入"],
            ["cached_prompt_tokens", "缓存命中"],
            ["completion_tokens", "输出"],
            ["latency_seconds", "耗时（秒）"],
            ["error_category", "问题"],
          ]}
          actions={(row) =>
            row.turn_id ? (
              <a
                href={`#audit?turn=${encodeURIComponent(String(row.turn_id))}`}
                className="file-open"
              >
                查看轮次
              </a>
            ) : (
              <span className="small">未绑定轮次</span>
            )
          }
        />
      </Section>
    </>
  );
}
