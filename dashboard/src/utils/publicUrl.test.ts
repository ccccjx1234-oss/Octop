import { afterEach, describe, expect, it, vi } from "vitest";
import { publicAssetUrl, publicUrl, routerBasename } from "./publicUrl";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("publicUrl", () => {
  it("prefixes the Vite base path", () => {
    expect(publicUrl("sw.js")).toMatch(/sw\.js$/);
    expect(publicUrl("/logo.png")).toMatch(/logo\.png$/);
  });
});

describe("publicAssetUrl", () => {
  it("prefixes bundled assets under a subpath", () => {
    vi.stubEnv("BASE_URL", "/octop/");
    expect(publicAssetUrl("/experts/avatars/team-host.svg")).toBe(
      "/octop/experts/avatars/team-host.svg",
    );
    expect(publicAssetUrl("/octop-mascot-peek.webp")).toBe(
      "/octop/octop-mascot-peek.webp",
    );
  });

  it("leaves API, external, data, and blob URLs untouched", () => {
    vi.stubEnv("BASE_URL", "/octop/");
    expect(publicAssetUrl("/api/agents/a/avatar")).toBe("/api/agents/a/avatar");
    expect(publicAssetUrl("https://example.com/a.png")).toBe(
      "https://example.com/a.png",
    );
    expect(publicAssetUrl("data:image/png;base64,abc")).toBe(
      "data:image/png;base64,abc",
    );
    expect(publicAssetUrl("blob:https://example.com/a")).toBe(
      "blob:https://example.com/a",
    );
  });
});

describe("routerBasename", () => {
  it("is undefined at domain root", () => {
    // Vitest default BASE_URL is "/".
    expect(routerBasename()).toBeUndefined();
  });
});
