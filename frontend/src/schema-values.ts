import type { Row } from "./api";

export function resolve(field: Row, root: Row, depth = 0): Row {
  if (depth > 16) return {};
  if (field.$ref)
    return resolve(
      ((root.$defs as Row)?.[String(field.$ref).split("/").pop()!] ||
        {}) as Row,
      root,
      depth + 1,
    );
  if (field.anyOf)
    return resolve(
      (field.anyOf as Row[]).find((item) => item.type !== "null") || {},
      root,
      depth + 1,
    );
  return field;
}

export function initialSchemaValue(raw: Row, root: Row, depth = 0): unknown {
  const field = resolve(raw, root);
  if (field.default !== undefined) return structuredClone(field.default);
  if (field.const !== undefined) return field.const;
  if (depth > 16) return null;
  if (field.oneOf)
    return initialSchemaValue((field.oneOf as Row[])[0], root, depth + 1);
  if (field.type === "object") {
    return Object.fromEntries(
      Object.entries((field.properties || {}) as Row)
        .filter(
          ([name, prop]) =>
            ((field.required || []) as string[]).includes(name) ||
            (prop as Row).default !== undefined,
        )
        .map(([name, prop]) => [
          name,
          initialSchemaValue(prop as Row, root, depth + 1),
        ]),
    );
  }
  if (field.type === "array") return [];
  if (field.enum) return (field.enum as unknown[])[0];
  if (field.type === "boolean") return false;
  if (field.type === "number" || field.type === "integer")
    return field.minimum ?? 0;
  return "";
}
