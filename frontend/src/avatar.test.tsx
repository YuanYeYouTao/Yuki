import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { Avatar } from "./names";

const canonical = "337656ad-9609-4d7e-b62d-8380b92e1582";

it.each([
  ["person", "远野"],
  ["presence", "Yuki"],
  ["space", "数字生命研究所"],
] as const)(
  "loads a %s QQ avatar and falls back to the name on image failure",
  (kind, name) => {
    const { container } = render(
      <div className="avatar">
        <Avatar
          kind={kind}
          id={canonical}
          label={`${name} 的头像`}
          fallback={name}
        />
      </div>,
    );
    const photo = screen.getByRole("img", { name: `${name} 的头像` });
    expect(photo).toHaveAttribute(
      "src",
      `/api/control/files/avatar/${kind}/${canonical}`,
    );
    expect(photo).toHaveClass("avatar-photo");
    fireEvent.error(photo);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(container.querySelector(".avatar-initial")).toHaveTextContent(
      Array.from(name)[0],
    );
  },
);
