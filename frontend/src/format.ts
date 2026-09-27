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
