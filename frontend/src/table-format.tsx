import type { Row } from "./api";
import { Badge } from "./components";

export function flatten(row: Row): Row {
  return { ...row, ...((row.fields as Row) || {}) };
}

export function renderBadge(value: unknown) {
  return <Badge value={value} />;
}
