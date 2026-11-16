import { screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { indexOptions, methodOption } from "../../test/adminData";
import { renderWithProviders } from "../../test/render";
import { ChangeWarning, IndexForm, canSendIndexForm, indexValueText } from "./IndexForm";
import { defaultIndexValues } from "./indexForm";

const OPTIONS = indexOptions();

describe("the index form", () => {
  test("values read as text for conflict lines", () => {
    expect(indexValueText(OPTIONS, "description", "")).toBe("Not set");
    expect(indexValueText(OPTIONS, "description", "Policies")).toBe("Policies");
    expect(indexValueText(OPTIONS, "method", "pagewise@1")).toBe("Page by page");
    expect(indexValueText(OPTIONS, "method", "gone@3")).toBe("gone@3");
    expect(indexValueText(OPTIONS, "languageAnalyzer", "de.microsoft")).toBe("German");
    expect(indexValueText(OPTIONS, "languageAnalyzer", "xx")).toBe("xx");
    expect(indexValueText(OPTIONS, "methodParameters", { targetTokens: 640 })).toBe(
      "targetTokens: 640",
    );
    expect(indexValueText(undefined, "embeddingDimensions", 256)).toBe("256");
  });

  test("conflict lines under the method, its settings and the model, with Use latest value", async () => {
    const onUseLatest = vi.fn();
    const { user } = renderWithProviders(
      <IndexForm
        options={OPTIONS}
        values={defaultIndexValues(OPTIONS)}
        onChange={vi.fn()}
        errors={new Map()}
        conflicts={{
          method: "pagewise@1",
          methodParameters: { targetTokens: 640 },
          embeddingModel: "text-embedding-3-small",
        }}
        onUseLatest={onUseLatest}
        register={() => () => undefined}
        warning={<ChangeWarning />}
        actions={null}
      />,
    );
    expect(screen.getByText("Latest saved value: Page by page")).toBeInTheDocument();
    expect(screen.getByText("Latest saved value: targetTokens: 640")).toBeInTheDocument();
    expect(screen.getByText("Latest saved value: text-embedding-3-small")).toBeInTheDocument();
    expect(
      screen.getByText(/Saving this change means every document must be re-ingested/),
    ).toBeInTheDocument();
    await user.click(
      screen.getAllByRole("button", { name: "Use latest value" })[0] ?? document.body,
    );
    expect(onUseLatest).toHaveBeenCalledWith("method");
  });

  test("a method whose schema isn't supported can't be sent", () => {
    const options = indexOptions({
      methods: [methodOption({ parameterSchema: { type: "object" } })],
    });
    expect(canSendIndexForm(options, defaultIndexValues(options))).toBe(false);
    expect(canSendIndexForm(OPTIONS, defaultIndexValues(OPTIONS))).toBe(true);
  });
});
