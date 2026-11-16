import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AskQuestionCard from "./AskQuestionCard";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const questions = [
  {
    header: "Framework",
    question: "Which framework?",
    options: [{ label: "React" }, { label: "Vue" }],
  },
  {
    header: "Database",
    question: "Which databases?",
    multi_select: true,
    options: [{ label: "PostgreSQL" }, { label: "Redis" }],
  },
];

describe("AskQuestionCard", () => {
  it("shows exactly one question at a time", () => {
    render(
      <AskQuestionCard
        questions={questions}
        status="pending"
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText("Which framework?")).toBeInTheDocument();
    expect(screen.queryByText("Which databases?")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /React/ }));

    expect(screen.queryByText("Which framework?")).not.toBeInTheDocument();
    expect(screen.getByText("Which databases?")).toBeInTheDocument();
  });

  it("collects all answers before submitting once", () => {
    const onSubmit = vi.fn();
    render(
      <AskQuestionCard
        questions={questions}
        status="pending"
        onSubmit={onSubmit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /React/ }));
    fireEvent.click(screen.getByRole("button", { name: /PostgreSQL/ }));
    fireEvent.click(screen.getByRole("button", { name: "chat.ask.review" }));

    expect(screen.getByText("Which framework?")).toBeInTheDocument();
    expect(screen.getByText("Which databases?")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "chat.ask.submit" }));
    expect(onSubmit).toHaveBeenCalledWith(
      "Framework: React\nDatabase: PostgreSQL",
    );
  });

  it("renders open-ended questions as a text field directly", () => {
    render(
      <AskQuestionCard
        questions={[{ question: "Anything else?" }]}
        status="pending"
        onSubmit={vi.fn()}
      />,
    );

    expect(
      screen.getByPlaceholderText("chat.ask.freeTextPlaceholder"),
    ).toBeInTheDocument();
    expect(screen.queryByText("chat.ask.other")).not.toBeInTheDocument();
  });

  it("closes the card without answering", () => {
    const onSubmit = vi.fn();
    const onDismiss = vi.fn();
    render(
      <AskQuestionCard
        questions={questions}
        status="pending"
        onSubmit={onSubmit}
        onDismiss={onDismiss}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "chat.ask.dismiss" }));

    expect(onDismiss).toHaveBeenCalledOnce();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("collapses an answered question set in message history", () => {
    const { container } = render(
      <AskQuestionCard questions={questions} status="approved" />,
    );

    const details = container.querySelector("details");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("chat.ask.answeredSummary")).toBeInTheDocument();
    expect(screen.queryByText("Which framework?")).not.toBeVisible();

    fireEvent.click(container.querySelector("summary")!);
    expect(details).toHaveAttribute("open");
    expect(screen.getByText("Which framework?")).toBeVisible();
  });

  it("renders up to 20 searchable master-data options while preserving keys and Other", () => {
    const onSubmit = vi.fn();
    const customerOptions = Array.from({ length: 20 }, (_, i) => ({
      label: `客户-${String(i + 1).padStart(2, "0")}`,
      description: i === 14 ? "重点品牌联系人：王五" : `普通客户 ${i + 1}`,
    }));

    render(
      <AskQuestionCard
        questions={[
          {
            header: "选择客户",
            question: "请选择要下单的客户（共 45 条，第 1 页）",
            options: customerOptions,
          },
        ]}
        status="pending"
        onSubmit={onSubmit}
      />,
    );

    const searchInput = screen.getByPlaceholderText(
      "chat.ask.filterPlaceholder",
    );
    expect(searchInput).toBeInTheDocument();
    expect(screen.getByText("客户-20")).toBeInTheDocument();
    // 20 options = A..T, so Other gets U
    const otherBtn = screen.getByRole("button", { name: /chat\.ask\.other/ });
    expect(otherBtn).toHaveTextContent("U");

    fireEvent.change(searchInput, { target: { value: "王五" } });
    expect(screen.queryByText("客户-01")).not.toBeInTheDocument();
    const matchedBtn = screen.getByRole("button", { name: /客户-15/ });
    // Original 15th option keeps badge O even when filtered
    expect(matchedBtn).toHaveTextContent("O");
    expect(
      screen.getByRole("button", { name: /chat\.ask\.other/ }),
    ).toBeInTheDocument();

    fireEvent.click(matchedBtn);
    fireEvent.click(screen.getByRole("button", { name: "chat.ask.submit" }));
    expect(onSubmit).toHaveBeenCalledWith("选择客户: 客户-15");
  });
});
