export function stamp(value: unknown) {
  if (!value) return "未记录";
  const date = new Date(
    typeof value === "number" ? value * 1000 : String(value),
  );
  return Number.isNaN(date.valueOf())
    ? String(value)
    : date.toLocaleString("zh-CN", { hour12: false });
}
export function text(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export const originName: Record<string, string> = {
  user_message: "聊天消息",
  autonomous_group: "自主参与",
  self_initiative: "自主唤醒",
  scheduled_automation: "定时工作",
  plugin_session: "插件会话",
  plugin_background: "插件后台工作",
  system_task: "系统工作",
};
