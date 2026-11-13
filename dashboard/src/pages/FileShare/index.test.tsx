import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { App } from "antd";

vi.mock("../../api/modules/fileShares", () => ({
  fileSharesApi: { info: vi.fn(), download: vi.fn(), revoke: vi.fn() },
}));
vi.mock("../../utils/saveBlobAsFile", () => ({ saveBlobAsFile: vi.fn() }));

import { fileSharesApi } from "../../api/modules/fileShares";
import { saveBlobAsFile } from "../../utils/saveBlobAsFile";
import FileSharePage from "./index";

function showPage() {
  return render(
    <App>
      <MemoryRouter initialEntries={["/file-shares/example"]}>
        <Routes>
          <Route path="/file-shares/:shareId" element={<FileSharePage />} />
        </Routes>
      </MemoryRouter>
    </App>,
  );
}

describe("private file sharing", () => {
  beforeEach(() => vi.clearAllMocks());

  it("offers no download or file metadata to an unauthorized account", async () => {
    vi.mocked(fileSharesApi.info).mockRejectedValue(new Error("404"));
    showPage();
    expect(
      await screen.findByText("fileSharing.unavailable"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "common.download" }),
    ).not.toBeInTheDocument();
    expect(fileSharesApi.download).not.toHaveBeenCalled();
  });

  it("lets a named recipient download without granting revocation", async () => {
    vi.mocked(fileSharesApi.info).mockResolvedValue({
      filename: "report.txt",
      size: 3,
      can_revoke: false,
    });
    const blob = new Blob(["abc"]);
    vi.mocked(fileSharesApi.download).mockResolvedValue(blob);
    showPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "common.download" }),
    );
    await waitFor(() =>
      expect(saveBlobAsFile).toHaveBeenCalledWith(blob, "report.txt"),
    );
    expect(
      screen.queryByRole("button", { name: "fileSharing.revoke" }),
    ).not.toBeInTheDocument();
  });

  it("removes the download after its owner revokes the share", async () => {
    vi.mocked(fileSharesApi.info).mockResolvedValue({
      filename: "report.txt",
      size: 3,
      can_revoke: true,
    });
    vi.mocked(fileSharesApi.revoke).mockResolvedValue(undefined);
    showPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "fileSharing.revoke" }),
    );
    await waitFor(() =>
      expect(fileSharesApi.revoke).toHaveBeenCalledWith("example"),
    );
    expect(
      await screen.findByText("fileSharing.unavailable"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "common.download" }),
    ).not.toBeInTheDocument();
  });
});
