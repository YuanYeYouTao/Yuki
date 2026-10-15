import { expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AutonomyParameterFields } from "./autonomy-parameters";

it("edits the review span using the loaded schema and preserves real rate overrides", () => {
  const change = vi.fn();
  render(
    <AutonomyParameterFields
      fields={{
        parameter_schema: {
          type: "object",
          properties: {
            continuation_review_span: { type: "integer", minimum: 1 },
            source_interval_seconds: { type: "number", exclusiveMinimum: 0 },
          },
        },
      }}
      document={{ continuation_review_span: 4, source_interval_seconds: 45 }}
      change={change}
    />,
  );
  const span = screen.getByLabelText("未核验快接跨度（条真人输入）");
  expect(span).toHaveValue(4);
  expect(span).toHaveAttribute("min", "1");
  expect(span).toHaveAttribute("step", "1");
  fireEvent.change(span, { target: { value: "3" } });
  expect(change).toHaveBeenCalledWith({
    continuation_review_span: 3,
    source_interval_seconds: 45,
  });
});
