import { describe, expect, it } from "vitest";
import { getCanvasCoords } from "./browserCanvas";

function makeCanvas(opts: {
  width: number;
  height: number;
  rect: { left: number; top: number; width: number; height: number };
  objectFit?: string;
}): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = opts.width;
  canvas.height = opts.height;
  if (opts.objectFit) {
    canvas.style.objectFit = opts.objectFit;
  }
  canvas.getBoundingClientRect = () =>
    ({
      left: opts.rect.left,
      top: opts.rect.top,
      right: opts.rect.left + opts.rect.width,
      bottom: opts.rect.top + opts.rect.height,
      width: opts.rect.width,
      height: opts.rect.height,
      x: opts.rect.left,
      y: opts.rect.top,
      toJSON: () => ({}),
    }) as DOMRect;
  return canvas;
}

describe("getCanvasCoords", () => {
  it("maps pointer directly when object-fit is not contain", () => {
    const canvas = makeCanvas({
      width: 1280,
      height: 800,
      rect: { left: 10, top: 20, width: 640, height: 400 },
    });

    expect(getCanvasCoords(canvas, { clientX: 330, clientY: 220 })).toEqual({
      x: 640,
      y: 400,
    });
  });

  it("accounts for vertical letterboxing under object-fit: contain", () => {
    // 1440x900 (16:10) rendered inside a tall 360x600 side panel:
    // rendered box is 360x225, vertically centered with 187.5px top/bottom bars.
    const canvas = makeCanvas({
      width: 1440,
      height: 900,
      rect: { left: 100, top: 50, width: 360, height: 600 },
      objectFit: "contain",
    });

    // Top-left of actual rendered frame: clientX=100, clientY=50+187.5=237.5
    expect(getCanvasCoords(canvas, { clientX: 100, clientY: 237.5 })).toEqual({
      x: 0,
      y: 0,
    });

    // Center of rendered frame: clientX=280, clientY=350
    expect(getCanvasCoords(canvas, { clientX: 280, clientY: 350 })).toEqual({
      x: 720,
      y: 450,
    });

    // Bottom-right of rendered frame: clientX=460, clientY=50+187.5+225=462.5
    expect(getCanvasCoords(canvas, { clientX: 460, clientY: 462.5 })).toEqual({
      x: 1440,
      y: 900,
    });

    // Clicking in top/bottom black bars clamps to [0, height]
    expect(getCanvasCoords(canvas, { clientX: 280, clientY: 60 })).toEqual({
      x: 720,
      y: 0,
    });
    expect(getCanvasCoords(canvas, { clientX: 280, clientY: 620 })).toEqual({
      x: 720,
      y: 900,
    });
  });

  it("accounts for horizontal pillarboxing under object-fit: contain", () => {
    // 1440x900 (16:10) rendered inside a wide 1000x300 container:
    // rendered box is 480x300, horizontally centered with 260px left/right bars.
    const canvas = makeCanvas({
      width: 1440,
      height: 900,
      rect: { left: 0, top: 0, width: 1000, height: 300 },
      objectFit: "contain",
    });

    expect(getCanvasCoords(canvas, { clientX: 260, clientY: 0 })).toEqual({
      x: 0,
      y: 0,
    });
    expect(getCanvasCoords(canvas, { clientX: 740, clientY: 300 })).toEqual({
      x: 1440,
      y: 900,
    });
  });
});
