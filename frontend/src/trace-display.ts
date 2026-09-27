import type { Row } from "./api";

const object = (value: unknown): Row =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Row)
    : {};
const rows = (value: unknown): Row[] =>
  Array.isArray(value) ? value.map(object) : [];
const string = (value: unknown) => (typeof value === "string" ? value : "");

// Display copies only. Full, redacted provider evidence always remains available.
export function displayTrace(evidence: Row) {
  const data = object(evidence.data || evidence);
  const body = object(data.body || data);
  const prompts: { role: string; content: unknown }[] = [];
  if (typeof body.instructions === "string")
    prompts.push({ role: "system", content: body.instructions });
  if (body.systemInstruction)
    prompts.push({
      role: "system",
      content: object(body.systemInstruction).parts,
    });
  if (body.system) prompts.push({ role: "system", content: body.system });
  const messages = body.messages || body.input || body.contents;
  if (typeof messages === "string")
    prompts.push({ role: "user", content: messages });
  for (const message of rows(messages)) {
    prompts.push({
      role: string(message.role) || string(message.type) || "input",
      content: message.content ?? message.parts ?? message,
    });
  }

  const reasoning: string[] = [],
    replies: string[] = [];
  function block(value: Row) {
    if (value.type === "thinking") reasoning.push(string(value.thinking));
    else if (value.type === "reasoning") {
      for (const summary of rows(value.summary))
        reasoning.push(string(summary.text));
    } else if (value.thought === true) reasoning.push(string(value.text));
    else if (typeof value.text === "string") replies.push(value.text);
    if (typeof value.refusal === "string") replies.push(value.refusal);
  }
  const message = object(rows(body.choices)[0]?.message || body);
  if (typeof message.reasoning_content === "string")
    reasoning.push(message.reasoning_content);
  if (typeof message.content === "string") replies.push(message.content);
  else for (const value of rows(message.content)) block(value);
  for (const output of rows(body.output)) {
    block(output);
    for (const content of rows(output.content)) block(content);
  }
  for (const candidate of rows(body.candidates)) {
    for (const part of rows(object(candidate.content).parts)) block(part);
  }
  return {
    prompts,
    reasoning: reasoning.filter(Boolean),
    replies: replies.filter(Boolean),
  };
}
